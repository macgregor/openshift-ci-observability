# OpenShift CI Observability

Scrapes CI build artifacts (`ci-operator-metrics.json`, `ci-operator.log`, and JUnit XML) from GCS for any OpenShift CI repository and ingests them into VictoriaMetrics (time-series) and VictoriaLogs (structured logs) for exploration via Grafana.

Works with any GitHub repository that uses [OpenShift CI](https://docs.ci.openshift.org/) (ci-operator / Prow). Point it at your repo and get dashboards, metrics, and log search immediately.

## Quickstart

```bash
cp .env.example .env
# Edit .env and set REPO to your GitHub org/repo:
#   REPO=openshift/cluster-monitoring-operator
make up
```

Open Grafana at http://localhost:3000 (anonymous access, no login required). Historical data (last 90 days) is backfilled automatically. Set `BACKFILL_WINDOW` in `.env` to adjust (e.g. `6m`, `1y`).

Run `make` to see all available commands.

## Configuration

`REPO` is the only required setting -- set it in `.env` to the GitHub org/repo you want to scrape (the same `org/repo` as the GitHub URL path, e.g. `openshift/installer`). All other parameters have sensible defaults. Run `python -m scraper backfill --help` or `watch --help` for the full list.

## Dashboards

Four dashboards are provisioned automatically:

- **CI Overview** (home page) -- at-a-glance CI health: failure count, success rate, retests per commit, pipeline duration trends, step breakdown, infrastructure overhead, and outlier tables with links to GitHub PRs and Prow jobs.
- **CI Investigation** -- drill into CI failures: identify top failing PRs, compare PR success rate against global baseline, scoped step failure analysis, outlier builds with links to GitHub and Prow, and build-level error logs.
- **CI Tests** -- test-case-level results from JUnit XML: test pass rate, top failing tests, slowest tests, suite duration trends, and per-build test results with failure messages.
- **CI Logs** -- browse ci-operator logs by level, PR, build, and source. Each log source gets its own panel to preserve ordering. Use the Level filter to surface errors across all builds.

Each dashboard has a collapsible "Dashboard Guide" row at the top with usage instructions.

## CI Investigator (Claude Code)

If you use [Claude Code](https://docs.anthropic.com/en/docs/claude-code), the `/ci-investigator` skill provides conversational CI failure analysis powered by the ingested data. Instead of manually writing PromQL or LogsQL queries, describe what you want to know:

```
/ci-investigator is CI healthy?
/ci-investigator PR 1234 keeps failing
/ci-investigator build 1789456300123456789
/ci-investigator what's causing ipi-install failures this week?
```

The investigator queries VictoriaMetrics and VictoriaLogs, traces failure chains to root cause, classifies failures (infrastructure, flaky test, quota, etc.), and recommends next steps -- all without leaving your terminal.

## Service Endpoints

- **Grafana**: http://localhost:3000
- **VictoriaMetrics**: http://localhost:8428
- **VictoriaLogs**: http://localhost:9428

## More Information

- [ARCHITECTURE.md](ARCHITECTURE.md) -- system design and data flow
- [CONTRIBUTING.md](CONTRIBUTING.md) -- development setup, testing, and reset procedures
