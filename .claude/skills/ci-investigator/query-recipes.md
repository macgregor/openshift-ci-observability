# Query Recipes

Reference for the `ci-query` tool subcommands. Each section describes what the command returns, how to use it, and how to interpret the results.

All commands output JSON lines (one JSON object per line). Empty output means no matching data.

---

## Phase 1: Triage

### `health [window]`

Returns a single JSON object with CI health metrics for the given time window (default: `24h`).

```
ci-query health        # last 24 hours
ci-query health 7d     # last 7 days
```

**Output fields:** `window`, `builds_ingested`, `total_builds`, `passed_builds`, `failed_builds`, `success_rate_pct`, `avg_scheduling_latency_s`, `max_scheduling_latency_s`

**Interpretation:**
- `success_rate_pct` < 60% is critical, 60-80% is warning, >80% is healthy
- `avg_scheduling_latency_s` > 30s indicates cluster pressure
- `max_scheduling_latency_s` > 120s may cause cascading timeouts
- `builds_ingested` should be close to `total_builds`; large gap means some builds lack outcome data

### `top-failing-steps [window] [limit]`

Steps ranked by how many distinct builds they failed in.

```
ci-query top-failing-steps         # 24h, top 10
ci-query top-failing-steps 7d 20   # 7 days, top 20
```

**Output fields:** `source` (step name), `failure_count`

**Interpretation:**
- `steps.clusterClaimStep` failures suggest cluster provisioning issues
- `steps.sourceStep` failures suggest source code fetch problems
- `release.importReleaseStep` failures suggest release image issues
- If one step dominates, it's likely systemic (not PR-specific)

### `top-failing-prs [window] [limit]`

PRs ranked by failure count (distinct builds).

```
ci-query top-failing-prs           # 24h, top 10
ci-query top-failing-prs 7d 20
```

**Output fields:** `pr_number`, `failure_count`

**Interpretation:**
- High failure count on one PR with low failures elsewhere = PR-specific issue
- Many PRs with similar failure counts = systemic issue
- Follow up with `builds-for-pr` on the top PR

---

## Discovery

### `list-jobs`

All job names with build counts across the full data window.

```
ci-query list-jobs
```

**Output fields:** `job_name`, `build_count`

### `list-prs [limit]`

PR numbers ranked by build count (default: top 20).

```
ci-query list-prs
ci-query list-prs 50
```

**Output fields:** `pr_number`, `build_count`

---

## Phase 2: Scope

### `builds-for-pr <pr_number>`

All builds for a PR with outcome and duration.

```
ci-query builds-for-pr 3221
```

**Output fields:** `build_id`, `job_name`, `success`, `duration_s`, `pr_sha`, `author`

