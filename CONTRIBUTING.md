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

## How to Add New Metric Transforms

Edit the `apply_known_transforms()` function in `scraper/scrape.py`.

## How to Add Canonical Aliases

Add entries to the `CANONICAL_ALIASES` dictionary in `scraper/scrape.py`.

## Project Structure

- `scraper/scrape.py` - Main scraper implementation with CLI, transform logic, and canonical aliases
- `scraper/requirements.txt` - Python dependencies
- `podman-compose.yml` - Container orchestration configuration for the full stack
