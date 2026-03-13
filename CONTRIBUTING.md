---
description: Developer guide for contributing to OpenShift CI Observability
---

# Contributing

## Prerequisites

- podman
- podman-compose
- make
- python3

## Local Development Setup

Install dependencies and run a dry-run backfill:

```bash
pip install -r scraper/requirements.txt
python -m scraper backfill --dry-run --window 2d
```

## Running the Full Stack Locally

```bash
make up
```

Run `make` to see all available commands (`up`, `down`, `restart`, `wipe`, `status`).

## Resetting Data

To start over with a clean database and re-ingest everything:

```bash
make wipe
make up
```

Scraper state is stored in VictoriaMetrics itself (via a sentinel metric), so wiping the database automatically resets state. VictoriaMetrics deduplicates identical data points, so re-ingesting the same builds is safe.

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

