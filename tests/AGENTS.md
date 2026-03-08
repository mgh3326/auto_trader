# TESTS KNOWLEDGE BASE

## OVERVIEW
`tests/` is the primary pytest suite for API, services, MCP tooling, trading flows, and websocket monitoring behavior.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Global fixtures, marker registration, and live gating | `tests/conftest.py` | Shared fixtures, env setup, `--run-live`, and live skip wiring |
| API/router tests | `tests/test_routers.py`, `tests/test_main_sentry.py`, `tests/test_openclaw_callback*.py` | HTTP layer and app lifecycle assertions |
| Service-layer tests | `tests/test_services.py`, `tests/test_*_service*.py` | Business logic and integration boundaries |
| Task and scheduler tests | `tests/test_tasks.py`, `tests/test_daily_scan.py`, `tests/test_kis_tasks.py` | TaskIQ-facing logic checks |
| MCP tool tests | `tests/test_mcp_*.py`, `tests/test_upbit_order_tools.py` | Tool contract and market-behavior coverage |
| Websocket monitor tests | `tests/test_websocket_monitor.py`, `tests/test_*websocket*.py` | Stream/runtime monitor behavior |

## CONVENTIONS
- Canonical pytest config lives in `pyproject.toml` (`[tool.pytest.ini_options]`).
- Enforced settings include strict markers/config. Coverage is explicit-only via `make test-cov` or CI.
- Registered markers are `slow`, `integration`, `unit`, and `live`; use only registered markers.
- `live` marks tests that make external API calls, is always a strict subset of `integration`, and requires `--run-live` to execute.
- Fast-gate selectors use `-m "not live"`; live execution is opt-in only.
- Naming follows `test_*.py` or `*_test.py`, test functions `test_*`, classes `Test*`.
- Default command set comes from `Makefile` (`make test`, `make test-unit`, `make test-integration`, `make test-live`, `make test-cov`, `make test-fast`, `make test-watch`).

## ANTI-PATTERNS
- Do not introduce unregistered markers when strict markers are enabled.
- Do not assert outdated coverage requirements from stale docs; use current `pyproject.toml` threshold.
- Do not mix long-running integration behavior into unit-only test runs without marker guards.
- Do not move core fixtures out of `tests/conftest.py` unless fixture scope/ownership is explicit.

## NOTES
- Root-level one-off websocket test scripts were removed; `blog/test_*.py` remains supplemental and is not part of canonical `tests/` discovery.
- CI runs lint before tests and uses PostgreSQL/Redis services for integration-sensitive coverage.
- When adding new MCP behavior, update both positive and unsupported-market/error-path tests.
