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
pip install -r scraper/dev-requirements.txt
python -m scraper backfill --dry-run --window 2d
```

This installs both runtime and test dependencies. Run `make test` to run the test suite.

## Running the Full Stack Locally

```bash
make up
```

Run `make` to see all available commands.

## Resetting Data

To start over with a clean database and re-ingest everything:

```bash
make wipe-db
make up
```

This preserves the GCS artifact cache, so re-ingestion reads from local disk instead of re-downloading from GCS. To also clear the cache:

```bash
make wipe-all    # delete DB + cache
make wipe-cache  # delete cache only
```

Scraper state is stored in VictoriaMetrics itself (via a sentinel metric), so wiping the database automatically resets state. VictoriaMetrics deduplicates identical data points, so re-ingesting the same builds is safe.

**When to wipe:** Changes to scraping logic (new pipelines, modified metric extraction, new artifact parsing) require a DB wipe to reprocess existing builds with the updated code. The scraper skips builds already present in VictoriaMetrics, so existing builds won't pick up new data without a wipe. You don't need to clear the GCS cache for this -- the cached artifacts are the raw inputs, not the processed output.

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

## Documentation Frontmatter

Appendix docs use YAML frontmatter so AI tools can decide when to load them. Use this template:

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
