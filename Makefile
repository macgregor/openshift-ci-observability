COMPOSE := podman-compose --profile backfill
CONTAINERS := ci-obs-victoriametrics ci-obs-victorialogs ci-obs-grafana ci-obs-scraper-watch ci-obs-scraper-backfill
VOLUMES := ci-obs-vm-data ci-obs-vl-data ci-obs-grafana-data ci-obs-scraper-state

.PHONY: up down restart wipe status help

up: ## Start the stack
	@if podman ps --format '{{.Names}}' | grep -q '^ci-obs-'; then \
		echo "Already running. Use 'make restart' to restart."; \
	else \
		$(COMPOSE) up -d && \
		echo "Grafana: http://localhost:3000"; \
	fi

down: ## Stop the stack
	@$(COMPOSE) down 2>/dev/null; true
	@podman rm -f $(CONTAINERS) 2>/dev/null; true

restart: ## Restart the stack
	@$(MAKE) -s down
	@$(COMPOSE) up -d
	@echo "Grafana: http://localhost:3000"

wipe: ## Stop and delete all data
	@echo "This will delete all metrics, logs, and scraper state."
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = y ] || exit 1
	@$(MAKE) -s down
	@podman volume rm -f $(VOLUMES) 2>/dev/null; true
	@echo "All data wiped."

status: ## Show running containers
	@$(COMPOSE) ps

help: ## Show available commands
	@grep -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
