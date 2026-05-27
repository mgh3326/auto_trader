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

## Execution-evidence gate (ROB-334)

Before any confirmed mock scalping run (`KIS_MOCK_SCALPING_WS_CONFIRM=true`), the
fill-evidence path below must be available; otherwise the executor fails closed
(no fabricated fill) and records an `entry_unfilled` / `exit_unconfirmed` anomaly.

**Authoritative source:** KIS daily order-execution inquiry
`inquire_daily_order_domestic(is_mock=True)`. Holdings/cash delta (ROB-102)
remains secondary. `inquire_korea_orders` (TTTC8036R, pending inquiry) is
**live-only** and is never used in mock.

**Deferred gap:** the execution-notice WebSocket `H0STCNI9` (실시간 체결통보) is
NOT implemented (requires an AES-CBC-decrypted, HTS-ID handshake frame path). It
is a fail-closed, documented gap and a candidate follow-up issue.

### Read-only preflight (no order submission)

```bash
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_fill_evidence_smoke \
    --order-no <ODNO> --symbol <KR_CODE>
```

Required env (names only — never echo values): `KIS_MOCK_APP_KEY`,
`KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`.

Expected success signal: exit `0`, a printed `verdict=...` line, and the
observed `row keys: [...]` (use these to confirm/tighten the classifier's
candidate field names).

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

### Confirmed one-off mock smoke (operator-gated, NOT run by this change)

The operator-approved bounded confirmed mock smoke (one minimal KRX limit order
round-trip) is a **separate, operator-gated step**. It is deferred here:
this change ships code + runbook + tests + the read-only preflight only.

Rollback / no-op: all additions are read-only or fail-closed. Reverting the PR
restores the prior `confirm_fill` stub (always-unfilled). No migration, no
scheduler, no env mutation.
