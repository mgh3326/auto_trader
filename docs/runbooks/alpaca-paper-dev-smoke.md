# Alpaca Paper Dev Smoke (Submit → Cancel) — Operator Runbook

Owner: Dev (NOT production ops)
Related issues: ROB-73 / ROB-74 / ROB-72 / ROB-71 / ROB-70 / ROB-69

This runbook covers the dev-owned smoke for the two new MCP tools
`alpaca_paper_submit_order` and `alpaca_paper_cancel_order`. The smoke is
intentionally hard to run with side effects by accident.

## Scope and safety boundary

- Adapter-specific paper-only tools. No live endpoint, no data endpoint as trading base, no generic order route, no bulk cancel.
- Default mode: preview only. No broker mutations, no `submit_order` / `cancel_order` HTTP calls.
- Side-effect mode requires BOTH a CLI flag (`--confirm-paper-side-effect`) AND an env var (`ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1`). Either alone exits with code 2 and zero broker calls.
- Side-effect smoke places ONE tiny PAPER order. Default equity mode uses `AAPL` buy `1` share `limit @ $1.00`; ROB-74 crypto mode is buy-only, limit-only, allowlisted (`BTC/USD`, `ETH/USD`, `SOL/USD`), capped at $50 notional/estimated cost, defaults omitted `--time-in-force` to `gtc`, accepts only `gtc`/`ioc`, and requires an explicit `--limit-price`. If market behaviour fills the order before cancel, mark the result PARTIAL and document in the report.
- ROB-74 candidate reports are operator metadata only: pass the path with `--candidate-report`; the script validates that the file exists and prints only `candidate_report_attached=True`, never report contents or derived trade instructions.
- Never run this from production hosts. This issue is dev-owned smoke.
- Never paste API keys, secrets, Authorization headers, or raw broker payloads.

## Step 1 — Verify environment without printing secrets

```bash
python - <<'PY'
import os
for k in ('ALPACA_PAPER_API_KEY', 'ALPACA_PAPER_API_SECRET'):
    v = os.environ.get(k, '')
    print(f'{k}: present={bool(v)} len={len(v)}')
print('ALPACA_PAPER_BASE_URL=', os.environ.get('ALPACA_PAPER_BASE_URL', '<unset>'))
PY
```

Expected: keys present, base URL unset or exactly `https://paper-api.alpaca.markets`.

## Step 2 — Preview-only smoke (default)

```bash
uv run python scripts/smoke/alpaca_paper_dev_smoke.py
```

Expected output shape:

```text
  [OK] get_account: status=ACTIVE
  [OK] get_cash: cash_set=True
  [OK] submit_order(confirm=False): blocked_reason=confirmation_required
  [OK] cancel_order(confirm=False): blocked_reason=confirmation_required
summary: PASS mode=preview_only
```

Exit code 0 = PASS. Any FAIL line → BLOCKED.

### ROB-74 crypto preview-only smoke

Crypto mode remains preview-only unless the separate operator approval gate is
explicitly unblocked. The operator chooses the allowlisted symbol, limit price,
notional, and optionally `--time-in-force gtc|ioc` (omitted defaults to `gtc`);
the script does not parse candidate reports into orders.

```bash
uv run python scripts/smoke/alpaca_paper_dev_smoke.py \
    --asset-class crypto \
    --symbol BTC/USD \
    --notional 10 \
    --limit-price 50000 \
    --candidate-report /path/to/candidate-report.md
```

Expected output shape:

```text
  [OK] get_account: status=ACTIVE
  [OK] get_cash: cash_set=True
  [OK] submit_order(confirm=False): blocked_reason=confirmation_required
  [OK] cancel_order(confirm=False): blocked_reason=confirmation_required
summary: PASS mode=preview_only asset_class=crypto candidate_report_attached=True
```

The candidate-report path must exist if supplied. Only its presence is printed;
contents, secrets, account payloads, and raw broker responses are not emitted.

## Step 3 — Side-effect smoke (BOTH gates required)

Only run when explicitly authorised on a dev host. For ROB-74 crypto smoke,
include `--asset-class crypto`, an allowlisted `--symbol`, `--notional` at or
below `50`, a crypto-valid `--time-in-force` (`gtc`/`ioc`, or omitted for
`gtc`), and an explicit operator-approved `--limit-price`; do not run this
until 광현님 unblocks the separate paper submit/cancel approval gate.

```bash
ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1 \
    uv run python scripts/smoke/alpaca_paper_dev_smoke.py \
    --confirm-paper-side-effect
```

Expected:

```text
  [OK] get_account: status=ACTIVE
  [OK] submit_order(confirm=True): order_id_len=36 status=accepted
  [OK] cancel_order(confirm=True): read_back=ok final_status=canceled
summary: PASS mode=side_effects
```

- `summary: PARTIAL mode=side_effects` → cancel did not confirm or read-back was unavailable. Investigate but do not retry without re-checking gates.
- `summary: BLOCKED ...` → either gate missing. Re-read Step 3.

## Step 4 — Verify post-smoke state

```bash
uv run python scripts/smoke/alpaca_paper_readonly_smoke.py
```

Expected: open orders count back to baseline (typically 0). The cancelled order may still appear under non-open statuses for a short time.

## Step 5 — Report template (dev → Linear)

```text
ROB-73 dev smoke: PASS|PARTIAL|BLOCKED
mode: preview_only|side_effects
preview_only_exit: <0|1>
side_effect_exit: <0|1|2|skipped>
notes: <redacted exception class only, if any>
safety: paper endpoint only; both gates required for side effects; no secrets printed; no bulk cancel.
```
