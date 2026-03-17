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
- Use `search-logs` on affected builds to see the actual error messages

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
- `test_name` is the full Go test path (e.g., `TestSuite/SubTest/...`)
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

## Regression Detection

### `last-success <job_pattern>`

Find when a job last passed and the first failure after it. Useful for pinpointing when a regression started.

```
ci-query last-success e2e-agnostic
ci-query last-success e2e-hypershift
```

**Output fields:** `last_success` (build_id), `pr_number`, `pr_sha`, `job_name`, `first_failure_after` (build_id), `first_failure_pr`, `total_builds`, `total_successes`, `failures_since`

**Interpretation:**
- `failures_since` > 0 with `total_successes` > 0 = regression occurred, investigate what changed between `last_success` and `first_failure_after`
- `total_successes` == 0 = job has never passed in the data window
- Compare timestamps of the transition builds against operator/manifest commits to find the culprit

### `test-failures [job_pattern] [test_pattern] [limit]`

Find failing test cases filtered by job name and/or test name. Useful for scoping test failures to a specific platform or component.

```
ci-query test-failures e2e-agnostic monitoring
ci-query test-failures e2e "" 20
ci-query test-failures "" alertmanager    # all jobs, alertmanager tests
```

**Output fields:** `build_id`, `pr_number`, `test_name`, `msg` (truncated failure message), `time`

**Interpretation:**
- Same test failing across many builds/PRs = systemic (broken test or broken dependency)
- Failures only in one job pattern (e.g., `e2e-agnostic` but not `e2e-aws-ovn`) = platform-specific issue (check manifest/config differences)
- `msg` contains the assertion failure -- look for ConfigMap errors, timeout messages, or crash indicators

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

---

## Cluster Provisioning

### `lease-health [window]`

Lease quota utilization and IPI install/deprovision times. For repos that provision clusters via IPI (installer-provisioned infrastructure) rather than claiming from a Hive pool.

```
ci-query lease-health         # 7d
ci-query lease-health 30d
```

**Output:** Summary line with lease quota and IPI timing, then per-region breakdown.

**Output fields (summary):** `window`, `builds_with_leases`, `lease_quota_total`, `lease_quota_avg_remaining`, `lease_quota_min_remaining`, `lease_quota_utilization_pct`, `ipi_install_avg_s`, `ipi_install_max_s`, `ipi_deprovision_avg_s`
**Output fields (per-region):** `region`, `builds`

**Interpretation:**
- `lease_quota_utilization_pct` > 60% = quota pressure, builds may queue for leases
- `lease_quota_min_remaining` == 0 = quota was fully exhausted at some point
- `ipi_install_avg_s` > 3600 = IPI installs are unusually slow (typically 30-50 min)
- Per-region breakdown helps identify if one region is more loaded than others

### `pool-health [window]`

Cluster pool claim wait times and idle time statistics per pool. Answers "is the pool the right size?"

```
ci-query pool-health         # 7d
ci-query pool-health 30d
```

**Output:** Per-pool claim wait stats, then per-pool idle time.

**Output fields (wait):** `cluster_pool`, `builds`, `avg_wait_s`, `p90_wait_s`, `max_wait_s`
**Output fields (idle):** `cluster_pool`, `avg_idle_s`

**Interpretation:**
- High `avg_wait_s` (>300s) = pool too small, builds are queuing for clusters
- Low `avg_idle_s` with high `avg_wait_s` = pool is saturated
- High `avg_idle_s` (>3600s) with low `avg_wait_s` = pool is over-provisioned
- Compare across pools to identify which pools need resizing

### `pool-builds <build_id>`

Cluster pool details for a specific build. Shows which pool was used, claim wait time, install duration, and idle time.

```
ci-query pool-builds 2032428735797399552
```

**Output fields:** `build_id`, `cluster_pool`, `ocp_version`, `cloud_region`, `power_state`, `claim_wait_s`, `install_duration_s`, `idle_s`

**Interpretation:**
- `claim_wait_s` > 300s for a single build = this build was affected by pool contention
- `power_state` = "Running" vs "Hibernating" helps diagnose hibernation resume issues
- `idle_s` tells how long the cluster sat unused before this build claimed it
- Use alongside `search-logs` to correlate pool issues with build failures

---

## Resource Utilization

### `test-cluster-utilization [window]`

