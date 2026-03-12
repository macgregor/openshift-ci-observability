# CI Metrics Scraper

## Key files
- `scraper/scrape.py` -- single-file scraper: GCS navigation, metric/log conversion, VM/VL ingestion
- `podman-compose.yml` -- service orchestration (VM, VL, Grafana, scraper-watch, scraper-backfill)
- `Containerfile.scraper` -- scraper container image

## Documentation 
- @README.md
- @ARCHITECTURE.md -- system design, data flow, deduplication, operational modes
- @CONTRIBUTING.md -- local dev setup, testing, reset procedures. Use when building, debugging, or onboarding.
- docs/appendix/ci-operator-metrics.md -- JSON field reference for ci-operator-metrics.json
- docs/appendix/gcs-bucket-layout.md -- GCS path structure and XML API navigation
- docs/appendix/grafana-visualizations.md -- visualization selection, design principles, data format gotchas. Use when building or modifying Grafana dashboards.

## Documentation quality
- Run `/document-reviewer` after creating or substantially updating documentation. Skip for minor fixes (typos, single-line edits).

## Dashboard verification
- After creating or modifying Grafana dashboards, verify them visually in the browser using chrome-devtools MCP. Don't rely solely on JSON syntax validation -- panels can be syntactically valid but render "No data", "Configure your query", or display incorrect data.
- Navigate to the dashboard URL, wait for panels to load, and screenshot to confirm panels render with actual data.

## Conventions
- Single-file scraper -- all logic in scrape.py, no package structure
- Prometheus text exposition format for metrics, JSON lines for logs
- GCS XML API (not JSON API) for bucket listing
- State file tracks ingested build_ids to prevent duplicates
- Generic metric extraction: every numeric field becomes a metric
- Known transforms and canonical aliases are opt-in enhancements

## Testing
- Dry-run mode: `python scraper/scrape.py backfill --dry-run --window 2d`
- Full stack: `podman-compose up -d` then check Grafana at localhost:3000

## YAML Frontmatter Template

```yaml
---
name: document-name  # required: lowercase-with-hyphens, max 64 chars
description: >  # required: when should AI load this? max 1024 chars
  Clear statement of when AI should load this document.
categories: [category1, category2]  # optional: broad classification
tags: [tag1, tag2]  # optional: specific concepts
related_docs:  # optional: relative paths from project root
  - path/to/doc.md
complexity: basic  # optional: basic|intermediate|advanced
---
```
