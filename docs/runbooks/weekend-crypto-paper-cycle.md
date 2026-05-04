# Weekend Crypto Paper Cycle Runner Runbook (ROB-94)

Operator runbook for the weekend crypto Alpaca Paper buy/sell cycle runner MVP.

## Overview

The runner orchestrates a bounded Alpaca Paper crypto roundtrip for weekend preopen signals:

```
plan → buy preview → preflight/packet validate
  → [execute buy] → fill reconcile
  → sell preview → [execute sell] → final reconcile
  → audit report
```

**Default behavior is always dry-run**. Execute requires explicit operator authorization.

## Safety Boundaries

| Boundary | Value |
|----------|-------|
| Max candidates | 3 |
| Max notional per candidate | $10 USD |
| Execution symbols | BTC/USD, ETH/USD, SOL/USD only |
| Order type | limit only |
| Signal venue | Upbit KRW crypto only |
| Execution venue | Alpaca Paper only |
| Asset class | crypto only |

## Dry-Run (Safe, No Broker Mutation)

```bash
uv run python scripts/run_weekend_crypto_paper_cycle.py \
  --dry-run \
  --max-candidates 1 \
  --symbols BTC/USD \
  --print-trace
```

Expected output: JSON report with `"status": "dry_run_ok"` and full stage traces showing `plan`, `buy_preview`, `preflight`, `packet_validate`, and `execute_gate` (skipped).

## Execute Mode

**Requires explicit operator approval and token.**

Prerequisites before execute:
- [ ] Preflight check shows no blocking anomalies
- [ ] Alpaca Paper account has no unexpected open orders or residual positions
- [ ] Approval packets are fresh (expires within 30 minutes)
- [ ] Per-candidate buy and sell approval tokens obtained out-of-band
- [ ] ROB-93 preflight shows `should_block=False`

```bash
export WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN='[REDACTED]'
uv run python scripts/run_weekend_crypto_paper_cycle.py \
  --execute \
  --max-candidates 1 \
  --symbols BTC/USD \
  --print-trace \
  --approval-tokens '{"<candidate_uuid>": "<buy_token>", "<candidate_uuid>:sell": "<sell_token>"}'
```

Expected output: JSON report with `"status": "ok"` or `"partial"` and final lifecycle states reaching `final_reconciled`.

## MCP Tool

The runner is also accessible as an MCP tool:

```
weekend_crypto_paper_cycle_run(
  dry_run=True,          # default: safe
  confirm=False,         # must be True for execute
  max_candidates=3,
  symbols=["BTC/USD"],
  approval_tokens={...}, # required for execute
  operator_token="...",  # required for execute
)
```

## Stop Conditions

Stop the runner and escalate immediately if:

- Preflight returns `should_block=True` for current Alpaca Paper account state
- Any unexpected open orders appear after a buy submit
- Fill readback returns no fill within the bounded smoke window
- Sell source verification fails (buy record not found or quantity mismatch)
- Any execute attempt hits the ledger idempotency gate (duplicate submit guard)

## Rollback / Pause

If a buy was submitted but sell did not complete:

1. Check Alpaca Paper open orders: `alpaca_paper_list_orders(status="open")`
2. Check positions: `alpaca_paper_list_positions()`
3. Cancel open orders if safe: `alpaca_paper_cancel_order(order_id, confirm=True)`
4. Record anomaly via ledger service if needed (through service, never direct SQL)
5. Pull roundtrip report to confirm lifecycle state: `alpaca_paper_roundtrip_report(lifecycle_correlation_id="...")`

## Related Runbooks

- `docs/runbooks/alpaca-paper-ledger.md` — Ledger lifecycle and taxonomy
- `docs/runbooks/alpaca-paper-fill-reconcile-smoke.md` — Fill reconcile smoke
- `docs/runbooks/alpaca-paper-roundtrip-report.md` — Roundtrip audit report

## Safety Checklist (Pre-Execute)

- [ ] Branch rebased on current `origin/main`
- [ ] Dry-run passes cleanly for target symbols
- [ ] Preflight `should_block=False`
- [ ] No residual open orders or positions from prior runs
- [ ] Approval tokens obtained and fresh (not expired)
- [ ] Operator token set as env var (not in shell history)
- [ ] `max_candidates=1` for initial execute smoke
- [ ] Notional confirmed at or below $10
- [ ] Previous roundtrip report reviewed (no unresolved anomalies)
