# KIS mock scalping smoke (ROB-321 PR4)

Operator runbook for the KIS official-mock (`account_mode=kis_mock`) scalping
loop: read-only quote WebSocket → signal/risk → monitored round-trip executor →
round-trip ledger. **Mock-only, default-disabled, dry-run by default.** Edge is
an intentional toy (ROB-316: scalping is net-negative after fees) — this loop
validates the *execution plumbing*, not a profitable strategy.

## Components (where the loop lives)

| Stage | Module |
|-------|--------|
| Quote WS (read-only) | `app/services/brokers/kis/mock_scalping_ws/market_stream.py` |
| Signal / risk / intent | `app/services/brokers/kis/mock_scalping/{signal,contract,order_intent}.py` |
| Supervisor (candle→trigger) | `app/services/brokers/kis/mock_scalping_ws/supervisor.py` |
| Exec bridge | `app/services/brokers/kis/mock_scalping_exec/ws_bridge.py` |
| Executor (TP/SL/time-stop) | `app/services/brokers/kis/mock_scalping_exec/executor.py` |
| Broker/ledger adapters | `app/services/brokers/kis/mock_scalping_exec/adapters.py` |
| Round-trip ledger | `review.kis_mock_order_ledger` (+ `20260526_rob321_p4a` migration) |
| Daemon | `scripts/kis_mock_scalping_daemon.py` |

## Gates (all default off)

| env | effect |
|-----|--------|
| `KIS_MOCK_SCALPING_ENABLED` | allows the `ScalpingExitContext` sell-guard bypass (mock-only) |
| `KIS_MOCK_SCALPING_WS_ENABLED` | runs the daemon at all (else no-op, exit 0) |
| `KIS_MOCK_SCALPING_WS_CONFIRM` | submits real mock orders (else preview/dry-run only) |

## Safety boundaries

- Market data is read-only; orders are mock-only via `_place_order_impl(is_mock=True, scalping_exit=…)`. Live order/guard paths are never touched (PR1 regression test).
- `ScalpingExitContext` (stop-loss below the avg*1.01 floor + current-price guard) is **fail-closed**: only for `is_mock=True` + `KIS_MOCK_SCALPING_ENABLED`.
- The executor never reports a clean success without a proven exit fill — entry-unfilled / exit-unconfirmed → `anomaly`.
- No scheduler/Prefect registration; the daemon runs only when launched manually.

## Step 0 — apply the migration (operator)

The round-trip ledger columns ship as a migration but are **not auto-applied**:

```bash
uv run alembic upgrade head   # applies 20260526_rob321_p4a (additive, nullable)
```

## Step 1 — dry-run / check-only (no orders)

```bash
# daemon stays a no-op until enabled
uv run python -m scripts.kis_mock_scalping_daemon

# enabled, dry-run (WS_CONFIRM unset) → triggers preview only, no orders/ledger
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_daemon \
    --symbols 005930,000660 --account-mode kis_mock --max-seconds 60
```

Expect: connects, logs candle/trigger activity, executor returns `dry_run` per
trigger, **zero rows** written to `review.kis_mock_order_ledger`.

## Step 2 — small confirm mock run (operator-gated)

> **Fill Evidence Gate wired (ROB-334).** `KisMockBroker.confirm_fill` is now driven by
> KIS daily order-execution inquiry, fail-closed. Run the preflight smoke below before
> trusting confirm mode.

```bash
KIS_MOCK_SCALPING_ENABLED=true \
KIS_MOCK_SCALPING_WS_ENABLED=true \
KIS_MOCK_SCALPING_WS_CONFIRM=true \
uv run python -m scripts.kis_mock_scalping_daemon \
    --symbols 005930 --account-mode kis_mock --max-seconds 120 --max-triggers 1
```

## Step 3 — post-run verification

```sql
-- round-trip rows (entry + exit share correlation_id; exit carries PnL)
SELECT correlation_id, scalping_role, side, lifecycle_state, exit_reason,
       gross_pnl, net_pnl, fee
FROM review.kis_mock_order_ledger
WHERE strategy = 'kis-mock-v1'
ORDER BY created_at DESC LIMIT 20;
```

Verify:
- a clean round trip = one `entry` (lifecycle `fill`) + one `exit` (lifecycle `reconciled`) with the same `correlation_id`, and `net_pnl = gross_pnl - fee`;
- no unexpected `anomaly` rows (an `anomaly` means the exit fill could not be proven — investigate, do not assume closed);
- no orphan open positions in the KIS mock account / pending list.

## Open question this loop still carries

Does the KIS **mock** WS (`:31000`) serve real-time quotes, or must quotes come
from **live** (`:21000`)? Resolved by the read-only quote smoke
(`scripts/kis_mock_scalping_ws_smoke.py`, see `kis-mock-scalping-ws-smoke.md`):
`kis_mock` delivered both orderbook and trade frames during the 2026-05-27 KRX
regular session, so the domestic mock scalping loop can use `--account-mode
kis_mock` for quote smoke. Keep live quote WS as a fallback only if a future
mock-session smoke returns exit 4 during market hours.

---

## Execution-evidence gate (ROB-334, revised by ROB-341)

Before any confirmed mock scalping run (`KIS_MOCK_SCALPING_WS_CONFIRM=true`), the
fill-evidence path below must be available; otherwise the executor fails closed
(no fabricated fill) and records an `entry_unfilled` / `exit_unconfirmed` anomaly.

