# AICP CI Metrics Scraper

Scrapes `ci-operator-metrics.json` artifacts from GCS for OpenShift CI builds and ingests them into VictoriaMetrics (time-series) and VictoriaLogs (structured events) for exploration via Grafana.

## Quickstart

```bash
cp .env.example .env
podman-compose up -d
```

Open Grafana at http://localhost:3000 (anonymous access, no login required).

## Backfilling Historical Data

```bash
podman-compose --profile backfill up -d
```

Backfills the last 90 days by default. Set `BACKFILL_WINDOW` in `.env` to adjust (e.g. `6m`, `1y`).

## Configuration

All parameters can be set via `.env` for compose or CLI flags for direct execution. The scraper uses env vars as defaults when CLI flags aren't provided. Run `python scraper/scrape.py backfill --help` or `watch --help` for the full list with env var names and defaults.

## Service Endpoints

- **Grafana**: http://localhost:3000
- **VictoriaMetrics**: http://localhost:8428
- **VictoriaLogs**: http://localhost:9428

## More Information

- [ARCHITECTURE.md](ARCHITECTURE.md) -- system design and data flow
- [CONTRIBUTING.md](CONTRIBUTING.md) -- development setup, testing, and reset procedures
