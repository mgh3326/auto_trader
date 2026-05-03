# Alpaca Paper fill/reconcile smoke (ROB-85)

This runbook covers the bounded ROB-85 manual smoke for a single Alpaca Paper
crypto buy-limit order and ledger reconciliation. It is intentionally not a
scheduler, strategy, sell/close flow, generic order route, or live-trading flow.

## Safety boundary

- Execution venue: Alpaca Paper only.
- Signal venue: Upbit KRW crypto symbols only (`KRW-BTC`, `KRW-ETH`, `KRW-SOL`).
- Execution symbol is derived by code (`KRW-BTC -> BTC/USD`, etc.); do not pass
  Upbit symbols into Alpaca order tools.
- Order shape: crypto `buy` + `limit` only, `gtc` or `ioc`.
- Maximum ROB-85 smoke notional: `$10`.
- At most one smoke order/position at a time for the execution symbol.
- Preview and `confirm=False` validation must pass before execute mode can call
  `alpaca_paper_submit_order(..., confirm=True)`.
- The execute path submits exactly one order and reconciles only the returned
  broker order id.
- If polling leaves the order open, record `open_after_poll_timeout` and stop.
  ROB-85 does not auto-cancel.
- Sell/close is out of scope for ROB-85.
- No live/generic/KIS/Upbit mutation, bulk cancel, watch alert, order intent,
  scheduler change, or direct SQL is part of this smoke.

## Dry-run validation (no broker mutation)

Use a USD Alpaca execution limit supplied by the operator. Do not derive it from
Upbit KRW prices.

```bash
uv run python scripts/smoke/alpaca_paper_fill_reconcile_smoke.py \
  --signal-symbol KRW-BTC \
  --notional 10 \
  --limit-price-usd <operator-supplied-usd-limit> \
  --time-in-force gtc \
  --client-order-id rob85-fill-YYYYMMDDHHMMSS
```

Dry-run performs:

1. Read-only account/cash/open-orders/positions/fills preflight.
2. `alpaca_paper_preview_order(...)` for the exact payload.
3. `alpaca_paper_submit_order(..., confirm=False)` and requires
   `blocked_reason=confirmation_required`.
4. Operator-safe summary only.

It does not write the ledger unless `--record-preview` is provided.

## Optional preview ledger record

```bash
uv run python scripts/smoke/alpaca_paper_fill_reconcile_smoke.py \
  --record-preview \
  --signal-symbol KRW-BTC \
  --notional 10 \
  --limit-price-usd <operator-supplied-usd-limit> \
  --client-order-id rob85-fill-YYYYMMDDHHMMSS
```

This writes only through `AlpacaPaperLedgerService.record_preview(...)` after
preview and confirm-false validation pass. It does not submit an order.

## Execute template (review/operator gate required)

Run execute mode only after code review confirms the standing ROB-85 policy
applies and the operator has supplied the exact USD limit.

```bash
uv run python scripts/smoke/alpaca_paper_fill_reconcile_smoke.py \
  --execute \
  --signal-symbol KRW-BTC \
  --notional 10 \
  --limit-price-usd <operator-supplied-marketable-usd-limit> \
  --time-in-force gtc \
  --client-order-id rob85-fill-YYYYMMDDHHMMSS
```

Execute mode performs the dry-run gates in the same process, records the preview,
submits exactly one Alpaca Paper order with `confirm=True`, polls only the
returned order id, lists fills after submit, filters positions by execution
symbol, then records submit/status/position/reconcile lifecycle through
`AlpacaPaperLedgerService`.

## Stop conditions

Stop before broker mutation when any of these is true:

- Unsupported signal symbol.
- Missing or invalid `--limit-price-usd`.
- Notional exceeds `$10`.
- Account status is known and not active.
- Buying power is below requested notional.
- Any open order exists for the execution symbol.
- Any existing position exists for the execution symbol.
- Preview fails or reports `would_exceed_buying_power=True`.
- Confirm-false validation does not block with `confirmation_required`.
- Returned submitted order lacks an id or client_order_id does not match.
- Any requested next step requires sell/close, cancel, live/generic routing,
  scheduler changes, direct SQL, or bulk/by-symbol/by-status cancellation.

## Report fields

The script prints an audit summary with:

- mode (`dry-run` or `execute`);
- signal venue/symbol and execution venue/symbol;
- client_order_id;
- requested notional and USD limit;
- summarized preflight counts and buying-power sufficiency;
- preview/confirm-false validation status;
- when executed: final order status, fill count, position present/qty, and
  reconcile status;
- explicit side-effect boundary statement.

The report does not print credentials, Authorization headers, account ids, raw
account payloads, or full broker payloads.
