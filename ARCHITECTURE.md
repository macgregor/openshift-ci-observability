---
description: System architecture and data flow for OpenShift CI Observability
---

# Architecture

## System Diagram

```mermaid
graph TD
    GCS["Google Cloud Storage\n(GCS Buckets)"]
    Cache["Local GCS Cache\n(podman volume)"]
    Scraper["Scraper\nscraper-watch · scraper-backfill"]
    VM["VictoriaMetrics"]
    VL["VictoriaLogs"]
    Grafana["Grafana"]

    GCS -- "GCS XML API" --> Cache
    Cache -- "cached artifacts" --> Scraper
    Scraper -- "Remote-write\n(Prometheus)" --> VM
    Scraper -- "JSON lines" --> VL
    VM -- "PromQL" --> Grafana
    VL -- "LogsQL" --> Grafana
```

## Data Flow

### Discovery

The scraper discovers CI builds for a specific repo via the GCS XML API:

1. **PR Enumeration**: List prefixes under `gs://test-platform-results/pr-logs/pull/{org}_{repo}/` (derived from the `--repo` flag)
2. **Job Enumeration**: Within each PR, list job directories
3. **Build Enumeration**: Within each job, list build directories
4. **Date Filtering**: Read `started.json` from each build to filter by the `--window` range
5. **Artifact Processing**: For each qualifying build, run registered pipelines (metrics, logs) against the build's artifacts

### Metric Conversion

Metrics are converted to Prometheus text exposition format:

- **Generic Extraction**: All numeric fields are automatically extracted as metrics
- **Known Transforms**:
  - Nanosecond values converted to seconds
  - Kubernetes quantities (e.g., `1Gi`, `500m`) parsed to numeric values
- **Canonical Aliases**: Common metric names are normalized (e.g., `duration_ns` -> `duration_seconds`)

Metrics are pushed to VictoriaMetrics via remote-write protocol.

### Log Ingestion

The scraper fetches `ci-operator.log` from each build's artifact directory. This file contains structured JSON lines emitted by the ci-operator process during execution. Each line is parsed independently:

- **Time and message** are extracted as `_time` and `_msg` fields
- **Scalar fields** (level, component, and other string/numeric/bool values) are flattened into the log entry
- **Job labels** from the build's `ci-operator-metrics.json` are merged into each log entry, providing consistent queryability across metrics and logs. Labels take precedence over log fields with the same name.

Logs are pushed to VictoriaLogs as JSON lines with `_stream_fields=job_name,build_id`.

## Scraper Internals

### Pipeline Architecture

The scraper is built around a pipeline architecture. Each pipeline processes a specific artifact type and emits records to a sink:

| Pipeline | Artifact | Output | Sink |
|---|---|---|---|
| MetricsPipeline | `ci-operator-metrics.json` | Prometheus metrics | VictoriaMetrics |
| LogPipeline | `ci-operator.log` | JSON log lines | VictoriaLogs |
| JunitPipeline | `junit_operator.xml`, `junit_*.xml` | Metrics + failure logs | Both |
| ClusterPoolPipeline | `clusterClaim.json`, `clusterDeployment.json` | Pool lifecycle metrics | VictoriaMetrics |
| TestClusterMetricsPipeline | `prometheus.tar` (TSDB dump) | Cluster utilization metrics (per-node metrics enriched with master/worker `role` label) | VictoriaMetrics |

Adding a new scrape target means writing a new Pipeline class and registering it in `__main__.py`. Pipelines are independent -- each receives a `BuildContext` and decides what to fetch and emit. Each pipeline declares a `version` string composed of `SHARED_VERSION` (for cross-cutting changes) and a pipeline-specific suffix. Bumping a pipeline's suffix invalidates only that pipeline; bumping `SHARED_VERSION` invalidates all pipelines.

### Core Entities

- **BuildContext**: Per-build facade providing lazy artifact fetching (with in-memory caching) and job label extraction. Pipelines access build artifacts and labels through the context without needing to know about GCS paths.

