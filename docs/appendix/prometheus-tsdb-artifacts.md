---
name: test-cluster-metrics-artifacts
description: >
  Load when working with test cluster metrics artifacts (prometheus.tar
  or cluster-health-metrics.txt), extracting utilization metrics, or
  extending the test cluster metrics pipeline.
categories: [reference, metrics]
tags: [prometheus, tsdb, promtool, utilization, test-cluster, health-metrics]
related_docs:
  - docs/appendix/gcs-bucket-layout.md
  - docs/appendix/ci-operator-metrics.md
complexity: intermediate
---

# Test Cluster Metrics Artifacts

Two artifact types provide test cluster utilization metrics. Projects can use either, both, or neither.

| | prometheus.tar | cluster-health-metrics.txt |
|---|---|---|
| **Source** | OpenShift CI gather-extra step (automatic) | Project e2e test binary (project-implemented) |
| **Format** | Prometheus TSDB (requires `promtool` to extract) | Prometheus text exposition format (plain text) |
| **Location** | `artifacts/{step}/gather-extra/artifacts/metrics/prometheus.tar` | `artifacts/{step}/cluster-health-metrics.txt` |
| **Processing cost** | High (~10s per build, 3-5x WAL size in RAM) | Negligible (sub-millisecond text parsing) |
| **Coverage** | All cluster metrics at 15-30s scrape intervals | Project-selected metrics at configurable intervals |
| **Availability** | Any job with a test cluster and gather-extra | Only projects that implement a metrics collector |

Both sources produce metrics with the `ci_test_cluster_` prefix. A `metrics_source` label distinguishes them: `"tsdb"` for prometheus.tar, `"health"` for cluster-health-metrics.txt.

## Overlapping Metrics

Several metrics are available from both sources. When both exist for a build, both are ingested as separate time series (distinguished by `metrics_source`). Dashboard queries should filter by `metrics_source` to avoid double-counting in aggregations.

| Metric | prometheus.tar | cluster-health-metrics.txt |
|---|---|---|
| `machine_cpu_cores` | Yes | Yes |
| `kube_pod_container_resource_requests` | Yes | Yes |
| `kube_pod_container_resource_limits` | Yes | Yes |
| `container_memory_working_set_bytes` | Yes | Yes |
| `kube_node_role` | Yes (for role mapping) | Yes (for role mapping) |
| `cluster:cpu_usage_cores:sum` | Yes | No (recording rule) |
| `cluster:capacity_cpu_cores:sum` | Yes | No (recording rule) |
| `cluster:memory_usage_bytes:sum` | Yes | No (recording rule) |
| `cluster:capacity_memory_bytes:sum` | Yes | No (recording rule) |
| `kube_node_status_allocatable` | No | Yes |
| `kube_pod_status_phase` | No | Yes |
| `kube_deployment_status_replicas` | No | Yes |
| `cluster_healthy` | No | Yes |

---

## prometheus.tar (TSDB Dump)

The `gather-extra` step in CI creates `prometheus.tar` by tarring the Prometheus data directory from the ephemeral test cluster. This TSDB contains full time-series data for the entire test duration.

## Artifact Location

```
artifacts/{test-step-name}/gather-extra/artifacts/metrics/prometheus.tar
```

The test step name is derived from the job name by stripping the `pull-ci-{org}-{repo}-{branch}-` prefix. Two HA replicas exist (`prometheus.tar` for `prometheus-k8s-0`, `prometheus-k8s-1.tar` for the second replica). Only one is needed.

## TSDB Structure

These are short-lived clusters (~40 minutes), so the TSDB contains:
- **WAL (Write-Ahead Log)** -- recent samples not yet compacted
- **Head chunks** -- in-memory blocks

There are **no compacted blocks** since the cluster doesn't live long enough for Prometheus to compact. Despite this, `promtool tsdb dump` works correctly on the data.

## File Size

- **Tar archive:** ~83 MB per Prometheus pod
- **Extracted:** ~171 MB on disk
- **Time range:** ~40 minutes of data at 15-30s scrape intervals
- **Total unique metrics:** ~2,581 metric names

## Utilization Metrics

Recording rules are available as pre-computed gauges, avoiding the need for rate computation:

| Metric | Samples/build | Type | Purpose |
|---|---|---|---|
| `cluster:cpu_usage_cores:sum` | ~68 | gauge | Cluster-wide CPU usage in cores |
| `cluster:capacity_cpu_cores:sum` | ~136 | gauge | Cluster CPU capacity in cores |
| `cluster:memory_usage_bytes:sum` | ~71 | gauge | Cluster-wide memory usage |
| `cluster:capacity_memory_bytes:sum` | ~136 | gauge | Cluster memory capacity |
| `instance:node_memory_utilisation:ratio` | ~419 | gauge | Per-node memory utilization (0-1) |
| `node_memory_MemTotal_bytes` | ~850 | gauge | Per-node memory capacity |
| `machine_cpu_cores` | ~424 | gauge | Per-node CPU count |
| `kube_node_role` | ~420 | gauge | Node role mapping (value=1); used to enrich per-node metrics with `role` label |

