.PHONY: help install install-dev test test-unit test-integration test-services-split test-cov test-fast test-watch lint format typecheck security clean dev taskiq-worker taskiq-scheduler docker-build docker-run docker-test sync-kr-symbol-universe sync-upbit-symbol-universe sync-us-symbol-universe sync-kr-candles-backfill sync-kr-candles-incremental frontend-install frontend-dev frontend-build frontend-typecheck

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync

install-dev: ## Install development dependencies
	uv sync --all-groups

test: ## Run all tests (excludes live)
	uv run pytest tests/ -v -m "not live"

test-unit: ## Run unit tests only (excludes integration and live)
	uv run pytest tests/ -v -m "not integration and not live"

test-integration: ## Run integration tests only (excludes live)
	uv run pytest tests/ -v -m "integration and not live"

test-services-split: ## Run split service tests for former test_services.py scope
	uv run pytest --no-cov -q \
		tests/test_services_upbit.py \
		tests/test_services_kis_client.py \
		tests/test_services_kis_market_data.py \
		tests/test_services_kis_logging.py \
		tests/test_services_stock_info.py \
		tests/test_services_gemini.py \
		tests/test_services_dart.py \
		tests/test_services_yahoo.py

test-cov: ## Run tests with coverage report (excludes live)
	uv run pytest tests/ -v -m "not live" --cov=app --cov-report=html --cov-report=term-missing

test-fast: ## Run tests without coverage (faster, excludes live)
	uv run pytest tests/ -v -m "not live" --no-cov

test-watch: ## Run tests in watch mode (excludes live)
	uv run pytest tests/ -v -m "not live" -f

test-live: ## Run live API tests only (requires external network)
	uv run pytest tests/ -v -m "integration and live" --run-live --no-cov
lint: ## Run linting checks (Ruff + ty)
	uv run ruff check app/ tests/
	uv run ruff format --check app/ tests/
	uv run ty check app/ --error-on-warning

format: ## Format code with Ruff
	uv run ruff format app/ tests/
	uv run ruff check --fix app/ tests/

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

sync-kr-candles-backfill: ## Backfill KR candles for recent sessions
	uv run python scripts/sync_kr_candles.py --mode backfill --sessions 10

sync-kr-candles-incremental: ## Incremental KR candles sync (venue-gated)
	uv run python scripts/sync_kr_candles.py --mode incremental

frontend-install: ## Install React/Vite workspace deps (npm ci)
	cd frontend/trading-decision && npm ci

frontend-dev: ## Start Vite dev server on :5173 (requires `make dev` for the API on :8000)
	cd frontend/trading-decision && npm run dev

frontend-build: ## Build the React/Vite workspace into frontend/trading-decision/dist/
	cd frontend/trading-decision && npm run build

frontend-typecheck: ## Run tsc --noEmit on the React/Vite workspace
	cd frontend/trading-decision && npm run typecheck

docker-build: ## Build Docker image
	vcs_ref="$$(git rev-parse HEAD)"; \
	docker build --build-arg VCS_REF="$$vcs_ref" -f Dockerfile.api -t auto_trader-api:local .

docker-run: docker-build ## Run Docker container
	docker run --rm --env-file .env -p 8000:8000 auto_trader-api:local

docker-test: docker-build ## Run tests in Docker
	docker run --rm auto_trader-api:local uv run pytest tests/ -v
