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
- **Challenge your own conclusions**: when you think you've found the root cause, stop. Ask: "if this is the cause, what other symptoms should I see?" Look for them. Then ask: "what else could cause these symptoms?" Look for evidence of alternatives. A plausible explanation is not a verified root cause. In distributed systems like OpenShift CI (multiple clusters, shared infrastructure, cloud providers), the first plausible explanation is often incomplete or wrong.
- **Self-sufficient first**: cross-team coordination is expensive and slow. When the root cause points to another team's component, develop a workaround we can implement ourselves. Report the issue upstream as a courtesy so they can fix it properly, but never make escalation our path to resolution. Our CI can't wait for upstream fixes.
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

## CI Infrastructure Context

!`cat docs/appendix/openshift-ci-infrastructure.md`

## Query Tool

All queries go through `.claude/skills/ci-investigator/ci-query <command> [args...]`. This script outputs JSON lines (one JSON object per line). Run `ci-query help` for the full command list.

See `query-recipes.md` for detailed usage of each subcommand with interpretation guidance.

### Key Commands by Phase

| Phase | Commands |
|-------|----------|
| Health check | `health`, `top-failing-steps`, `top-failing-prs`, `top-failing-tests` |
| Discovery | `list-jobs`, `list-prs` |
| Scope to PR | `builds-for-pr`, `pr-success-rate`, `flakiness` |
| Scope to build | `build-info`, `step-failures`, `step-timeline`, `junit-steps`, `junit-tests` |
| Root cause | `error-logs`, `warning-logs`, `search-logs`, `step-offsets`, `pod-outcomes`, `scheduling-latency` |
| Cross-build | `cross-pr-errors`, `step-consistency`, `error-impact` |
| Infrastructure | `build-latency` |

## Key Metrics

The scraper extracts all numeric fields from `ci-operator-metrics.json` as Prometheus metrics with prefix `ci_{section}_{field_path}`. These are the most useful for investigation:

| Metric | What It Tells You |
|--------|-------------------|
| `ci_test_platform_insights_additional_context_duration_seconds{name="execution_completed"}` | Build outcome and total duration. Filter by `success="true"` or `success="false"`. |
| `ci_step_duration_seconds{source, level}` | Per-step timing. `level="Error"` = step failed. `source` = step name. |
| `ci_step_relative_start_seconds{source}` | Step start relative to pipeline start (for timeline reconstruction). |
| `ci_pod_scheduling_latency_seconds` | Time from pod creation to scheduled. High values = cluster pressure. |
| `ci_pods_completion_latency{pod_phase}` | Pod completion time. `pod_phase` = Succeeded/Failed. |
| `ci_junit_step_duration_seconds{step_name, status}` | Per-step duration from JUnit XML. `status` = passed/failed/skipped. `step_name` = human-readable step description. |
| `ci_junit_test_duration_seconds{test_name, suite, status, test_variant, leaf}` | Per-test-case duration from JUnit XML. `test_name` = Go test path. `test_variant` = e2e variant. `leaf="true"` for leaf tests (no children). |
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

JUnit entries use `source` to distinguish record types:
- `source:junit_step` -- step-level results from `junit_operator.xml`. Fields: `step_name`, `status`, `duration_seconds`, `_msg` (failure message)
- `source:junit_test` -- test-case results from `junit_report.xml`. Fields: `test_name`, `status`, `duration_seconds`, `test_variant`, `_msg` (failure message)

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

### Phase 3b: Validate Hypothesis

Phase 3 gives you a candidate root cause. Do NOT present it yet. First, stress-test it:

1. **State the hypothesis explicitly**: "I believe the root cause is X because Y"
2. **Predict expected symptoms**: if X is the root cause, what else should be true?
   - If quota is exhausted, other PRs should be failing too
   - If a controller is broken, errors should appear in multiple namespaces
   - If a node is unhealthy, other pods on that node should be affected
   - If it's a flaky test, the same test should fail intermittently across PRs
3. **Search for corroborating evidence**: run queries to find predicted symptoms. Finding them strengthens the hypothesis.
4. **Search for contradicting evidence**: what would disprove this? Look for it. Not finding contradictions is weak support; finding them means revise the hypothesis.
5. **Consider alternatives**: what else could produce these symptoms? For each, identify distinguishing evidence and look for it.
6. **Assess confidence**:
   - **High**: predicted symptoms present AND alternatives ruled out by evidence
   - **Medium**: symptoms present but alternatives not fully eliminated
   - **Low**: plausible but insufficient evidence -- say so and identify what data would resolve it

If confidence is low, look for the missing data before presenting. If the data isn't available, explicitly state what you can't verify and why.

**Example**: You find `serviceaccount "default" not found` errors. Before concluding "SA race condition":
- Check: does the error appear in multiple namespaces? (If yes, it's cluster-wide, not a race)
- Check: does it appear in the must-gather namespace? (If yes, the SA controller is broken, not a namespace race)
- Check: do healthy builds on the same cluster have this? (If no, it's cluster-specific)
- Alternative: could this be a hibernation resume issue? Check claim-to-ready times.

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

Every recommendation MUST follow this structure, in this priority order:

**1. Workaround (what can WE do right now?)**
Think about what's in our control. Can we:
- Add retry logic, adjust timeouts, add fallback paths in our code?
- Detect this condition and skip/handle it gracefully?
- Modify our scraper, tooling, or test configuration to avoid the problem?
- Add monitoring/alerting so we catch this faster next time?
- Filter out or quarantine the broken component (e.g., remove a bad cluster from the pool)?

The goal: our CI keeps running even if the underlying problem persists.

**2. Permanent fix (what should be done properly?)**
- Is this something we own and can fix in our code/config?
- If it requires changes to shared infrastructure, what specifically needs to change?

**3. Upstream report (if the root cause is outside our control)**
- Report as a courtesy to help others, NOT as our path to resolution
- Include the evidence and diagnosis -- save the upstream team the investigation work
- Be specific: "component X has bug Y, here's the evidence" not "something is broken"

Never present "escalate to team X" as the primary recommendation. If the only path is escalation, explain what workarounds you considered and why they're insufficient.

## Output Format

Structure findings as:

```
## CI Investigation: [scope]

**Classification**: [category]
**Scope**: [PR-specific | systemic | unknown]

### Evidence
- [query result 1]
- [query result 2]

### Root Cause
[Traced failure chain: symptom -> immediate cause -> underlying cause]

### Hypothesis Validation
- **Hypothesis**: [what you believe the root cause is and why]
- **Expected symptoms**: [what else should be true if this is correct]
- **Corroborating evidence**: [evidence found that supports the hypothesis]
- **Contradicting evidence**: [evidence that weakens it, or "none found" with what you looked for]
- **Alternatives considered**: [other possible causes and why they're less likely]
- **Confidence**: [high/medium/low -- high = symptoms confirmed + alternatives ruled out]

### Recommendation
**Workaround (now):** [what we implement ourselves to keep CI running]
**Permanent fix:** [proper solution, whether ours or upstream]
**Upstream report:** [if applicable -- what to tell the responsible team, with evidence]

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
