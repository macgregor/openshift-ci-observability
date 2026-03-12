# AICP CI Metrics Scraper

This tool scrapes `ci-operator-metrics.json` artifacts from Google Cloud Storage for OpenShift CI builds and ingests them into VictoriaMetrics (time-series) and VictoriaLogs (structured events) for exploration via Grafana. It watches a GitHub repository for new builds, downloads their metrics artifacts, and makes the data queryable through a local observability stack.

## Quickstart

```bash
cp .env.example .env
podman-compose up -d
```

Open Grafana at http://localhost:3000 (credentials: `admin`/`admin`).

## Backfilling Historical Data

To ingest metrics from past builds, edit the date range in `.env`:

```bash
# Edit .env to set BACKFILL_SINCE and BACKFILL_UNTIL
podman-compose --profile backfill up -d
```

## Configuration

Customize behavior by editing `.env`:

- `REPO`: GitHub repository to monitor (e.g., `opendatahub-io/opendatahub-operator`)
- `WATCH_WINDOW_HOURS`: How far back to look for recent builds (default: 24)
- `POLL_INTERVAL`: Seconds between polling cycles (default: 300)
- `BACKFILL_SINCE`: Start date for historical backfill (format: `YYYY-MM-DD`)
- `BACKFILL_UNTIL`: End date for historical backfill (format: `YYYY-MM-DD`)

## Service Endpoints

- **VictoriaMetrics**: http://localhost:8428
- **VictoriaLogs**: http://localhost:9428
- **Grafana**: http://localhost:3000

## More Information

- [ARCHITECTURE.md](ARCHITECTURE.md) - System design and data flow
- [CONTRIBUTING.md](CONTRIBUTING.md) - Development guidelines and testing
