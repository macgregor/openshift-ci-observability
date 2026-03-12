---
description: System architecture and data flow for the CI metrics scraper
---

# Architecture

## System Diagram

```
┌─────────────────┐
│  Google Cloud   │
│    Storage      │
│  (GCS Buckets)  │
└────────┬────────┘
         │
         │ GCS XML API
         │
         ▼
┌─────────────────┐
│     Scraper     │
│                 │
│  scraper-watch  │  (continuous polling)
│ scraper-backfill│  (one-time, exits when done)
└────────┬────────┘
         │
         │ Remote-write (Prometheus) + JSON lines
         │
         ▼
┌─────────────────────────────────┐
│  VictoriaMetrics + VictoriaLogs │
└────────┬────────────────────────┘
         │
         │ PromQL / LogsQL queries
         │
         ▼
┌─────────────────┐
│     Grafana     │
└─────────────────┘
```

## Data Flow

### Discovery

The scraper discovers CI builds for a specific repo via the GCS XML API:

1. **PR Enumeration**: List prefixes under `gs://test-platform-results/pr-logs/pull/{org}_{repo}/` (derived from the `--repo` flag)
2. **Job Enumeration**: Within each PR, list job directories
3. **Build Enumeration**: Within each job, list build directories
4. **Date Filtering**: Read `started.json` from each build to filter by the `--window` range
5. **Metric Extraction**: Parse `ci-operator-metrics.json` from qualifying builds

### Metric Conversion

Metrics are converted to Prometheus text exposition format:

- **Generic Extraction**: All numeric fields are automatically extracted as metrics
- **Known Transforms**:
  - Nanosecond values converted to seconds
  - Kubernetes quantities (e.g., `1Gi`, `500m`) parsed to numeric values
- **Canonical Aliases**: Common metric names are normalized (e.g., `duration_ns` -> `duration_seconds`)

Metrics are pushed to VictoriaMetrics via remote-write protocol.

### Log Conversion

Logs are converted to JSON lines format with two layers:

- **Layer 1 (Raw JSON)**: The entire `ci-operator-metrics.json` as a single log entry
- **Layer 2 (Structured)**: Per-entry logs for each item in each section (events, pods, nodes, etc.) with scalar fields flattened

Logs are pushed to VictoriaLogs via JSON lines ingestion.

## Portability Design

The scraper outputs data in standard formats:

- **Metrics**: Prometheus text exposition format
- **Logs**: JSON lines

This design enables migration to hosted observability platforms by changing the ingestion URL and adding authentication, without modifying the scraper's output format.

## Deduplication Strategy

### Metrics

VictoriaMetrics handles deduplication via `-dedup.minScrapeInterval=1ms` flag, which deduplicates samples with identical timestamps and labels.

### Logs

Deduplication is handled at the scraper level via the state file -- builds already recorded are skipped entirely. See State Management below.

## Operational Modes

The scraper runs as two compose services sharing a state volume:

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

Build processing state is tracked via a JSON file:

```json
{
  "build_id_1": "2024-01-01T00:00:00Z",
  "build_id_2": "2024-01-01T01:00:00Z"
}
```

- **Concurrent Access**: `fcntl.flock` provides file-level locking
- **Atomic Updates**: State is written atomically to prevent corruption
- **Idempotency**: Builds can be reprocessed safely; deduplication occurs at the storage layer
