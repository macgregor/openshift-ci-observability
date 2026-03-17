COMPOSE := podman-compose --profile backfill
CONTAINERS := ci-obs-victoriametrics ci-obs-victorialogs ci-obs-grafana ci-obs-scraper-watch ci-obs-scraper-backfill
VOLUMES := ci-obs-vm-data ci-obs-vl-data ci-obs-grafana-data
CACHE_VOLUME := ci-obs-gcs-cache

.PHONY: check-deps build up down restart wipe-db wipe-cache wipe-all logs status test help

# Print GCS cache size with a note about disabling/reclaiming.
# Silently skips if the volume doesn't exist yet.
define cache_report
	@mp=$$(podman volume inspect $(CACHE_VOLUME) --format '{{.Mountpoint}}' 2>/dev/null) && \
		sz=$$(du -sh "$$mp" 2>/dev/null | cut -f1) && \
		echo "GCS cache: $$sz (set GCS_NO_CACHE=true in .env to disable, 'make wipe-cache' to reclaim)"
endef

check-deps:
	@command -v podman >/dev/null || { echo "podman not found. Install: https://podman.io/docs/installation"; exit 1; }
	@command -v podman-compose >/dev/null || { echo "podman-compose not found. Install: pip install podman-compose"; exit 1; }

build: check-deps ## Build the scraper container image
	@podman build -f Containerfile.scraper -t ci-obs-scraper:latest .

up: check-deps build ## Start the stack (rebuilds scraper image)
	@if podman ps --format '{{.Names}}' | grep -q '^ci-obs-'; then \
		echo "Already running. Use 'make restart' to restart."; \
	else \
		$(COMPOSE) up -d && \
		echo "Grafana: http://localhost:3000"; \
	fi
	$(cache_report)

down: check-deps ## Stop the stack
	$(cache_report)
	@$(COMPOSE) down 2>/dev/null; true
	@podman rm -f $(CONTAINERS) 2>/dev/null; true

restart: check-deps build ## Restart the stack (rebuilds scraper image)
	@$(MAKE) -s down
	@$(COMPOSE) up -d
	@echo "Grafana: http://localhost:3000"
	$(cache_report)

wipe-db: check-deps ## Stop and delete metrics/logs (keeps GCS cache for fast re-ingestion)
	@echo "This will delete all metrics, logs, and scraper state (GCS cache preserved)."
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = y ] || exit 1
	@$(MAKE) -s down
	@podman volume rm -f $(VOLUMES) 2>/dev/null; true
	@echo "Data wiped. GCS cache preserved -- re-ingestion will use cached artifacts."
	$(cache_report)

wipe-cache: check-deps ## Delete the GCS artifact cache (can be tens of GB)
	$(cache_report)
	@$(MAKE) -s down
	@podman volume rm -f $(CACHE_VOLUME) 2>/dev/null; true
	@echo "GCS cache deleted."

wipe-all: check-deps ## Stop and delete everything including GCS cache
	@echo "This will delete all data AND the GCS artifact cache."
	$(cache_report)
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = y ] || exit 1
	@$(MAKE) -s down
	@podman volume rm -f $(VOLUMES) $(CACHE_VOLUME) 2>/dev/null; true
	@echo "All data and cache wiped."

logs: check-deps ## Tail logs (use SVC=scraper-watch to filter)
	@$(COMPOSE) logs -f --tail=100 $(SVC)

status: check-deps ## Show running containers and volume sizes
	@$(COMPOSE) ps
	@echo ""
	@echo "Volumes:"
	@for v in $(VOLUMES); do \
		mp=$$(podman volume inspect "$$v" --format '{{.Mountpoint}}' 2>/dev/null) && \
		sz=$$(du -sh "$$mp" 2>/dev/null | cut -f1) && \
		printf "  %-25s %s\n" "$$v" "$$sz"; \
	done
	$(cache_report)

test: ## Run tests
	python -m pytest tests/ -v

help: ## Show available commands
	@grep -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
