---
name: prometheus-tsdb-artifacts
description: >
  Load when working with Prometheus TSDB artifacts from test clusters,
  extracting utilization metrics from prometheus.tar, or extending
  the test cluster metrics pipeline.
categories: [reference, metrics]
tags: [prometheus, tsdb, promtool, utilization, test-cluster]
related_docs:
  - docs/appendix/gcs-bucket-layout.md
  - docs/appendix/ci-operator-metrics.md
complexity: intermediate
---

# Prometheus TSDB Artifacts

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
