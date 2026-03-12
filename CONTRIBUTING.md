---
description: Developer guide for contributing to the CI metrics scraper
---

# Contributing

## Prerequisites

- podman
- podman-compose
- python3

## Local Development Setup

Install dependencies and run a dry-run backfill:

```bash
pip install -r scraper/requirements.txt
python scraper/scrape.py backfill --dry-run --window 2d
```

## Running the Full Stack Locally

```bash
podman-compose up -d
```

## Resetting Data

To start over with a clean database and re-ingest everything:

```bash
podman-compose --profile backfill down -v
podman-compose --profile backfill up -d --build
```

The `-v` flag removes named volumes (VictoriaMetrics, VictoriaLogs, Grafana, and scraper state). The backfill will re-ingest from scratch on the next start.

To reset only the scraper state (re-ingest without wiping metrics/logs):

```bash
podman exec aicp-ci-metrics-scraper_scraper-watch_1 rm /state/scrape-state.json
```

VictoriaMetrics deduplicates identical data points, so re-ingesting the same builds is safe.

## How to Add New Metric Transforms

Edit the `apply_known_transforms()` function in `scraper/scrape.py`.

## How to Add Canonical Aliases

Add entries to the `CANONICAL_ALIASES` dictionary in `scraper/scrape.py`.

## Project Structure

- `scraper/scrape.py` - Main scraper implementation with CLI, transform logic, and canonical aliases
- `scraper/requirements.txt` - Python dependencies
- `podman-compose.yml` - Container orchestration configuration for the full stack
