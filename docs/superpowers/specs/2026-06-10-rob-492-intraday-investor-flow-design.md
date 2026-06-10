# ROB-492 Intraday Investor Flow Design

## Goal

Expose same-day, intraday provisional foreign/institution net-buy flow for a
specific Korean symbol through MCP, without mixing it into the existing
confirmed daily `get_investor_trends` history.

## Source Verification

The current `get_investor_trends` path uses Naver daily investor history. KIS
`inquire-investor` (`FHKST01010900`) is not enough for ROB-492 because the KIS
sample documentation states that same-day data is provided after market close.

The matching KIS source is:

- API name: `종목별 외인기관 추정가집계[v1_국내주식-046]`
- URL: `/uapi/domestic-stock/v1/quotations/investor-trend-estimate`
- Real domain: `https://openapi.koreainvestment.com:9443`
- Mock trading: unsupported
- TR ID: `HHPTJ04160200`
- Request body/query field: `MKSC_SHRN_ISCD`
- Response array: `output2[]`
- Response fields:
  - `bsop_hour_gb`: input slot, `1=09:30`, `2=10:00`, `3=11:20`, `4=13:20`, `5=14:30`
  - `frgn_fake_ntby_qty`: foreign provisional net-buy quantity
  - `orgn_fake_ntby_qty`: institution provisional net-buy quantity
  - `sum_fake_ntby_qty`: combined provisional net-buy quantity

KIS describes this data as a simple cumulative intraday estimate entered by
brokerage staff. Foreign input slots are 09:30, 11:20, 13:20, 14:30; institution
input slots are 10:00, 11:20, 13:20, 14:30; these times may vary.

## Public MCP Contract

Add a new tool, `get_intraday_investor_flow`, instead of adding
`include_intraday` to `get_investor_trends`.

Rationale:

- `get_investor_trends` is confirmed historical data and supports day/week/month
  aggregation.
- The new KIS source is same-day provisional data with different semantics,
  update cadence, and reliability caveats.
- A separate tool lets callers see `provisional: true`,
  `data_state: "intraday_provisional"`, and `as_of` without confusing the daily
  trend series.

Input:

```python
symbol: str
```

Only 6-digit Korean equity symbols are accepted.

Success response:

```json
{
  "symbol": "000660",
  "instrument_type": "equity_kr",
  "source": "kis",
  "data_state": "intraday_provisional",
  "market_session_state": "fresh",
  "provisional": true,
  "as_of": "2026-06-10T14:30:00+09:00",
  "as_of_time_kst": "14:30",
  "foreign_net_qty": -120000,
  "institution_net_qty": 50000,
  "combined_net_qty": -70000,
  "rows": [
    {
      "slot": "1",
      "as_of_time_kst": "09:30",
      "foreign_net_qty": -10000,
      "institution_net_qty": null,
      "combined_net_qty": -10000
    }
  ],
  "note": "KIS investor-trend-estimate is intraday provisional cumulative input, not a confirmed daily close figure."
}
```

`as_of` is derived from KST request date plus the latest returned
`bsop_hour_gb` slot. The KIS payload does not include a date field, so the
response also exposes `as_of_time_kst` and `market_session_state` to make that
inference visible.

Error behavior:

- Empty or non-KR symbol: raise `ValueError`, consistent with
  `get_investor_trends`.
- KIS call failure: return `error_payload(source="kis", symbol=symbol,
  instrument_type="equity_kr")`.
- No valid rows: return a success-shaped payload with `rows: []`,
  `as_of: null`, quantity fields `null`, and a note that no provisional rows
  were returned.

## Code Boundaries

Add KIS constants and a thin KIS client wrapper:

- `app/services/brokers/kis/constants.py`
- `app/services/brokers/kis/domestic_market_data.py`
- `app/services/brokers/kis/client.py`

Add a focused MCP handler module:

- `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py`

Register it in:

- `app/mcp_server/tooling/fundamentals_handlers.py`
- `app/mcp_server/__init__.py`

Update public docs:

- `app/mcp_server/README.md`

## Testing Strategy

Unit tests should cover:

- KIS wrapper URL, TR ID, and `MKSC_SHRN_ISCD` parameter.
- KIS wrapper accepts `output2` list and returns only dict rows.
- MCP handler maps KIS raw fields into normalized response fields.
- MCP handler chooses the latest slot by `bsop_hour_gb`.
- MCP handler rejects non-KR symbols.
- MCP handler returns an error payload for upstream KIS failures.
- Tool registration exposes `get_intraday_investor_flow`.

No DB migration, scheduler, recurring ingestion, live trading, order approval,
or strategy policy change is part of ROB-492.
