# OpenShift CI Observability

Scrapes CI build artifacts (`ci-operator-metrics.json` and `ci-operator.log`) from GCS for OpenShift CI builds and ingests them into VictoriaMetrics (time-series) and VictoriaLogs (structured logs) for exploration via Grafana.

## Quickstart

```bash
cp .env.example .env
make up
```

Open Grafana at http://localhost:3000 (anonymous access, no login required). Historical data (last 90 days) is backfilled automatically. Set `BACKFILL_WINDOW` in `.env` to adjust (e.g. `6m`, `1y`).

Run `make` to see all available commands.

## Configuration

All parameters can be set via `.env` for compose or CLI flags for direct execution. The scraper uses env vars as defaults when CLI flags aren't provided. Run `python -m scraper backfill --help` or `watch --help` for the full list with env var names and defaults.

## Dashboards

Three dashboards are provisioned automatically:

- **CI Overview** (home page) -- at-a-glance CI health: failure count, success rate, retests per commit, pipeline duration trends, step breakdown, infrastructure overhead, and outlier tables with links to GitHub PRs and Prow jobs.
- **CI Investigation** -- drill into CI failures: identify top failing PRs, compare PR success rate against global baseline, scoped step failure analysis, outlier builds with links to GitHub and Prow, and build-level error logs.
- **CI Logs** -- browse ci-operator logs by level, PR, build, and source. Each log source gets its own panel to preserve ordering. Use the Level filter to surface errors across all builds.

Each dashboard has a collapsible "Dashboard Guide" row at the top with usage instructions.

## Service Endpoints

- **Grafana**: http://localhost:3000
- **VictoriaMetrics**: http://localhost:8428
- **VictoriaLogs**: http://localhost:9428

## More Information

- [ARCHITECTURE.md](ARCHITECTURE.md) -- system design and data flow
- [CONTRIBUTING.md](CONTRIBUTING.md) -- development setup, testing, and reset procedures