**Primary same-day source (ROB-341):** the baseline-vs-post **holdings delta**
read from `account.fetch_domestic_balance_snapshot(is_mock=True)` (load-bearing),
corroborated by the **cash delta** (`dnca_tot_amt`), which also derives the fill
price (`|Δcash| / qty`, falling back to the submitted limit price). The baseline
holdings+cash snapshot is captured immediately before each submit and stamped
into the submit result; `confirm_fill` then polls a post-submit snapshot and
classifies the delta via the shared `classify_fill_by_delta` kernel (the same
kernel the ROB-102 reconciler uses). Ambiguous, zero, or wrong-direction deltas,
a missing baseline, or a snapshot read failure all fail closed.

**daily-ccld is NOT the primary same-day signal (ROB-341).** KIS official mock
`inquire_daily_order_domestic` / daily-ccld can return `rt_cd=0` with **empty
rows even after same-day mock order activity**, so an empty same-day daily-ccld
result must never be read as "no fill" and can neither gate nor override the
holdings verdict. daily-ccld is retained only as a **supplementary,
post-settlement diagnostic** (`KisMockBroker.poll_daily_ccld_diagnostic`), whose
empty same-day result is classified clearly (`pending` / `no_matching_order`).
`inquire_korea_orders` (TTTC8036R, pending inquiry) is **live-only**, never mock.

**Deferred gap (follow-up):** the execution-notice WebSocket `H0STCNI9`
(실시간 체결통보) is NOT implemented (requires an AES-CBC-decrypted, HTS-ID
handshake frame path). It is a fail-closed, documented gap and remains an
explicit follow-up — out of ROB-341 scope.

### Read-only preflight (no order submission, ROB-341)

```bash
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_holdings_delta_smoke \
    --preflight --symbol <KR_CODE>
```

Required env (names only — never echo values): `KIS_MOCK_APP_KEY`,
`KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`. Expected success signal: exit `0`
and a printed JSON line with `holdings_qty` + `cash_dnca_tot_amt` for the symbol.
If the snapshot read fails (exit `2`), stop — the primary same-day path is
unavailable.

The legacy daily-ccld field-name probe
(`scripts/kis_mock_fill_evidence_smoke.py --order-no <ODNO>`) is still available
for **post-settlement** diagnostics only; it is no longer the same-day gate.

### Bounded confirmed smoke (operator-gated, ROB-341)

Run ONLY after the read-only preflight passes and operator approval boundaries
are satisfied, during a KRX regular session:

```bash
KIS_MOCK_SCALPING_ENABLED=true KIS_MOCK_SCALPING_WS_ENABLED=true \
    uv run python -m scripts.kis_mock_holdings_delta_smoke \
    --confirm --symbol <KR_CODE> --notional-krw 10000
```

The cleanup SELL flattens through the mock scalping-exit bypass, so `--confirm`
requires **both** `KIS_MOCK_SCALPING_ENABLED=true` (the sell-guard bypass) and
`KIS_MOCK_SCALPING_WS_ENABLED=true`. **Set both ephemerally for this run only —
never as persistent env/shell exports.** The cleanup exit reason defaults to
`stop_loss` (an existing allowed `ScalpingExitContext` reason — ROB-358 does not
add a smoke-only reason); override with `--cleanup-reason {stop_loss,take_profit,time_stop}`
only if deliberately needed.

Both gates (plus the cleanup reason) are **preflighted before any BUY** (ROB-358):
if `KIS_MOCK_SCALPING_ENABLED` is unset or the cleanup reason is not an allowed
`ScalpingExitContext` reason, the run stops with exit `4` and **no position is
acquired** — it never buys something it cannot flatten.

Places one small marketable limit BUY (at best ask), confirms the fill via the
holdings/cash delta, then flattens with a cleanup SELL (at best bid) back to
baseline. Prints a JSON evidence packet: symbol, side(s), order id(s), baseline
holdings/cash, post-submit holdings/cash, confirmation signal + price source,
cleanup result, and final position delta vs baseline. A clean exit `0` requires
`final_position_delta_vs_baseline` to be **exactly `0`** — any non-zero delta is
an anomaly. Exit `2` if the fill could not be confirmed in the poll window
(ROB-341 STOP condition — capture the packet and report; do not force), `3` if
the position could not be returned to baseline, including:
- a residual position remaining (`final delta > 0`);
- an over-flatten / below-baseline drop (`final delta < 0`, e.g. sold past
  baseline or holdings dropped before the cleanup SELL) — `cleanup` =
  `over_flattened_anomaly` / `below_baseline_anomaly`;
- a cleanup SELL the broker **rejected** or one that returned no `odno`/`order_no`.

All exit-3 cases surface `cleanup_error` + the non-zero
`final_position_delta_vs_baseline` (never a silent exit 1). Exit `4` disabled/not
configured **or a cleanup preflight gate failure (no order placed)**.

### Failure categories

| category | meaning | operator action |
|---|---|---|
| `code` | parse/classifier fault, unexpected response | file a bug with the redacted detail |
| `env/config` | mock creds/account missing or gate off | set the named env vars; do not commit secrets |
| `data-precondition` | not regular session / no matching order / no odno | run during KRX session after a real mock order |
| `unsupported mock API` | the daily-execution inquiry is rejected in mock | stop; the authoritative path is unavailable |
| `operator approval needed` | confirmed run attempted without approval | obtain explicit operator approval first |

Exit codes: `0` ok · `2` inquiry error / unsupported · `4` disabled or not
configured · `1` unexpected.

Rollback / no-op: all additions are read-only or fail-closed. Reverting the
ROB-341 PR restores the prior daily-ccld-based `confirm_fill` (which fails closed
on the empty same-day mock rows this change works around). No migration, no
scheduler, no env mutation.
