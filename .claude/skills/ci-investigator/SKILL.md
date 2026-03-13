---
name: ci-investigator
description: >
  Investigate CI failures, assess CI health, and perform root cause analysis
  using VictoriaMetrics and VictoriaLogs. Use when asked about broken builds,
  flaky tests, CI health, or infrastructure issues.
argument-hint: "[PR number, build ID, job name, or problem description]"
allowed-tools: Bash, Read, Grep, Glob, mcp__chrome-devtools__*
---

# CI Investigator

Investigate OpenShift CI failures by querying VictoriaMetrics (metrics) and VictoriaLogs (logs). Read-only analysis -- query, correlate, classify, recommend.

## Principles

- **Investigation-only**: query and analyze, never modify production data or infrastructure
- **Root cause first**: trace symptoms to origin -- don't stop at "step X failed"
- **Evidence-based**: every conclusion backed by query results
- **Human decides**: present findings with confidence levels; the engineer makes the call

## Services & Data

| Service | URL | API |
|---------|-----|-----|
| VictoriaMetrics | `http://localhost:8428` | PromQL via `/api/v1/query` |
| VictoriaLogs | `http://localhost:9428` | LogsQL via `/select/logsql/query` |
| Grafana | `http://localhost:3000` | Dashboards (visual verification) |

**Status:** !`curl -s http://localhost:8428/api/v1/query --data-urlencode 'query=count(last_over_time(ci_build_scraped[90d]))' 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'VM up -- {d[\"data\"][\"result\"][0][\"value\"][1]} builds ingested')" || echo "VictoriaMetrics not reachable -- start services with 'make up'"`
!`curl -sf http://localhost:9428/health >/dev/null 2>&1 && echo "VictoriaLogs up" || echo "VictoriaLogs not reachable"`

**External links:**
- Prow job: `https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/{org}_{repo}/{pr_number}/{job_name}/{build_id}`
- GitHub PR: `https://github.com/{org}/{repo}/pull/{pr_number}`

**References:**
- Raw JSON field structure: `docs/appendix/ci-operator-metrics.md`
- System architecture: `ARCHITECTURE.md`

## Query Tool

All queries go through `.claude/skills/ci-investigator/ci-query <command> [args...]`. This script outputs JSON lines (one JSON object per line). Run `ci-query help` for the full command list.

See `query-recipes.md` for detailed usage of each subcommand with interpretation guidance.

### Key Commands by Phase

| Phase | Commands |
|-------|----------|
| Health check | `health`, `top-failing-steps`, `top-failing-prs` |
| Discovery | `list-jobs`, `list-prs` |
| Scope to PR | `builds-for-pr`, `pr-success-rate`, `flakiness` |
| Scope to build | `build-info`, `step-failures`, `step-timeline` |
| Root cause | `error-logs`, `warning-logs`, `search-logs`, `step-offsets`, `pod-outcomes`, `scheduling-latency` |
| Cross-build | `cross-pr-errors`, `step-consistency` |

## Key Metrics

The scraper extracts all numeric fields from `ci-operator-metrics.json` as Prometheus metrics with prefix `ci_{section}_{field_path}`. These are the most useful for investigation:

| Metric | What It Tells You |
|--------|-------------------|
| `ci_test_platform_insights_additional_context_duration_seconds{name="execution_completed"}` | Build outcome and total duration. Filter by `success="true"` or `success="false"`. |
| `ci_step_duration_seconds{source, level}` | Per-step timing. `level="Error"` = step failed. `source` = step name. |
| `ci_step_relative_start_seconds{source}` | Step start relative to pipeline start (for timeline reconstruction). |
| `ci_pod_scheduling_latency_seconds` | Time from pod creation to scheduled. High values = cluster pressure. |
| `ci_pods_completion_latency{pod_phase}` | Pod completion time. `pod_phase` = Succeeded/Failed. |
| `ci_build_scraped` | Sentinel: value=1 for each processed build. Use to check data exists. |

**Common labels** (on all metrics): `org`, `repo`, `branch`, `job_name`, `build_id`, `pr_number`, `pr_sha`, `author`
**Step-level labels**: `source` (step name), `level` (severity), `name` (insight entry name), `success`
**Pod-level labels**: `pod_name`, `pod_phase`, `namespace`

## Log Fields

VictoriaLogs stores parsed `ci-operator.log` entries with:
- `_time`, `_msg`: timestamp and message
- `level`: log severity (**mixed case** -- ci-operator uses lowercase, events use capitalized)
- `component`, `source`: log metadata
- Job labels merged into each entry: `job_name`, `build_id`, `pr_number`, `org`, `repo`, etc.
- Stream fields: `job_name`, `build_id`

## Investigation Workflow

### Phase 0: Parse Input

Route by what the user provides:

| Input | Action |
|-------|--------|
| Build ID (numeric) | Go to Phase 2 -- scope to build |
| PR number | Go to Phase 2 -- scope to PR |
| Job name | Go to Phase 2 -- scope to job |
| "Is CI healthy?" / no args | Go to Phase 1 -- health snapshot |
| Vague problem description | Go to Phase 1 first, then narrow |

### Phase 1: Triage (Health Snapshot)

Run `ci-query health`. Interpret results:

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Success rate (24h) | >80% | 60-80% | <60% |
| Avg retests per commit | <2 | 2-3 | >3 |
| Avg scheduling latency | <10s | 10-30s | >30s |
| Max scheduling latency | <60s | 60-120s | >120s |