Total: ~2,500 samples per build.

Per-node metrics are enriched at ingestion with a `role` label (`master` or `worker`) derived from `kube_node_role`. This enables filtering by node type without joining metrics at query time (e.g., `ci_test_cluster_machine_cpu_cores{role="worker"}`).

### Not Available

- `instance:node_cpu_utilisation:rate5m` -- 0 samples in these TSDBs. Per-node CPU utilization as a recording rule doesn't exist.
- Raw `node_cpu_seconds_total` (~40K counter samples) exists but requires rate computation, which `promtool tsdb dump` cannot do.

## Exploring Locally

Extract and inspect a TSDB:

```bash
# Download from GCS
curl -o prometheus.tar "https://storage.googleapis.com/test-platform-results/pr-logs/pull/.../.../artifacts/.../gather-extra/artifacts/metrics/prometheus.tar"

# Extract
mkdir prom-data && tar xf prometheus.tar -C prom-data

# List all metric names
promtool tsdb dump prom-data | sed 's/{.*//' | sort -u

# Dump specific metrics
promtool tsdb dump --match='{__name__=~"cluster:cpu_usage_cores:sum|cluster:capacity_cpu_cores:sum"}' prom-data

# Check time range and block info
promtool tsdb list prom-data
```

## Output Format

`promtool tsdb dump` outputs one line per sample:

```
{__name__="cluster:cpu_usage_cores:sum", prometheus="openshift-monitoring/k8s"} 3.14 1710000000000
```

Format: `{labels} value timestamp_ms`

The timestamp is in milliseconds. Labels follow Prometheus exposition format.

## Performance

Dumping targeted metrics (`--match` with the utilization metric set) takes ~10 seconds for a 171 MB TSDB. Full dump without filtering takes significantly longer.

---

## cluster-health-metrics.txt (Health Check Metrics)

A lightweight alternative to prometheus.tar. Projects implement a metrics collector (typically a background goroutine in their e2e test binary) that periodically writes kube-state-metrics-style data to `$ARTIFACT_DIR/cluster-health-metrics.txt` during test execution.

### Artifact Location

```
artifacts/{test-step-name}/cluster-health-metrics.txt
```

### Format

Standard Prometheus text exposition format:

```
# HELP cluster_healthy Overall cluster health status
# TYPE cluster_healthy gauge
cluster_healthy 1.0 1710849600000
kube_node_role{node="ip-10-0-1-1",role="worker"} 1.0 1710849600000
kube_node_status_allocatable{node="ip-10-0-1-1",resource="cpu",unit="core"} 4.0 1710849600000
```

Lines starting with `#` are comments (HELP/TYPE metadata). Timestamps are in milliseconds since epoch.

### Metrics

| Metric | Labels | Purpose |
|---|---|---|
| `kube_node_role` | `node`, `role` | Node role mapping; used for role enrichment (not emitted) |
| `kube_node_status_allocatable` | `node`, `resource`, `unit` | Per-node allocatable resources (cpu, memory) |
| `machine_cpu_cores` | `node` | Per-node CPU core count |
| `kube_pod_status_phase` | `namespace`, `pod`, `phase` | Pod lifecycle phase (Pending, Running, etc.) |
| `kube_pod_container_resource_requests` | `namespace`, `pod`, `container`, `resource`, `unit` | Container resource requests |
| `kube_pod_container_resource_limits` | `namespace`, `pod`, `container`, `resource`, `unit` | Container resource limits |
| `container_memory_working_set_bytes` | `namespace`, `pod`, `container` | Container memory usage |
| `kube_deployment_status_replicas` | `namespace`, `deployment` | Desired replica count |
| `kube_deployment_status_replicas_available` | `namespace`, `deployment` | Available replicas |
| `kube_deployment_status_replicas_ready` | `namespace`, `deployment` | Ready replicas |
| `kube_deployment_status_replicas_updated` | `namespace`, `deployment` | Updated replicas |
| `cluster_healthy` | (none) | Overall cluster health gauge (1.0 = healthy, 0.0 = unhealthy) |

### Implementing a Collector

Projects that want to emit health metrics need to:

1. Create a background goroutine (or periodic task) that collects metrics from the Kubernetes API
2. Write metrics in Prometheus text exposition format to `$ARTIFACT_DIR/cluster-health-metrics.txt`
3. Use metric names from the table above for automatic ingestion

See [opendatahub-io/opendatahub-operator#3316](https://github.com/opendatahub-io/opendatahub-operator/pull/3316) for a reference implementation.

### Advantages Over prometheus.tar

- **No infrastructure dependency**: doesn't require `gather-extra` or Prometheus TSDB access
- **Negligible processing cost**: plain text parsing vs promtool WAL replay
- **Project-specific signals**: can include custom health metrics (e.g., `cluster_healthy`)
- **Continuous sampling**: collector runs throughout the test, not just at gather time