Average and max CPU/memory utilization from the Hive-claimed test clusters where e2e tests run (worker nodes only). This is actual usage data extracted from the cluster's Prometheus TSDB, not resource requests.

```
ci-query test-cluster-utilization         # 7d
ci-query test-cluster-utilization 30d
```

**Output fields:** `window`, `builds_with_data`, `worker_cpu_usage_cores`, `worker_cpu_capacity_cores`, `worker_cpu_util_pct`, `worker_max_cpu_cores`, `worker_memory_usage_gib`, `worker_memory_capacity_gib`, `worker_memory_util_pct`, `worker_max_memory_gib`, `note`

**Interpretation:**
- Reports **worker nodes only** (excludes masters). Master utilization is available per-build via `test-cluster-build`.
- `worker_cpu_util_pct` < 30% = workers are over-provisioned (right-sizing opportunity)
- `worker_cpu_util_pct` > 80% = tests may be resource-constrained
- `worker_memory_usage_gib` includes page cache and buffers (reclaimable under pressure). For actual working set, use `test-cluster-build` per-node breakdown.
- Memory is reported in GiB (binary, 1024^3 bytes), matching how Kubernetes reports capacity.

### `test-cluster-build <build_id>`

Utilization details for a specific build's test cluster. Shows CPU and memory usage vs capacity split by role (master/worker), plus per-node memory working set.

```
ci-query test-cluster-build 2031880686163464192
```

**Output:** Per-role summary lines (master, worker), followed by per-node working set details.

**Output fields (per-role):** `build_id`, `role`, `instance_type`, `cpu_capacity_cores`, `cpu_usage_cores`, `cpu_util_pct`, `memory_capacity_gib`, `memory_usage_gib`, `memory_util_pct`
**Output fields (per-node):** `node`, `role`, `working_set_pct`, `working_set_gib`, `total_gib`

**Interpretation:**
- Per-role summary shows `memory_usage_gib` which includes cache/buffers. Per-node `working_set_gib` excludes reclaimable memory and reflects actual application memory consumption.
- Worker `working_set_pct` is the key metric for VM right-sizing. If max working set per worker node is well below node capacity, smaller VMs can be used.
- Master memory utilization tends to be high (etcd, API server, controller-manager). If master `working_set_pct` exceeds 80%, masters cannot be downsized.
- The `role` label is set at ingestion time from `kube_node_role` in the test cluster's Prometheus TSDB.

### `node-utilization [window] [limit]`

Build cluster node CPU and memory utilization as a percentage of capacity. Shows the shared CI infrastructure nodes where ci-operator orchestrates builds (not the Hive-claimed test clusters).

```
ci-query node-utilization         # 7d, top 20 nodes
ci-query node-utilization 30d 10
```

**Output:** First block: per-node CPU utilization. Second block: per-node memory utilization.

**Output fields (CPU):** `node`, `machine_type`, `cpu_util_pct`
**Output fields (memory):** `node`, `machine_type`, `mem_util_pct`

**Interpretation:**
- These are build cluster nodes only (e.g., `build04-*-ci-longtests-worker-*`), not test cluster nodes
- High utilization on build cluster nodes may explain pod scheduling latency (see `build-latency`)
- Useful for CI infrastructure cost analysis, not for cluster pool right-sizing

---

## Config Tracking

### `config-versions [window]`

List distinct config hashes with build counts and date ranges. Shows when CI config transitions happened.

```
ci-query config-versions           # 90d
ci-query config-versions 30d
```

**Output fields:** `config_hash`, `build_count`, `first_seen_ts`, `last_seen_ts`

**Interpretation:**
- Multiple hashes = config changed during the window
- A hash with few builds at the end of the range = recent config change
- Use `config-diff` to compare what changed between hashes

### `config-diff <hash1> <hash2>`

Compare step graph contents between two config hashes. Shows which steps were added, removed, or changed.

```
ci-query config-diff abc123def456 789abc012345
```

**Output:** One line per changed step, then a summary.

**Output fields (per-step):** `step`, `change` (`added`/`removed`/`modified`), `description`, `dependencies`
**Output fields (summary):** `hash1`, `hash2`, `added`, `removed`, `modified`, `unchanged`

**Interpretation:**
- Added/removed steps = structural config change (new test steps, removed stages)
- Modified steps = description or dependency changes
- Use alongside `top-failing-steps` to correlate config changes with failure patterns