**Interpretation:**
- Look for patterns: same job failing repeatedly? Different SHAs with different outcomes?
- Short duration + failure = infrastructure issue (build didn't reach tests)
- Compare `pr_sha` values to see if a new push changed the outcome

### `build-info <build_id>`

Detailed info for a single build.

```
ci-query build-info 2031722177379700736
```

**Output fields:** `build_id`, `job_name`, `pr_number`, `pr_sha`, `success`, `duration_s`, `author`, `org`, `repo`, `branch`

### `pr-success-rate <pr_number>`

Success rate for a specific PR. Compare against global rate from `health` to determine if the PR is an outlier.

```
ci-query pr-success-rate 3221
```

**Output fields:** `pr_number`, `total_builds`, `passed_builds`, `failed_builds`, `success_rate_pct`

**Interpretation:**
- PR rate significantly below global rate = PR-specific issue
- PR rate similar to global rate = systemic issue, not the PR's fault

---

## Phase 3: Investigate

### `step-failures <build_id>`

Only the failed steps for a build.

```
ci-query step-failures 2031722177379700736
```

**Output fields:** `source` (step name), `duration_s`

### `step-timeline <build_id>`

All steps sorted by duration (longest first), with pass/fail status.

```
ci-query step-timeline 2031722177379700736
```

**Output fields:** `source`, `duration_s`, `level`, `failed` (boolean)

**Interpretation:**
- Failed steps with short duration = early failure (likely setup/infra)
- Failed steps with long duration = timeout or slow failure
- Look at the ratio of failed step duration to total build duration

### `step-offsets <build_id>`

Step start times relative to pipeline start, sorted chronologically. Useful for understanding execution order and parallelism.

```
ci-query step-offsets 2031722177379700736
```

**Output fields:** `source`, `offset_s`

### `pod-outcomes <build_id>`

Pod phase outcomes (Succeeded/Failed) for all pods in a build.

```
ci-query pod-outcomes 2031722177379700736
```

**Output fields:** `pod_name`, `pod_phase`, `completion_latency_s`

**Interpretation:**
- Many pods in Failed phase = infrastructure instability
- Cross-reference with `scheduling-latency` for infra diagnosis

### `scheduling-latency <build_id>`

Per-pod scheduling latency (time from creation to scheduled).

```
ci-query scheduling-latency 2031722177379700736
```

**Output fields:** `pod_name`, `latency_s`

**Interpretation:**
- >30s average = cluster under pressure
- >120s for any pod = likely caused downstream timeouts

### `error-logs <build_id> [limit]`

Error-level log entries (case-insensitive match, catches both `error` and `Error`).

```
ci-query error-logs 2031722177379700736
ci-query error-logs 2031722177379700736 100
```

**Output fields:** `_time`, `_msg`, `source`, `component`

### `warning-logs <build_id> [limit]`

Warning-level log entries.

```
ci-query warning-logs 2031722177379700736
```

**Output fields:** `_time`, `_msg`, `source`, `component`

### `all-logs <build_id> [limit]`

All log entries for context expansion around errors. Default limit: 200.

```
ci-query all-logs 2031722177379700736
ci-query all-logs 2031722177379700736 500
```

**Output fields:** `_time`, `_msg`, `level`, `source`, `component`

### `search-logs <build_id> <pattern> [limit]`

Search logs by regex pattern within a build.

```
ci-query search-logs 2031722177379700736 "timeout|deadline"
ci-query search-logs 2031722177379700736 "OOM|memory" 50
```

**Output fields:** `_time`, `_msg`, `level`, `source`

### `cross-pr-errors <pattern> [limit]`

Find an error pattern across all builds (deduplicated to one result per build). Useful for confirming systemic issues.

```
ci-query cross-pr-errors "quota exceeded"
ci-query cross-pr-errors "image pull" 100
```

**Output fields:** `build_id`, `pr_number`, `job_name`, `_msg`

**Interpretation:**
- Same error across many PRs/builds = systemic issue
- Error only in one PR's builds = PR-specific

### `flakiness <pr_number>`

Detect flaky outcomes (mix of pass and fail) for a PR. If flaky, outputs per-build details.

```
ci-query flakiness 3221
```

**Output:** First line is summary (`total_builds`, `passed`, `failed`, `success_rate_pct`, `flaky` boolean). If `flaky` is true, subsequent lines show each build's outcome.

**Interpretation:**
- `flaky: true` with same `pr_sha` = non-deterministic failure (infra or flaky test)
- `flaky: true` with different `pr_sha` values = author pushed a fix

### `step-errors <step_source> [window] [max_builds]`

Error logs across builds where a specific step failed. First line is a summary, followed by error log entries from a sample of failing builds.

```
ci-query step-errors steps.projectDirectoryImageBuildStep 7d
ci-query step-errors steps.clusterClaimStep 24h 10
```

**Output:** First line: `step`, `window`, `builds_with_failure`, `sampling`. Subsequent lines: `build_id`, `pr_number`, `_time`, `_msg`, `source`.

**Interpretation:**
- Look for recurring `_msg` patterns across builds to categorize failure modes
- Error-level logs are often lifecycle events ("event: X Finished"). For root-cause details, follow up with `search-logs <build_id> <pattern>` on specific builds to find diagnostic messages at info/warning level.
- If all sampled builds show the same error, it's systemic. Mixed errors suggest multiple causes.

### `build-latency [window]`

Scheduling latency aggregated by pod location: build cluster (shared CI infrastructure where images are compiled) vs ephemeral cluster (claimed test clusters). Answers "is the build cluster or the test cluster the bottleneck?"

```
ci-query build-latency
ci-query build-latency 7d
```

**Output:** Two JSON lines, one per location (`build_cluster`, `ephemeral_cluster`).

**Output fields:** `location`, `total_pods`, `pods_gt_60s`, `pods_gt_120s`, `avg_latency_s`, `max_latency_s`

**Interpretation:**
- High `build_cluster` latency with low `ephemeral_cluster` latency = shared CI build infrastructure under pressure (not actionable by the team)
- High `ephemeral_cluster` latency = claimed clusters are under-resourced or unhealthy
- `pods_gt_60s / total_pods` gives the fraction of builds paying a significant scheduling tax

### `error-impact <pattern> [window] [limit]`

Count unique builds and PRs affected by a log search pattern. Answers "how widespread is this specific error?" without showing full log messages.

```
ci-query error-impact 'serviceaccount.*not found' 7d
ci-query error-impact 'quota exceeded' 24h
```

**Output:** First line is summary (`pattern`, `window`, `unique_builds`, `unique_prs`). Subsequent lines break down affected builds per PR.

**Output fields (summary):** `pattern`, `window`, `unique_builds`, `unique_prs`
**Output fields (per-PR):** `pr_number`, `affected_builds`

**Interpretation:**
- High `unique_prs` relative to total active PRs = systemic issue
- Most builds concentrated in one PR = likely PR-specific
- Use `cross-pr-errors` to see the actual error messages once impact is confirmed

### `step-consistency <pr_number>`

Which steps fail across builds for a PR, and how consistently.

```
ci-query step-consistency 3221
```

**Output fields:** `pr_number`, `total_failures`, `builds_with_failures`, `steps` (array of `{source, count}`)

**Interpretation:**
- One step with count == builds_with_failures = deterministic failure in that step
- Multiple steps with low counts = inconsistent/flaky failures
- Same step failing across many builds = root cause is in that step

---

## JUnit

### `junit-steps <build_id> [limit]`

Step results with failure messages from JUnit XML. Shows each ci-operator step's pass/fail status with the exact failure message from `junit_operator.xml`.

```
ci-query junit-steps 2031710585065836544
```

**Output fields:** `step_name`, `status`, `duration_seconds`, `_msg` (failure message, only for failed steps)

**Interpretation:**
- Provides cleaner failure messages than ci-operator error logs
- `_msg` is the one-line failure reason from JUnit XML -- more concise than log messages
- Use alongside `step-failures` for a complete picture: `step-failures` gives timing, `junit-steps` gives the reason

### `junit-tests <build_id> [limit]`

Individual test case results from JUnit XML. Shows each e2e test case's pass/fail status with duration.

```
ci-query junit-tests 2030989391509327872
ci-query junit-tests 2030989391509327872 200
```

**Output fields:** `test_name`, `status`, `duration_seconds`, `test_variant`, `_msg` (failure message, only for failed tests)

**Interpretation:**
- `test_name` is the full Go test path (e.g., `TestOdhOperator/Operator_Manager_E2E_Tests/...`)
- `test_variant` identifies which e2e suite the test belongs to
- Failed tests include `_msg` with the assertion failure message
- Use to identify specific test failures within the e2e step

### `top-failing-tests [window] [limit]`

Most frequently failing test cases across all builds.

```
ci-query top-failing-tests         # 7d, top 10
ci-query top-failing-tests 30d 20  # 30 days, top 20
```

**Output fields:** `test_name`, `failure_count`

**Interpretation:**
- High failure count on a specific test = likely flaky or broken test
- Compare against `top-failing-steps` -- if the e2e step is the top failing step and specific tests dominate, those tests are the root cause
- Use `junit-tests` on specific builds to see the failure messages for top-failing tests

---

## Cluster Health / Hibernation

### `classify-sa [window]`

Classify "serviceaccount default not found" failures as cluster-wide (broken cluster) vs namespace-specific (possible race condition). Checks whether the SA error also appears in `openshift-must-gather-*` namespaces (created by ci-operator, separate from the test namespace).

```
ci-query classify-sa           # last 14 days
ci-query classify-sa 7d
```

**Output:** Per-build classification, then a summary line.

**Output fields (per-build):** `build_id`, `pr_number`, `must_gather_sa`, `install_sa`, `classification` (`cluster_wide` or `namespace_only`)
**Output fields (summary):** `total`, `cluster_wide`, `namespace_only`

**Interpretation:**
- `cluster_wide` = SA controller is non-functional on the entire cluster (hibernation resume issue, not a namespace race)
- `namespace_only` = SA might be slow to create in one namespace (genuine race condition, wait/retry may help)
- If most failures are `cluster_wide`, the problem is infrastructure (Hive pool / hibernation), not the test

### `claim-times [window] [broken_pattern]`

Compare cluster claim-to-ready times between builds matching a failure pattern vs healthy builds. Useful for diagnosing hibernation resume issues.

```
ci-query claim-times                                           # default: 14d, SA error pattern
ci-query claim-times 7d "serviceaccount.*default.*not found"   # explicit
```

**Output:** Two JSON lines, one per category (`broken`, `healthy`).

**Output fields:** `category`, `count`, `avg_s`, `min_s`, `max_s`

**Interpretation:**
- Broken clusters claiming *faster* than healthy ones = hibernation resume issue. The broken clusters were sitting in the pool (hibernated), resumed quickly but aren't fully initialized. Healthy clusters waited longer (pool exhaustion or slower resume), giving controllers time to sync.
- Similar claim times = the issue is not resume-related

### `failure-hours <pattern> [window]`

Group failures matching a log pattern by hour-of-day (UTC). Detects time-correlated patterns like hibernation resume failures clustering during off-peak hours.

```
ci-query failure-hours 'serviceaccount.*default.*not found' 14d
ci-query failure-hours 'quota exceeded' 7d
```

**Output fields:** `hour_utc`, `failure_count`

**Interpretation:**
- Failures clustering in off-peak hours (e.g., 00:00-06:00 UTC) = clusters sit idle longer before being claimed, more likely to have stale state after hibernation
- Even distribution = not time-correlated, likely a different root cause
