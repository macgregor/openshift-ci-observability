---
description: Developer guide for contributing to OpenShift CI Observability
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
podman-compose exec scraper-watch rm /state/scrape-state.json
```

VictoriaMetrics deduplicates identical data points, so re-ingesting the same builds is safe.

## Chrome DevTools MCP (for Claude)

The project includes a chrome-devtools MCP server that lets Claude interact with Grafana dashboards in a browser -- taking screenshots, inspecting panels, clicking elements, and verifying dashboard changes visually.

**Prerequisites:**
- Node.js / npx (for the MCP server)
- Chromium or Google Chrome

**Setup:**

The MCP server and auto-launch hook are committed to the repo (`.mcp.json` and `.claude/hooks/ensure-chrome-debug.sh`). No manual setup is needed -- when Claude uses any chrome-devtools tool, the hook automatically launches Chromium with remote debugging on port 9222 if it isn't already running.

If Chromium isn't in your PATH, set the browser binary path in the hook script or launch it manually:

```bash
chromium-browser --remote-debugging-port=9222 --user-data-dir=$HOME/.chrome-debug-profile
```

## How to Add New Metric Transforms

Edit the `apply_known_transforms()` function in `scraper/scrape.py`.

## How to Add Canonical Aliases

Add entries to the `CANONICAL_ALIASES` dictionary in `scraper/scrape.py`.

## Project Structure

- `scraper/scrape.py` - Main scraper implementation with CLI, transform logic, and canonical aliases
- `scraper/requirements.txt` - Python dependencies
- `podman-compose.yml` - Container orchestration configuration for the full stack