- **Sink**: Handles batching and HTTP transport to backend services. `VictoriaMetricsSink` pushes Prometheus text format, `VictoriaLogsSink` pushes JSON lines.

- **GCSClient**: HTTP client for the GCS XML API with optional filesystem cache. Listing operations (PR/job/build enumeration) are never cached; object fetches are cached on disk so re-ingestion after a DB wipe reads from local disk.

- **Scraper**: Orchestrator that discovers builds, filters by date range, skips already-processed builds, and runs all pipelines via a thread pool.

### Concurrency

The scraper uses a `ThreadPoolExecutor` shared by both discovery and build processing. PR listing runs first (single GCS call), then per-PR discovery tasks (list jobs, list builds) and build processing tasks compete for the pool. As each discovery completes, its builds are submitted immediately and interleave with remaining discoveries. The main thread drives the work loop, submitting new discoveries to keep the pipeline fed as earlier ones complete. The TestClusterMetricsPipeline runs `promtool` WAL replay in a separate 4-worker pool to avoid starving the main pool with CPU-intensive work.

### GCS Artifact Cache

GCS artifacts are immutable once written, so the scraper caches fetched objects to a local directory (a podman volume shared between watch and backfill services). Cache entries use the GCS path as the filesystem path, mirroring the bucket layout. 404 responses are cached as `.miss` marker files to avoid re-probing missing artifacts.

The TestClusterMetricsPipeline also caches processed output as `.metrics` sibling files next to the raw `prometheus.tar`. Each `.metrics` file contains a version header and the final Prometheus text format ready for pushing. On read, the version is compared against the pipeline's current version -- a mismatch means stale, and the file is reprocessed. This avoids redundant `promtool` WAL replay, which is the most expensive operation in the scraper.

`make wipe-db` clears the database but preserves the cache, enabling fast re-ingestion after scrape logic changes. `make wipe-all` clears both.

## Portability Design

The scraper outputs data in standard formats:

- **Metrics**: Prometheus text exposition format
- **Logs**: JSON lines

This design enables migration to hosted observability platforms by changing the ingestion URL and adding authentication, without modifying the scraper's output format.

## Deduplication Strategy

### Metrics

VictoriaMetrics handles deduplication via `-dedup.minScrapeInterval=1ms` flag, which deduplicates samples with identical timestamps and labels. Changed or removed metrics age out via retention.

### Logs

Log entries include a `pipeline` field (`"logs"` or `"junit"`). When a pipeline's version changes and a build is reprocessed, the scraper deletes old log entries for that build_id and pipeline via the VictoriaLogs delete API before re-pushing, preventing duplicates.

## Operational Modes

The scraper runs as two compose services:

### scraper-watch (Continuous Polling)

- Continuously polls GCS for new builds
- Runs indefinitely
- Processes builds as they appear
- Intended for real-time monitoring

### scraper-backfill (One-Time)

- Processes builds within the configured `--window`
- Exits when complete
- Used for historical data ingestion
- Activated via compose profiles: `podman-compose --profile backfill up -d`

## State Management

Build processing state is stored in VictoriaMetrics itself via per-pipeline sentinel metrics. For each pipeline+build combination, the scraper pushes:

```
ci_pipeline_scraped{build_id="123", pipeline="metrics", pipeline_v="1.1"} 1
```

At the start of each scrape cycle, the scraper queries for known build_ids per pipeline at the current version. A build is skipped only if ALL pipelines have processed it at their current version. If any pipeline's version has changed, only that pipeline reprocesses -- no `make wipe-db` needed.

This means:

- **No external state**: no state file, no shared volume between scraper instances
- **Self-healing**: if VictoriaMetrics data is wiped, builds are automatically re-ingested
- **Idempotency**: builds can be reprocessed safely; VictoriaMetrics deduplicates identical data points
- **Selective reprocessing**: bumping a pipeline's version reprocesses only that pipeline for all builds
