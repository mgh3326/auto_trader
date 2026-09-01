.PHONY: help install install-dev test test-unit test-integration test-services-split test-cov test-fast test-watch lint format typecheck security clean dev taskiq-worker taskiq-scheduler docker-build docker-run docker-test dev-config dev-up dev-seed dev-verify dev-down sync-kr-symbol-universe sync-upbit-symbol-universe sync-us-symbol-universe sync-kr-candles-backfill sync-kr-candles-incremental frontend-install frontend-dev frontend-build frontend-typecheck

DEV_ENV_FILE ?= .env.dev
-include $(DEV_ENV_FILE)

COMPOSE_PROJECT_NAME ?= at-dev-local
DEV_PG_BIND_HOST ?= 127.0.0.1
DEV_PG_PORT ?= 55432
DEV_PG_USER ?= atdev
DEV_PG_PASSWORD ?= atdev-local-password
DEV_PG_DATABASE ?= auto_trader_dev
DEV_REDIS_BIND_HOST ?= 127.0.0.1
DEV_REDIS_PORT ?= 56379
DEV_UP_TIMEOUT_SECONDS ?= 180
DEV_HEALTH_TIMEOUT_SECONDS ?= 30
DEV_API_PORT ?= 58080
DEV_SEED_DUMP ?= dev-seed.dump
DEV_DATABASE_URL ?= postgresql+asyncpg://$(DEV_PG_USER):$(DEV_PG_PASSWORD)@$(DEV_PG_BIND_HOST):$(DEV_PG_PORT)/$(DEV_PG_DATABASE)
DEV_REDIS_URL ?= redis://$(DEV_REDIS_BIND_HOST):$(DEV_REDIS_PORT)/0
DEV_COMPOSE_ENV = COMPOSE_PROJECT_NAME="$(COMPOSE_PROJECT_NAME)" DEV_PG_BIND_HOST="$(DEV_PG_BIND_HOST)" DEV_PG_PORT="$(DEV_PG_PORT)" DEV_PG_USER="$(DEV_PG_USER)" DEV_PG_PASSWORD="$(DEV_PG_PASSWORD)" DEV_PG_DATABASE="$(DEV_PG_DATABASE)" DEV_REDIS_BIND_HOST="$(DEV_REDIS_BIND_HOST)" DEV_REDIS_PORT="$(DEV_REDIS_PORT)"
DEV_COMPOSE = $(DEV_COMPOSE_ENV) docker compose --env-file "$(DEV_ENV_FILE)" -f docker-compose.dev.yml
DEV_RUNTIME_SANITIZER = env -i PATH="$(PATH)" TMPDIR=/tmp
DEV_RUNTIME_ENV = $(DEV_RUNTIME_SANITIZER) ENV_FILE="$(DEV_ENV_FILE)" DATABASE_URL="$(DEV_DATABASE_URL)" REDIS_URL="$(DEV_REDIS_URL)"

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync

install-dev: ## Install development dependencies
	uv sync --all-groups

test: ## Run all tests (excludes live)
	uv run pytest tests/ -q -ra -m "not live"

test-unit: ## Run positively marked unit tests (excludes slow and live)
	uv run pytest tests/ -q -ra -m "unit and not integration and not slow and not live"

test-integration: ## Run integration tests only (excludes live)
	uv run pytest tests/ -q -ra -m "integration and not live"

test-services-split: ## Run split service tests for former test_services.py scope
	uv run pytest --no-cov -q \
		tests/test_services_upbit.py \
		tests/test_services_kis_client.py \
		tests/test_services_kis_market_data.py \
		tests/test_services_kis_market_data_unit.py \
		tests/test_services_kis_logging.py \
		tests/test_services_dart.py \
		tests/test_services_yahoo.py

test-cov: ## Run tests with coverage report (excludes live)
	uv run pytest tests/ -q -ra -m "not live" --cov=app --cov-report=html --cov-report=term-missing

test-fast: ## Run the bounded parallel unit development loop
	uv run pytest tests/ -q -ra -m "unit and not integration and not slow and not live" \
		--no-cov --maxfail=1 -n 4 --dist=loadfile

test-watch: ## Run tests in watch mode (excludes live)
	uv run pytest tests/ -q -ra -m "not live" -f

test-live: ## Run live API tests only (requires external network)
	uv run pytest tests/ -q -ra -m "integration and live" --run-live --no-cov
lint: ## Run linting checks (Ruff + ty)
	uv run ruff check app/ tests/ research/ scripts/
	uv run ruff format --check app/ tests/ research/ scripts/
	uv run ty check app/ --error-on-warning

format: ## Format code with Ruff
	uv run ruff format app/ tests/ research/ scripts/
	uv run ruff check --fix app/ tests/ research/ scripts/

typecheck: ## Run ty type checking
	uv run ty check app/ --error-on-warning

security: ## Run security checks
	uv run bandit -r app/
	uv run safety check

clean: ## Clean up generated files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type f -name ".coverage" -delete
	find . -type d -name "htmlcov" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

dev: ## Start development server
	uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000

taskiq-worker: ## Start TaskIQ worker
	uv run taskiq worker app.core.taskiq_broker:broker app.tasks

