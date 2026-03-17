---
name: openshift-ci-infrastructure
description: >
  Load when investigating CI failures, interpreting CI metrics/logs in context,
  understanding job lifecycle phases, cluster provisioning behavior, resource
  contention patterns, or navigating external CI tooling and configuration.
categories: [reference, infrastructure]
tags: [openshift-ci, prow, ci-operator, cluster-pools, hive, boskos]
related_docs:
  - docs/appendix/ci-operator-metrics.md
  - docs/appendix/gcs-bucket-layout.md
complexity: intermediate
---

# OpenShift CI Infrastructure

Context for interpreting CI metrics and logs within the OpenShift CI platform. This document covers the structural elements of the CI system that produce the data we observe -- job lifecycle, cluster provisioning, resource management, and where to look for more information.

Source: distilled from OpenShift CI documentation.

## CI Platform Overview

CI runs on **OpenShift CI**, which is two components:

- **Prow**: Kubernetes-native CI scheduler. Handles GitHub webhook events, job scheduling, ChatOps (`/test`, `/retest`), and result reporting. Prow decides *when* and *whether* to run a job.
- **ci-operator**: OpenShift CI's job orchestration tool. Reads YAML config to build images, provision/claim clusters, and run test steps. ci-operator decides *how* a job runs.

Configuration lives in the [openshift/release](https://github.com/openshift/release) repository:

| Path | Contents |
|------|----------|
| `ci-operator/config/{org}/{repo}/` | ci-operator config per branch (images, tests, workflows) |
| `ci-operator/jobs/{org}/{repo}/` | Generated Prow job YAML (presubmits, postsubmits) |
| `clusters/hosted-mgmt/hive/pools/{team}/` | Hive cluster pool manifests |
| `core-services/prow/02_config/_config.yaml` | Global Prow config |

## Job Lifecycle

A PR event triggers Prow presubmit jobs. Each E2E job follows this phase sequence:

```
Build --> Claim/Lease --> Pre (cluster setup) --> Test --> Post (gather + cleanup)
```

### Phase breakdown

| Phase | What happens | Metrics produced |
|-------|-------------|-----------------|
| **Build** | Compile source, build container images, create operator bundles | `ci_images_*` metrics |
| **Claim/Lease** | Acquire a cluster (from pool or via IPI provisioning) | `ci_leases_*` metrics, `clusterClaimStep` in step metrics |
| **Pre** | RBAC setup, operator installation, node readiness checks | Step metrics for setup steps |
| **Test** | Run the actual test suite (e.g., `make e2e-test`) | `ci_step_*`, `ci_pods_*`, `ci_test_platform_insights_*` |
| **Post** | Gather artifacts (`must-gather`, audit logs), deprovision cluster | Step metrics for gather/cleanup steps |

**Key insight for investigation**: failures in Build or Claim phases mean tests never ran. A `clusterClaimStep` failure is infrastructure, not a test problem. Check `ci_step_duration_seconds` with early `ci_step_relative_start_seconds` values to spot builds that died before reaching the test phase.

### What triggers jobs

| Trigger | Behavior |
|---------|----------|
| PR push (new commit) | Runs all `always_run: true` jobs; runs conditional jobs if file patterns match. **Cancels** running jobs for the previous commit on the same PR. |
| `/test <job>` | Triggers a specific job unconditionally |
| `/retest` | Re-triggers only failed/completed jobs (not currently running ones) |
| `/test all` | Triggers all automatic jobs conditionally |

**Superseding behavior**: When a new commit is pushed to a PR, Prow aborts running presubmit jobs for the old commit. This means a single PR should hold at most one instance of each job at any time. `/retest` on the same commit only triggers jobs that have already completed (failed), so it also doesn't create duplicates.

## Cluster Provisioning Models

How a job gets a cluster affects what failures look like and how long phases take.

### Cluster Claim (Hive pools)

Pre-provisioned clusters managed by the Hive operator. Jobs claim a cluster from a pool and the cluster is destroyed after use (Hive provisions a replacement).

```yaml
cluster_claim:
  architecture: amd64
  cloud: aws
  owner: my-team
  product: ocp
  timeout: 2h0m0s      # max wait for a cluster
  version: "4.19"
```

- **Claim time**: instant (running cluster) to 3-6 min (hibernating)
- **Pool exhaustion**: if all clusters are claimed, jobs wait up to `timeout` then fail as a `clusterClaimStep` failure
- **Cluster lifecycle**: claimed clusters are never returned -- Hive destroys them and provisions fresh replacements
- **Pool replenishment**: takes 40-60 minutes per cluster (full IPI install)
- **Workflow**: `generic-claim`

**Investigation implication**: high `clusterClaimStep` failure rates indicate pool exhaustion. Cross-reference with the number of concurrent active PRs -- each PR with running E2E jobs holds ~2 clusters (one per E2E variant). Check time-of-day patterns; shared infrastructure contention causes measurable variance.

### Cluster Profile (IPI provisioning)

Legacy model. Each job provisions a fresh cluster via Installer-Provisioned Infrastructure. Adds 25-40 minutes of overhead before any test runs. Uses Boskos for cloud credential leasing.

- **Workflow**: `optional-operators-ci-operator-sdk-{cloud}`
- **Visible as**: `ipi-install-install` step in step metrics (the long pre-phase step)

### HyperShift (hosted control plane)

Hosted control plane on a management cluster. Faster provisioning (~10-15 min) but different cluster topology that can cause test behavioral differences.

## Resource Contention

### Shared infrastructure pools

Multiple repositories within an organization can share the same cloud credential pools (Boskos) or cluster pools (Hive). This means CI load from one repo affects others sharing the same pool.

**Observable pattern**: success rates vary by time of day due to shared infrastructure contention. Historical data showed up to 21 percentage points of variance (best at 5-7 AM UTC, worst at 9 PM UTC). When investigating low success rates, check whether the pattern is time-correlated before assuming a code or test problem.

### Prow concurrency controls

| Mechanism | Scope | Purpose |
|-----------|-------|---------|
| `max_concurrency` (per job) | All PRs, single job type | Limits concurrent instances of one specific job across all PRs |
| `cluster_claim.timeout` | Per job instance | How long to wait for a pool cluster before failing |
| Step `timeout` | Per step within a job | Kills a step after the specified duration |
| `skip_if_only_changed` | Per job | Skips the job entirely when only matching files changed |

**What Prow does NOT provide**: per-PR concurrency limits, debounce/backoff on job triggering, pool-aware admission control, or rate limiting on `/retest` commands.

## Timeout Architecture

Timeouts form a hierarchy. When investigating timeout-related failures, identify which layer timed out:

| Layer | Default | Configurable via |
|-------|---------|-----------------|
| Prow global job timeout | 24h | Prow config or job-level `timeout` |
| ci-operator step timeout | 2h | Step-level `timeout` field |
| Step grace period | 15s | Step-level `grace_period` field |
| Go test timeout | none | `-timeout` flag in test commands |
| Application-level waits | varies | Test code (e.g., Gomega `Eventually()` timeouts) |

**Investigation implication**: a test that reports "context deadline exceeded" at exactly 60 minutes hit the Go test timeout, not a step timeout. A step killed at exactly 1h30m hit the step timeout. A job that ran for hours without producing results likely has no effective timeout set.

## External Observability Tools

When local metrics/logs aren't sufficient, these external tools provide additional context:

| Tool | URL | What it provides |
|------|-----|-----------------|
| **Prow UI** | `prow.ci.openshift.org` | Job pages, Spyglass artifact viewer, raw logs |
| **BigQuery** | `openshift-gce-devel.ci_analysis_us` | Historical job and test case results (rolling ~2 month window) |
| **Sippy** | `sippy.dptools.openshift.org` | Flake rate tracking, component health, PR risk analysis |
| **Component Readiness** | via Sippy | Statistical regression detection (requires ci-test-mapping registration) |
| **search.ci** | `search.dptools.openshift.org` | Full-text search across JUnit failures and build logs |
| **Looker Studio** | [Dashboard link](https://lookerstudio.google.com/u/0/reporting/3d305775-c217-4c12-9331-9768276c3211/page/p_52w2d8a4uc) | Live prowjob analysis (BigQuery-backed) |

### Prow job URL pattern

```
https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/{org}_{repo}/{pr_number}/{job_name}/{build_id}
```

This links directly to the Spyglass view for a specific build, showing step logs, JUnit results, and artifacts.

## Mapping Scraped Data to CI Phases

The scraper ingests `ci-operator-metrics.json` and `ci-operator.log` from each build's GCS artifacts. Understanding which CI phase produced which metrics helps during investigation:

| Metric pattern | CI phase | What to look for |
|---------------|----------|-----------------|
| `ci_step_*` with early `relative_start_seconds` | Build / Claim | Setup failures (source checkout, image build, cluster claim) |
| `ci_step_*` with `source` containing "install" | Pre | Operator installation failures |
| `ci_step_*` with `source` containing "e2e" | Test | Actual test execution |
| `ci_step_*` with `source` containing "gather" | Post | Artifact collection (secondary -- look for earlier failures) |
| `ci_pod_scheduling_latency_seconds` | Any | Cluster pressure during the job |
| `ci_leases_*` | Claim | Resource leasing behavior |

### Log context

ci-operator logs (`ci-operator.log`) contain events from the orchestrator, not from the tests themselves. The `source` field indicates which step or component emitted the log. The `component` field distinguishes between `ci-operator` core events and `event` entries (step lifecycle events like "Started", "Finished").

## Communication Channels

| Channel | Purpose |
|---------|---------|
| `#forum-ocp-testplatform` | TRT engagement for cluster pool setup and OpenShift CI infrastructure support |

## References

| Resource | URL |
|----------|-----|
| OpenShift CI docs | https://docs.ci.openshift.org/ |
| Prow docs | https://docs.prow.k8s.io/ |
| Prow timeout handling | https://docs.ci.openshift.org/docs/architecture/timeouts/ |
| ci-operator source | https://github.com/openshift/ci-tools |
| Prow job configuration | https://docs.prow.k8s.io/docs/jobs/ |
| Tide (merge automation) | https://docs.prow.k8s.io/docs/components/core/tide/ |
