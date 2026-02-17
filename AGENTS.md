# PROJECT KNOWLEDGE BASE

**Generated:** 2026-02-17 12:32 KST
**Commit:** 6b23e7b
**Branch:** main

## OVERVIEW
Auto Trader is a Python 3.13+ multi-runtime system for market data ingestion, AI analysis, and trade execution.
Primary processes are FastAPI (`app/main.py`), TaskIQ worker/scheduler, MCP server (`app/mcp_server/main.py`), and websocket monitor processes.

## STRUCTURE
```text
auto_trader/
├── app/                      # runtime code (api, auth, services, jobs/tasks, mcp, monitoring)
├── tests/                    # pytest suites and fixtures
├── scripts/                  # deploy, migration, health, environment utilities
├── alembic/                  # migration env + revision history
├── data/                     # loader modules + static market reference assets
├── blog/                     # internal docs plus supplemental script/test assets
├── docs/                     # plans and operational notes
└── docker-compose*.yml       # local/prod/migration stack definitions
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| API lifecycle and router wiring | `app/main.py` | App creation, router includes, middleware, exception handling |
| Background execution | `app/core/taskiq_broker.py`, `app/core/scheduler.py`, `app/tasks/` | Broker/scheduler wiring and scheduled task declarations |
| Domain business logic | `app/services/` | Provider clients and trading/holdings/orchestration services |
| MCP server behavior | `app/mcp_server/` and `app/mcp_server/tooling/` | Process bootstrap + tool registration and handlers |
| Monitoring and notifications | `app/monitoring/` | Sentry integration and Telegram trade notifications |
| ORM + API DTO boundaries | `app/models/`, `app/schemas/` | SQLAlchemy models vs Pydantic transport schemas |
| Test contracts and fixtures | `tests/` and `tests/conftest.py` | Strict markers/config and shared fixture bootstrap |
| Deployment and migration flow | `scripts/`, `docker-compose.prod.yml`, `docker-compose.migration.yml` | Operator-facing deploy/migrate/health workflows |

## CODE MAP
| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `create_app` | Function | `app/main.py` | API runtime target (`app.main:api`) | FastAPI app bootstrap and lifecycle wiring |
| `broker` | Variable | `app/core/taskiq_broker.py` | API + scheduler + tasks | TaskIQ broker and worker middleware |
| `sched` | Variable | `app/core/scheduler.py` | `make taskiq-scheduler` | Periodic schedule execution |
| `register_all_tools` | Function | `app/mcp_server/tooling/registry.py` | MCP bootstrap + tests | MCP tool registration orchestrator |
| `get_trade_notifier` | Function | `app/monitoring/trade_notifier.py` | API + worker + services/jobs | Singleton notifier lifecycle and delivery |
| `KISClient` | Class | `app/services/kis.py` | services/jobs/routers/mcp | KIS integration backbone |

## CONVENTIONS
- Toolchain source of truth is `pyproject.toml`, `Makefile`, and CI workflows.
- Runtime baseline is Python 3.13+; dependency and command execution use `uv`.
- Formatting/lint/type checks are Ruff + Pyright.
- Test suite uses strict pytest markers/config (`slow`, `integration`, `unit`) with `--cov-fail-under=50`.
- Keep task declarations in `app/tasks/`; job orchestration stays in `app/jobs/`.
- Keep MCP behavior changes synchronized with `app/mcp_server/README.md` and tests.

## ANTI-PATTERNS (THIS PROJECT)
- Do not hardcode credentials/secrets in code or scripts.
- Do not keep default/example secrets in production environments.
- Do not add new usage of deprecated tick-size helper (`app/mcp_server/tick_size.py:_get_tick_size`).
- Do not add `@broker.task(...)` directly in `app/jobs/`.
- Do not embed heavy business logic directly in router handlers.

## UNIQUE STYLES
- Multi-runtime repository: API, worker/scheduler, MCP process, and websocket monitors all coexist.
- Root includes many operator/debug utilities (`debug_*.py`, monitor scripts, management CLIs).
- Production compose is host-network oriented and includes a migration profile workflow.
- `data/stocks_info/` mixes Python loaders with static `.h` reference files.

## COMMANDS
```bash
# setup and local runtime
make install-dev
make dev
make taskiq-worker
make taskiq-scheduler
uv run python -m app.mcp_server.main
python websocket_monitor.py --mode both

# quality and tests
make test
make test-unit
make test-integration
make lint
make security

# migration and deploy operations
uv run alembic upgrade head
bash scripts/migration-check.sh
bash scripts/migrate.sh
bash scripts/deploy.sh --manual-migrate --health-check
docker compose -f docker-compose.prod.yml --profile migration up migration
bash scripts/healthcheck.sh
python manage_users.py list
```

## NOTES
- Some docs still contain older Celery phrasing; runtime execution is TaskIQ-based.
- Deployment and migration scripts include interactive/safety checks; do not assume non-interactive behavior.
- `tests/` is canonical pytest discovery root; root/blog `test_*.py` files are supplemental scripts.
- For very large files (`app/services/kis.py`, large `tests/test_mcp_*.py`), prefer targeted reads/searches.
- Child AGENTS files under `app/` and `data/` provide tighter local rules and override where needed.
