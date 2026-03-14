# Cluster Pool Right-Sizing Investigation

## Status: Paused -- needs utilization metrics

## Context

PR [openshift/release#76260](https://github.com/openshift/release/pull/76260) started as a fix for "serviceaccount default not found" failures in opendatahub E2E jobs. Investigation revealed the root cause is a **hibernation resume race condition**: Hive declares clusters "ready" when the API server responds, but kube-controller-manager needs several more minutes to sync informer caches after waking from hibernation. The SA controller doesn't process events during this window.

While investigating, we found the opendatahub cluster pool is significantly over-provisioned compared to every other team. This document captures the sizing analysis for a follow-up optimization.

## Current Config vs Other Teams

| Setting | opendatahub | serverless | OSSM |
|---|---|---|---|
| Masters | 3 (default) | 3 (default) | 1 x m5.xlarge |
| Workers | **3 x m5.2xlarge** | 3 x m5.xlarge | 1 x m5.xlarge |
| Total vCPU | 36 | 24 | 8 |
| Total RAM | 144 GiB | 96 GiB | 32 GiB |
| Pool size | 12 | 0 (scale-from-zero) | 1 |
| EBS per pool | ~8,640 GB | 0 | 240 GB |

The m5.2xlarge workers were chosen "to match our current n2-standard-8 GCP instances" from the legacy IPI workflow. The sizing was carried over without re-evaluation when migrating to Hive pools.

## Resource Requests Analysis (from pods.json)

Source: `gather-extra/artifacts/pods.json` from build `2032461694420127744` (PR 3232, rhoai-e2e variant, operator fully deployed, test running).

### By layer

| Layer | CPU Requests | Memory Requests | Notes |
|---|---|---|---|
| OCP platform (fixed) | 9.4 cores | 37.3 GiB | 236 pods, from actual pods.json |
| ODH operator (3 replicas) | 300m | 2,340 MiB | 100m/780Mi per replica |
| Pre-requisites (cert-mgr, sail, lws) | ~600m | ~2.3 GiB | Includes istiod estimate |
| DSC components (all enabled) | ~3.0 cores | ~5.1 GiB | Controllers only, no user workloads |
| DSP sub-components (if DSPA CR) | ~1.4 cores | ~3.2 GiB | Only if pipeline stack is tested |
| kuadrant (dependency) | 610m | 664 MiB | From actual pods.json |
| **Worst-case peak** | **~15.3 cores** | **~51 GiB** | |

Source: pods.json from build `2032461694420127744` for platform numbers; component manifests from opendatahub-operator repo for ODH stack numbers.

### Component detail (from operator source manifests)

| Component | CPU Req | Mem Req | Replicas |
|---|---|---|---|
| ODH Controller Manager | 100m | 780Mi | 3 |
| Dashboard + kube-rbac-proxy | 1000m | 2Gi | 2 |
| KServe Controller | 100m | 200Mi | 1 |
| ODH Model Controller | 10m | 64Mi | 1 |
| KubeRay Operator | 100m | 512Mi | 1 |
| TrustyAI Operator | 10m | 64Mi | 1 |
| Model Registry Operator | 100m | 256Mi | 1 |
| DSP Operator | 200m | 400Mi | 1 |
| Feast Operator | 10m | 64Mi | 1 |
| LlamaStack Operator | 10m | 256Mi | 1 |
| MLflow Operator | 200m | 400Mi | 1 |
| Spark Operator (ctrl + webhook) | 200m | 256Mi | 2 |
| Kueue Controller Manager | 500m | 512Mi | 1 |
| ODH Notebook Controller | 500m | 256Mi | 1 |

Note: Training Operator and Trainer have **no resource requests** defined in their manifests. E2e tests enable components in groups (not all at once), but do not disable between groups, so most components accumulate as the test progresses.

### Capacity comparison

| Config | vCPU | RAM | Peak CPU% | Peak Mem% |
|---|---|---|---|---|
| **Current**: 3m + 3w x m5.2xlarge | 36 | 144 GiB | **42%** | **35%** |
| 3m + 3w x m5.xlarge | 24 | 96 GiB | 64% | 53% |
| 3m + 2w x m5.xlarge | 20 | 80 GiB | 77% | 64% |

### E2E test component groups (run sequentially)

The tests enable components in groups, not all at once:

- **group_1** (12 components): dashboard, datasciencepipelines, feastoperator, kserve, llamastackoperator, mlflowoperator, modelregistry, ray, sparkoperator, trainer, trainingoperator, workbenches
- **group_2** (2): kueue, modelcontroller
- **group_3** (1): trustyai
- **group_4** (1): modelsasservice

Peak resource usage occurs during group_1 when the most components are active simultaneously.

## What's Missing: Actual Utilization Metrics

The analysis above is based on resource **requests** (what pods ask the scheduler to reserve). This is worst-case reservation, not actual usage. Pods often use far less than requested.

**We have no actual utilization data from claimed clusters.** The CI pipeline does not capture node-level resource usage. What's available in GCS:

| Artifact | Has utilization? | Notes |
|---|---|---|
| `ci-operator-metrics.json` | No | Only CI pipeline metrics (step durations, pod scheduling on build cluster) |
| `gather-extra/nodes.json` | Partial | Has `allocatable`/`capacity` but NOT current usage |
| `gather-extra/pods.json` | Partial | Has resource requests/limits but NOT actual consumption |
| `gather-must-gather/` | Maybe | Contains Prometheus TSDB tarball but not easily queryable |

### Proposed: Node Utilization Capture

Add a mechanism to capture actual node resource utilization during the test, similar to how `ci-operator-metrics.json` captures pipeline metrics. Two approaches:

#### Option A: Periodic `oc adm top nodes` capture

Add a sidecar or background process in the test step that periodically runs `oc adm top nodes --no-headers` and writes timestamped results to an artifact file:

```bash
# Run in background during e2e test
while true; do
  echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",$(oc adm top nodes --no-headers 2>/dev/null | awk '{printf "\"node\":\"%s\",\"cpu_cores\":\"%s\",\"cpu_pct\":\"%s\",\"mem_bytes\":\"%s\",\"mem_pct\":\"%s\"", $1,$2,$3,$4,$5}')}"
  sleep 30
done > "${ARTIFACT_DIR}/node-utilization.json" &
```

This produces a JSON-lines file that our scraper could ingest into VictoriaMetrics, giving us time-series utilization data across all builds.

#### Option B: Point-in-time capture

Simpler: capture `oc adm top nodes` and `oc adm top pods -A --sum` output as part of the gather-extra step. Gives a snapshot rather than a time series but much simpler to implement.

Either approach would let us answer "what do the clusters actually use?" rather than relying on requests.

## Preliminary Recommendation (pending utilization data)

**Safe change** (high confidence): Switch workers from m5.2xlarge to m5.xlarge with 3 replicas. This is what every other team uses, and even with worst-case request-based estimates we'd have 33% CPU headroom and 55% memory headroom.

**Possible further optimization** (needs utilization data): Reduce to 2 workers. The estimated peak is 80% CPU which is workable but tight. Need actual utilization measurements to validate.

**Not recommended without testing**: Reducing masters below 3 (like OSSM's single-master setup). Single master means no etcd HA, different failure characteristics, and may affect test behavior for components that expect HA topology.

## Next Steps

1. Add node utilization capture to the e2e test step (Option A or B above)
2. Collect data for a week across successful and failed builds
3. Analyze actual peak CPU and memory utilization per node
4. Make sizing decision based on real data
5. If changing worker size: update `clusters/hosted-mgmt/hive/pools/opendatahub/install-config-aws_secret.yaml`, submit as a separate PR from the hibernation fixes