taskiq-scheduler: ## Start TaskIQ scheduler
	uv run taskiq scheduler app.core.scheduler:sched app.tasks

sync-kr-symbol-universe: ## Sync KR symbol universe for KR 1h routing
	uv run python scripts/sync_kr_symbol_universe.py

sync-upbit-symbol-universe: ## Sync Upbit symbol universe for crypto symbol resolution
	uv run python scripts/sync_upbit_symbol_universe.py

sync-us-symbol-universe: ## Sync US symbol universe for US symbol/exchange resolution
	uv run python scripts/sync_us_symbol_universe.py

sync-toss-warnings: ## Sync Toss warnings for KR market
	uv run python scripts/sync_toss_warnings.py

sync-kr-candles-backfill: ## Backfill KR candles for recent sessions
	uv run python scripts/sync_kr_candles.py --mode backfill --sessions 10

sync-kr-candles-incremental: ## Incremental KR candles sync (venue-gated)
	uv run python scripts/sync_kr_candles.py --mode incremental

docker-build: ## Build Docker image
	vcs_ref="$$(git rev-parse HEAD)"; \
	docker build --build-arg VCS_REF="$$vcs_ref" -f Dockerfile.api -t auto_trader-api:local .

docker-run: docker-build ## Run Docker container
	docker run --rm --env-file .env -p 8000:8000 auto_trader-api:local

docker-test: docker-build ## Run tests in Docker
	docker run --rm auto_trader-api:local uv run pytest tests/ -v

_dev-env-check:
	@if [ ! -f "$(DEV_ENV_FILE)" ]; then \
		printf '%s\n' "Missing $(DEV_ENV_FILE). Copy .env.dev.example first."; \
		exit 2; \
	fi
	@case "$(COMPOSE_PROJECT_NAME)" in \
		at-dev-?*) ;; \
		*) printf '%s\n' "COMPOSE_PROJECT_NAME must use at-dev-<suffix>."; exit 2;; \
	esac

dev-config: _dev-env-check ## Render and validate the isolated development compose config
	@$(DEV_COMPOSE) config

dev-up: _dev-env-check ## Start isolated PostgreSQL/Timescale and Redis with a bounded readiness wait
	@$(DEV_COMPOSE) pull -q
	@$(DEV_COMPOSE) up -d
	@deadline=$$(($$(date +%s) + $(DEV_UP_TIMEOUT_SECONDS))); \
	until $(DEV_COMPOSE) exec -T db sh -ec 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" >/dev/null'; do \
		if [ $$(date +%s) -ge $$deadline ]; then \
			printf '%s\n' "Timed out waiting for development PostgreSQL after $(DEV_UP_TIMEOUT_SECONDS)s."; \
			$(DEV_COMPOSE) ps; \
			exit 1; \
		fi; \
		sleep 2; \
	done
	@printf '%s\n' "Development PostgreSQL is ready."

dev-seed: dev-up ## Migrate to Alembic head, then restore dev-seed.dump when present
	@$(DEV_RUNTIME_ENV) uv run alembic upgrade head
	@if [ -f "$(DEV_SEED_DUMP)" ]; then \
		printf '%s\n' "Restoring $(DEV_SEED_DUMP) into the namespaced development database."; \
		$(DEV_COMPOSE) exec -T db sh -ec 'psql -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < "$(DEV_SEED_DUMP)"; \
	else \
		printf '%s\n' "No $(DEV_SEED_DUMP) found; Alembic head is ready with an intentionally empty database."; \
	fi

dev-verify: dev-up ## Verify Alembic, the bounded dev-kit smoke path, and /healthz startup
	@$(DEV_RUNTIME_ENV) uv run alembic current
	@$(DEV_RUNTIME_ENV) uv run --group test pytest tests/test_dev_kit.py -q --no-cov
	@set -eu; \
	log_file=$$(mktemp "$${TMPDIR:-/tmp}/at-dev-verify.XXXXXX"); \
	cleanup() { \
		if [ -n "$${pid:-}" ] && kill -0 "$$pid" >/dev/null 2>&1; then kill "$$pid" >/dev/null 2>&1 || true; fi; \
		if [ -n "$${pid:-}" ]; then wait "$$pid" >/dev/null 2>&1 || true; fi; \
		rm -f "$$log_file"; \
	}; \
	$(DEV_RUNTIME_ENV) uv run uvicorn app.main:app --host 127.0.0.1 --port "$(DEV_API_PORT)" >"$$log_file" 2>&1 & \
	pid=$$!; \
	trap cleanup EXIT INT TERM; \
	if ! $(DEV_RUNTIME_ENV) uv run python scripts/devkit_healthcheck.py --port "$(DEV_API_PORT)" --timeout-seconds "$(DEV_HEALTH_TIMEOUT_SECONDS)"; then \
		cat "$$log_file"; \
		exit 1; \
	fi; \
	cleanup; \
	trap - EXIT INT TERM

dev-down: _dev-env-check ## Stop only this isolated development compose project; preserve its volumes
	@$(DEV_COMPOSE) down --remove-orphans
