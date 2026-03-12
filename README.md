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

## Dashboards

Three dashboards are provisioned automatically:

- **CI Overview** (home page) -- at-a-glance CI health: failure count, success rate, retests per commit, pipeline duration trends, step breakdown, infrastructure overhead, and outlier tables with links to GitHub PRs and Prow jobs.
- **CI Investigation** -- drill into specific PRs and builds: compare a PR's success rate against the global baseline, view per-commit retry history, step failure leaderboard, pod phase breakdown, and build-level error logs.
- **Log Explorer** -- freeform LogsQL search across all ingested CI events, pods, builds, and nodes.

Each dashboard has a collapsible "Dashboard Guide" row at the top with usage instructions. Use the Job Type and Outcome filters to narrow scope across all panels.

## Service Endpoints

- **Grafana**: http://localhost:3000
- **VictoriaMetrics**: http://localhost:8428
- **VictoriaLogs**: http://localhost:9428

## More Information

- [ARCHITECTURE.md](ARCHITECTURE.md) -- system design and data flow
- [CONTRIBUTING.md](CONTRIBUTING.md) -- development setup, testing, and reset procedures
