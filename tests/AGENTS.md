# TESTS KNOWLEDGE BASE

## OVERVIEW
`tests/` is the primary pytest suite for API, services, MCP tooling, trading flows, and websocket monitoring behavior.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Global fixtures and marker registration | `tests/conftest.py` | Shared fixtures, env setup, marker definitions |
| Integration test switch | `tests/integration/conftest.py` | Adds `--run-integration` option and integration marker wiring |
| API/router tests | `tests/test_routers.py`, `tests/test_main_sentry.py`, `tests/test_openclaw_callback*.py` | HTTP layer and app lifecycle assertions |
| Service-layer tests | `tests/test_services.py`, `tests/test_*_service*.py` | Business logic and integration boundaries |
| Task and scheduler tests | `tests/test_tasks.py`, `tests/test_daily_scan.py`, `tests/test_kis_tasks.py` | TaskIQ-facing logic checks |
| MCP tool tests | `tests/test_mcp_*.py`, `tests/test_upbit_order_tools.py` | Tool contract and market-behavior coverage |
| Websocket monitor tests | `tests/test_websocket_monitor.py`, `tests/test_*websocket*.py` | Stream/runtime monitor behavior |

## CONVENTIONS
- Canonical pytest config lives in `pyproject.toml` (`[tool.pytest.ini_options]`).
- Enforced settings include strict markers/config and `--cov-fail-under=50`.
- Registered markers are `slow`, `integration`, and `unit`; use only registered markers.
- Naming follows `test_*.py` or `*_test.py`, test functions `test_*`, classes `Test*`.
- Default command set comes from `Makefile` (`make test`, `make test-unit`, `make test-integration`, `make test-cov`).

## ANTI-PATTERNS
- Do not introduce unregistered markers when strict markers are enabled.
- Do not assert outdated coverage requirements from stale docs; use current `pyproject.toml` threshold.
- Do not mix long-running integration behavior into unit-only test runs without marker guards.
- Do not move core fixtures out of `tests/conftest.py` unless fixture scope/ownership is explicit.

## NOTES
- Root-level `test_websocket.py` and `blog/test_*.py` exist outside `tests/`; treat them as supplemental scripts.
- CI runs lint before tests and uses PostgreSQL/Redis services for integration-sensitive coverage.
- When adding new MCP behavior, update both positive and unsupported-market/error-path tests.
