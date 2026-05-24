# Binance Demo Scalping — Observe-Only Signal Runner (ROB-307 PR1)

PR1 of the Binance Demo scalping MVP: a **read-only, observe-only**
deterministic signal runner plus the risk/order-intent contract and
ledger-backed durable state. **It never places, previews, or tests an
order.** Later PRs add execution (PR2), TP/SL (PR3), and the default-OFF
scheduler scaffold (PR4).

## What PR1 ships

| Module | Responsibility |
|---|---|
| `app/services/brokers/binance/demo_scalping/contract.py` | Risk envelope (§5) + `evaluate_risk` + reason codes |
| `…/signal.py` | Deterministic trend micro-breakout signal |
| `…/market_data.py` | Read-only Demo-host klines/bookTicker adapter (own fail-closed allowlist) |
| `…/ledger_state.py` | Durable `LedgerSnapshot` from `binance_demo_order_ledger` |
| `…/runner.py` | Orchestration → `ObserveOnlyRecord` |
| `scripts/binance_demo_scalping_signal.py` | Observe-only CLI (default-disabled) |

## Strategy (deterministic, tunable)

Trend micro-breakout: **enter long** when `sma_fast(7) > sma_slow(25)`
AND the latest close breaks the prior `breakout_lookback(20)`-bar high.
Futures may take the mirror **short** on downtrend + breakdown; **spot is
long-only** (a spot SELL only closes/reduces a holding). Exits are
fixed-bps TP/SL (`+30 bps` / `-20 bps`). Thresholds live in
`SignalConfig` / `ScalpingRiskLimits`.

## Risk envelope (`ScalpingRiskLimits`, §5)

- Allowlist `XRPUSDT, DOGEUSDT, SOLUSDT`; `BTCUSDT` excluded (Futures
  MIN_NOTIONAL 50 > 10 cap).
- Max notional **10 USDT**/order (sizing floors, never rounds up).
- Caps: one open lifecycle per product+symbol, global open-lifecycle
  cap, daily order-count cap, daily realized-loss budget.
- Gates: spread (bps), data freshness, cooldown — all read from the
  ledger so they survive a fresh process / scheduler run.

## Safety boundaries

- **Demo hosts only**: `demo-api.binance.com` (spot) /
  `demo-fapi.binance.com` (futures), unsigned GETs. The adapter's
  `DEMO_DATA_HOSTS` allowlist fails closed; the live public adapter
  (which permits `api.binance.com`) is **banned** here.
- **No order mutation reachable**: an AST import guard
  (`tests/services/brokers/binance/demo_scalping/test_no_mutation_imports.py`)
  fails the build if the package *or this CLI* imports any execution
  client, signing helper, live-host adapter, ledger-internal repository,
  or the credential resolver.
- **No credentials needed**: the observe path is unsigned read-only.
- **No LLM** in the decision loop.

## Observe-only CLI

Default-disabled — with `BINANCE_DEMO_SCALPING_ENABLED` unset/false it
logs one line and exits 0 with zero side effects.

```bash
# Disabled (default): zero side effects, exit 0
uv run python scripts/binance_demo_scalping_signal.py

# Enabled: one-shot observe over the allowlist (spot)
BINANCE_DEMO_SCALPING_ENABLED=true \
  uv run python scripts/binance_demo_scalping_signal.py \
    --symbols XRPUSDT,DOGEUSDT,SOLUSDT --products spot --interval 1m --limit 50

# Spot + USD-M futures
BINANCE_DEMO_SCALPING_ENABLED=true \
  uv run python scripts/binance_demo_scalping_signal.py \
    --products spot,usdm_futures
```

Each line is an observe-only evidence record: `action` is always
`observe_only`; `would_enter` reports whether signal + risk *would* have
permitted entry (no order is placed). The ledger snapshot requires a DB
(`DATABASE_URL`); the market-data path itself needs neither DB nor creds.

Exit codes: `0` clean/disabled, `1` operator misconfiguration, `2`
runtime failure.

## Order-test filter validation (§5)

Min-notional/LOT_SIZE feasibility under the 10 USDT cap was validated
with the existing non-mutating order-test tooling (signed
`POST /order/test`, no order placed) — see
`docs/runbooks/binance-spot-demo-smoke.md` and
`docs/runbooks/binance-futures-demo-smoke.md`:

```bash
# Spot + Futures, per allowlisted symbol (requires Demo creds)
BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python scripts/binance_spot_demo_smoke.py --order-test --symbol XRPUSDT --cap-usdt 10
BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_futures_demo_smoke.py --order-test --symbol XRPUSDT --cap-usdt 10
```

All of XRP/DOGE/SOL clear MIN_NOTIONAL under the 10 USDT cap on both
products.

## Not in PR1 (later phases)

- Order execution (PR2), TP/SL exit strategy (PR3).
- Scheduler scaffold + kill switch (PR4) — TaskIQ default-OFF; any
  Prefect deployment lives in `robin-prefect-automations` and activation
  is a separate operator gate.