Follow up with `ci-query top-failing-steps` and `ci-query top-failing-prs` to identify hotspots. Proceed to Phase 2 with the most impactful issue.

### Phase 2: Scope

Narrow focus to the specific problem area:
- **For a PR**: `builds-for-pr`, `pr-success-rate`, compare against global from `health`
- **For a build**: `build-info`, `step-failures`
- **For a job type**: aggregate failure patterns via `top-failing-steps`

Key question: **Is this PR-specific or systemic?** Compare PR success rate vs global. If similar, the problem is upstream.

### Phase 3: Investigate (Root Cause Trace)

Trace the failure chain:

1. **What failed?** -- `step-failures <build_id>`
2. **How did it fail?** -- `error-logs <build_id>`, expand with `all-logs` around key errors
3. **Is it consistent?** -- `step-consistency <pr_number>`, `flakiness <pr_number>`
4. **Infrastructure or test?** -- `scheduling-latency <build_id>`, `pod-outcomes <build_id>`
5. **What's the pattern?** -- Match against known failure signatures (see Classification)

### Phase 4: Classify

```
Build failed?
  |
  +-- Duration < 5 min AND no test steps ran?
  |     -> INFRASTRUCTURE (setup/provisioning failure)
  |
  +-- Same step fails across multiple PRs?
  |     -> SYSTEMIC (platform issue, not PR-specific)
  |
  +-- Same step fails on retries of same PR?
  |     +-- Different step each time? -> FLAKY INFRASTRUCTURE
  |     +-- Same step every time? -> DETERMINISTIC FAILURE
  |           +-- Step is e2e/test? -> TEST FAILURE
  |           +-- Step is build/images? -> BUILD FAILURE
  |           +-- Step is ipi-install/provision? -> CLUSTER PROVISIONING
  |           +-- Step is gather? -> SECONDARY (look for earlier failure)
  |           +-- Step is lease? -> RESOURCE QUOTA
  |
  +-- Log pattern match?
        +-- timeout/deadline -> TIMEOUT
        +-- OOM/memory -> RESOURCE EXHAUSTION
        +-- quota/limit -> QUOTA
        +-- image pull/not found -> IMAGE ISSUE
        +-- API incompatibility/scheme -> API BREAKING CHANGE
        +-- network/connection refused -> NETWORK
```

Consult `known-patterns.md` for additional domain-specific signatures.

### Phase 5: Recommend

| Classification | Recommendation |
|---------------|----------------|
| INFRASTRUCTURE | Check cluster health, retry, escalate if persistent |
| SYSTEMIC | Identify affected component, check platform-wide incidents |
| FLAKY INFRASTRUCTURE | Retry; if persistent, investigate infra instability |
| DETERMINISTIC FAILURE | Debug the specific step; likely code or test issue |
| TEST FAILURE | Review test code and PR changes for compatibility |
| BUILD FAILURE | Check compilation errors, dependency changes |
| CLUSTER PROVISIONING | Cloud provider issues, quota, region availability |
| TIMEOUT | Check if timeout is too short or target is genuinely slow |
| RESOURCE EXHAUSTION | Check resource requests/limits, node capacity |
| QUOTA | Check cloud quota, lease availability |
| IMAGE ISSUE | Check image references, registry availability |
| API BREAKING CHANGE | Check upstream API changes, version skew |
| NETWORK | Transient or DNS; retry first, investigate if persistent |

## Output Format

Structure findings as:

```
## CI Investigation: [scope]

**Classification**: [category] (confidence: high/medium/low)
**Scope**: [PR-specific | systemic | unknown]

### Evidence
- [query result 1]
- [query result 2]

### Root Cause
[Traced failure chain: symptom -> immediate cause -> underlying cause]

### Recommendation
[Action items for the engineer]

### Links
- [Prow job URL]
- [GitHub PR URL]
```

## Knowledge Capture

- Read `known-patterns.md` at the start of investigations to leverage accumulated domain knowledge.
- When you discover a **stable domain pattern** during investigation (recurring failure signature, metric correlation, new failure category), append it to `known-patterns.md`.
- Don't capture session-specific findings -- that's conversation context.
- Patterns must be observed across multiple builds or PRs to be considered stable.

## Extending the Query Tool

When a new query is needed during investigation:

1. Add a `cmd_<name>(args)` function to `.claude/skills/ci-investigator/ci-query`
2. Register it in the `COMMANDS` dict with usage string and description
3. Use `vm_query()`/`vm_scalar()` for VictoriaMetrics, `vl_query()` for VictoriaLogs
4. Output via `emit()` (one JSON object per result)
5. Document the new command in `query-recipes.md`

The script is designed for easy extension -- each command is a standalone function.

## Tips

- **Check data exists first**: run `ci-query health` before complex queries. Zero builds = no data.
- **VictoriaLogs levels are mixed-case**: ci-operator uses lowercase (`error`), events use capitalized (`Error`). The ci-query tool handles this with case-insensitive matching.
- **Error logs first**: start with `error-logs`, then expand context with `all-logs` around key messages.
- **Step names are in `source` label**: e.g., `steps.clusterClaimStep`, `release.importReleaseStep`.
- **Grafana dashboards** provide visual context. Use chrome-devtools MCP to screenshot relevant panels.
- **Labels override log fields**: job labels (build_id, pr_number, etc.) take precedence over same-named fields in log entries.
