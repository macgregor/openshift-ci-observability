# CI Metrics Scraper

## Key files
- `scraper/scrape.py` -- single-file scraper: GCS navigation, metric/log conversion, VM/VL ingestion
- `podman-compose.yml` -- service orchestration (VM, VL, Grafana, scraper-watch, scraper-backfill)
- `Containerfile.scraper` -- scraper container image

## Architecture
- See ARCHITECTURE.md for system design and data flow
- See docs/appendix/ci-operator-metrics.md for JSON field reference
- See docs/appendix/gcs-bucket-layout.md for GCS path structure

## Conventions
- Single-file scraper -- all logic in scrape.py, no package structure
- Prometheus text exposition format for metrics, JSON lines for logs
- GCS XML API (not JSON API) for bucket listing
- State file tracks ingested build_ids to prevent duplicates
- Generic metric extraction: every numeric field becomes a metric
- Known transforms and canonical aliases are opt-in enhancements

## Testing
- Dry-run mode: `python scraper/scrape.py backfill --dry-run --since YYYY-MM-DD --until YYYY-MM-DD`
- Full stack: `podman-compose up -d` then check Grafana at localhost:3000
