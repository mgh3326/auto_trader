# ROB-567 FX Rate MCP Design

## Context

ROB-567 asks for an MCP way to answer the operator question "USD/KRW is lower; how much should I exchange?" without browsing Naver manually. The Linear issue originally mentions spot rate, short-term trends, bank quotes, preferential rates, and later exchange execution.

Code grounding changed the shape of the work: `app/services/exchange_rate_service.py` already provides a production USD/KRW quote path with Toss as the primary source and open.er-api as fallback. It exposes `get_usd_krw_rate_details()` with `rate`, `mid_rate`, `source`, `valid_from`, `valid_until`, `basis_point`, and `rate_change_type`.

## Approved Scope

P1 ships only a read-only MCP primitive:

- `get_fx_rate(pair="USDKRW")`
- Supported pair: USD/KRW only, accepting common spellings such as `USDKRW`, `USD/KRW`, `USD_KRW`, and `USD-KRW`
- Source: existing `get_usd_krw_rate_details()`
- No order mutation, exchange execution, database migration, history ETL, or account-routing changes

Explicitly out of scope:

- 1M/3M trend data
- Bank-specific posted rates
- Toss/KIS preferential effective rate modeling
- US order preview total-cost integration
- Currency conversion execution tools

Those belong to P2/P3 follow-ups or ROB-565 for account-routing and total-cost decisions.

## Design

Add a focused MCP handler module at `app/mcp_server/tooling/fundamentals/_fx_rates.py`. The handler normalizes the pair, rejects unsupported FX pairs with `ValueError`, calls `get_usd_krw_rate_details()`, and returns a stable, JSON-safe payload. Datetime fields are serialized to ISO-8601 strings, and optional upstream fields remain `null` when the provider does not supply them.

Register the tool in the existing fundamentals MCP surface through `app/mcp_server/tooling/fundamentals_handlers.py`, because FX rate lookup is read-only decision context like market indices and valuation tools. Add `get_fx_rate` to `FUNDAMENTALS_TOOL_NAMES` and `app/mcp_server/__init__.py::AVAILABLE_TOOL_NAMES`.

Do not add `USDKRW` to `_INDEX_META` or route FX through `get_market_index`; FX is not an index domain and must keep a separate public tool.

## Response Contract

Example response:

```json
{
  "pair": "USDKRW",
  "base_currency": "USD",
  "quote_currency": "KRW",
  "rate": 1505.7,
  "mid_rate": 1505.4,
  "default_rate": 1505.4,
  "source": "toss",
  "valid_from": "2026-06-15T00:00:00+00:00",
  "valid_until": "2026-06-15T00:01:00+00:00",
  "basis_point": -12.5,
  "rate_change_type": "DOWN"
}
```

`default_rate` mirrors the service's existing scalar consumer behavior and should be used by callers that need one conversion rate.

## Testing

Use focused MCP tests in `tests/test_mcp_fundamentals_tools.py`:

- Handler returns the existing service quote fields as JSON-safe values.
- Pair aliases normalize to `USDKRW`.
- Unsupported pairs raise a clear `ValueError`.
- The registered MCP tool is present on every profile.
- `get_market_index("USDKRW")` continues to fail as an unknown index symbol.

Run the real FastMCP boot smoke test in `tests/test_mcp_tool_registration_boot.py` to catch duplicate tool-name collisions.

## Self-Review

- Spec coverage: matches approved P1 scope and excludes P2/P3/ROB-565 work.
- Placeholder scan: no unresolved placeholders.
- Consistency: public tool name, handler name, supported pair, and response fields are consistent across design and planned tests.
