# PROJECT KNOWLEDGE BASE

**Generated:** 2026-02-16 17:29 KST
**Commit:** 41b7513
**Branch:** main

## OVERVIEW
Auto Trader is a Python 3.13+ service stack for market data, AI-assisted analysis, and order workflows.
Primary runtimes are FastAPI (`app/main.py`), TaskIQ worker/scheduler, MCP server (`app/mcp_server/main.py`), and websocket monitor processes.

## STRUCTURE
```text
auto_trader/
├── app/                      # runtime code (api, services, jobs/tasks, mcp tools)
├── tests/                    # pytest suites and fixtures
├── scripts/                  # deployment, migration, ops utilities
├── alembic/                  # migration env + revision history
├── data/                     # static symbols and loader constants
├── blog/                     # internal articles and image generation utilities
└── docker-compose*.yml       # local/prod stack definitions
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| API lifecycle and router wiring | `app/main.py` | App creation, router includes, exception handling |
| HTTP endpoints | `app/routers/` and `app/auth/` | Domain routers and auth/admin/web auth routers |
| Domain/business logic | `app/services/` | Upbit/KIS/Yahoo + holdings/trading service logic |
| Scheduled jobs and worker tasks | `app/jobs/`, `app/tasks/`, `app/core/taskiq_broker.py` | TaskIQ broker/scheduler integration |
| MCP tool behavior | `app/mcp_server/tooling/` and `app/mcp_server/README.md` | Public tool contract and market-specific behavior |
| Tests and fixtures | `tests/` and `tests/conftest.py` | Strict markers and fixture bootstrap |
| CI/build/deploy flows | `Makefile`, `.github/workflows/`, `.circleci/config.yml`, `scripts/` | Commands, job ordering, deployment modes |

## CODE MAP
| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `create_app` | Function | `app/main.py` | API runtime target (`app.main:api`) | Builds FastAPI app and middleware/router stack |
| `router` | APIRouter vars | `app/routers/*.py` + `app/auth/*.py` | 21 router modules | HTTP surface area |
| `broker` | TaskIQ broker | `app/core/taskiq_broker.py` | Worker/scheduler commands | Background task transport and startup hooks |
| `sched` | TaskIQ scheduler | `app/core/scheduler.py` | `make taskiq-scheduler` | Periodic task scheduling |
| `@broker.task(...)` | Scheduled task defs | `app/tasks/daily_scan_tasks.py` | 2 scheduled entries | Strategy and crash-detection schedules |
| `main` | Function | `app/mcp_server/main.py` | Module entrypoint | MCP process bootstrap and mode binding |

## CONVENTIONS
- Source of truth for toolchain: `pyproject.toml` + `Makefile` + CI workflows.
- Runtime version is Python 3.13+ (`pyproject.toml`, CI); do not assume older doc values.
- Formatting/lint/type stack is Ruff + Pyright, not Black/isort/mypy.
- Tests use strict pytest markers/config with `--cov-fail-under=50`.
- Dependency and command execution uses `uv` (`uv sync`, `uv run ...`).

## ANTI-PATTERNS (THIS PROJECT)
- Do not keep default secrets in production env (`env.example`: explicit prohibition).
- Do not hardcode secrets or credentials (`blog/blog_8_authentication.md`, `blog/blog_9_kis_trading.md`).
- Do not store passwords in plaintext (`blog/blog_8_authentication.md`: explicit prohibition).
- Do not add new usage of deprecated tick-size helper (`app/mcp_server/tick_size.py`: use `get_tick_size_kr()`).

## UNIQUE STYLES
- Multi-runtime repo: API, TaskIQ worker/scheduler, MCP server, websocket monitors.
- Root contains many executable diagnostics (`debug_*.py`, `manage_users.py`, monitor scripts).
- MCP tooling is organized by capability with registration modules per domain.
- Production compose uses host networking for service-to-service communication.

## COMMANDS
```bash
make install-dev
make dev
make taskiq-worker
make taskiq-scheduler
uv run python -m app.mcp_server.main
python websocket_monitor.py --mode both
make test
make test-integration
make lint
make security
uv run alembic upgrade head
```

## NOTES
- Historical docs and older guidance may conflict with current config values; prefer executable config files.
- `app/services/kis.py` is very large; use targeted searches/offset reads instead of full-file scans.
- For MCP behavior changes, update both code and `app/mcp_server/README.md` to keep tool contracts aligned.
