.PHONY: help install install-dev test test-unit test-integration test-cov test-fast test-watch lint format typecheck security clean dev taskiq-worker taskiq-scheduler docker-build docker-run docker-test sync-kr-symbol-universe sync-upbit-symbol-universe sync-us-symbol-universe sync-kr-candles-backfill sync-kr-candles-incremental

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync

install-dev: ## Install development dependencies
	uv sync --all-groups

test: ## Run all tests
	uv run pytest tests/ -v

test-unit: ## Run unit tests only
	uv run pytest tests/ -v -m "not integration"

test-integration: ## Run integration tests only
	uv run pytest tests/ -v -m "integration"

test-cov: ## Run tests with coverage report
	uv run pytest tests/ -v --cov=app --cov-report=html --cov-report=term-missing

test-fast: ## Run tests without coverage (faster)
	uv run pytest tests/ -v --no-cov

test-watch: ## Run tests in watch mode
	uv run pytest tests/ -v -f

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

docker-build: ## Build Docker image
	docker build -t auto-trader .

docker-run: ## Run Docker container
	docker run -p 8000:8000 auto-trader

docker-test: ## Run tests in Docker
	docker run --rm auto-trader uv run pytest tests/ -v
