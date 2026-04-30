# ROB-57 Review Report

**Decision:** review_passed

## Summary

The implementation faithfully follows the plan in `docs/plans/ROB-57-alpaca-paper-service-foundation-plan.md`. It introduces an isolated `app/services/brokers/alpaca/` package with paper-only configuration, a typed `AlpacaPaperBrokerProtocol`, a service implementation with injected `HTTPTransport`, and four hermetic unit-test files (23 tests passing). The only edit to a pre-existing file is an additive block in `app/core/config.py`. Lint, format, and type checks all pass.

## Must-fix items

None.

## Safety invariant assessment

| Invariant | Enforced in code | Enforced in tests | Verdict |
|---|---|---|---|
| **I1** Trading base URL is exactly `https://paper-api.alpaca.markets` | `service.py` constructor + `config.py` validator | `test_settings_default_paper_base_url_is_paper_api`, `test_service_init_accepts_paper_endpoint` | ✅ |
| **I2** Live endpoint never usable for trading | `FORBIDDEN_TRADING_BASE_URLS` rejection in `service.py` + settings validator | `test_settings_rejects_live_trading_base_url`, `test_service_init_rejects_live_endpoint` | ✅ |
| **I3** Data endpoint never usable as trading base | `FORBIDDEN_TRADING_BASE_URLS` rejection + settings validator | `test_settings_rejects_data_endpoint_as_trading_base_url`, `test_service_init_rejects_data_endpoint_as_trading_base` | ✅ |
| **I4** No paper→live fallback | No `live_*`/`fallback_*` attribute exists; no env-driven branch | `test_service_has_no_live_fallback_attribute`, `test_no_alpaca_live_settings_field` | ✅ |
| **I5** No real network in tests | All tests inject `AsyncMock` transport; no real `httpx.AsyncClient` instantiation in tests | All 23 tests offline | ✅ |
| **I6** No router/MCP/Hermes exposure | `__init__.py` exports only the adapter; no consumer imports added | `test_no_router_imports_alpaca_paper`, `test_no_mcp_tool_imports_alpaca_paper`, `test_no_hermes_profile_imports_alpaca_paper` | ✅ |
| **I7** Settings load fails fast on bad URL | `field_validator(mode="before")` raises before service construction | `test_settings_rejects_*` cases | ✅ |

The validator in `app/core/config.py` is appropriately strict — it rejects both the explicit forbidden set and any value that is not exactly the paper URL, providing belt-and-suspenders enforcement alongside the service-layer guard.

## Service interface coverage

All required surface methods are present and tested with mocked transports:
- account → `get_account` (`test_get_account_returns_snapshot`)
- cash → `get_cash` (`test_get_cash_returns_cash_balance`)
- positions → `list_positions` (`test_list_positions_parses_array`, `test_list_positions_empty`)
- assets → `list_assets` (`test_list_assets_passes_status_and_class_query`)
- orders → `submit_order`, `list_orders`, `get_order`, `cancel_order`
- fills → `list_fills` (`test_list_fills_uses_activities_executions_endpoint`)
- error wrapping → `test_request_error_wraps_http_error`

## ROB-56 conflict assessment

**No conflict.** Verified the patch touches none of the ROB-56 files:
- `app/models/paper_trading.py`, `app/services/paper_trading_service.py`, `app/services/sell_signal_service.py` — untouched
- `app/services/brokers/kis/{client,constants,domestic_market_data}.py` — untouched
- `app/mcp_server/tooling/{fundamentals/_valuation,fundamentals_handlers,paper_analytics_registration}.py` — untouched
- `app/routers/n8n.py`, `app/schemas/n8n/sell_signal.py` — untouched
- `tests/test_{mcp_fundamentals_tools,paper_analytics_tools,paper_trading_service}.py` — untouched

The only pre-existing file edit is `app/core/config.py`, which ROB-56 did not modify, and the change is purely additive (new fields + one validator). Conflict gate satisfied.

## Suggested follow-ups (non-blocking)

1. **Decimal JSON serialization in `submit_order`**: `OrderRequest.model_dump(exclude_none=True)` returns `Decimal` instances; the default `httpx` JSON encoder cannot serialize them. Tests pass because the transport is mocked. Consider `model_dump(mode="json")` (or a custom encoder) before this is wired into a real call path. Defer to F2/F3.
2. **HttpxTransport client lifetime**: `HttpxTransport.request` instantiates a fresh `AsyncClient` per request. Acceptable for foundation but inefficient; consider a long-lived client with proper async context management when the adapter graduates beyond foundation.
3. **`_request` exception wrapping**: only `httpx.HTTPError` is caught and wrapped. Unexpected runtime errors during transport (e.g. JSON decode of malformed bodies) propagate raw. Consider broadening or adding a separate decode path. Low priority while no consumer exists.
4. **Test env isolation**: `tests/test_alpaca_paper_config.py` uses `patch.dict(os.environ, ..., clear=False)`. If a developer has `ALPACA_PAPER_*` set locally with conflicting values it could mask a regression. Switching to `clear=True` with a fully-specified base env would harden these tests. Cosmetic.
5. **`alpaca_paper_data_base_url`**: the field is declared but currently unused. That is consistent with the plan (data client is F5), but a brief comment or `# noqa: TODO(ROB-57.F5)` marker might make intent clearer to future readers. Optional.
6. **Hermes scan scope**: `test_no_hermes_profile_imports_alpaca_paper` walks only `app/mcp_server/tooling/` files matching certain keywords. If Hermes profile registration ever moves outside this dir, the guard would silently stop covering it. Worth a comment recording the assumption.

None of the above block merge.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-57
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-57-review-report.md
AOE_NEXT: create_pr
