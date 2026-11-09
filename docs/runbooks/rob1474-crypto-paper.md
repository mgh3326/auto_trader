# ROB-1474 crypto paper episodes

Manual CLI only. No scheduler, deployment, exchange smoke or migration application
is part of this change. The operator owns activation and Dagu registration after merge.

The strategy freezes `relative_volatility_20`, with the low-volatility orientation
from `research/stock_selection/models.py`, for horizons 1/3/5/10 (default 3).
The #2271 report table selected this baseline for each crypto horizon. Those ICs
are descriptive Upbit survivor-sample diagnostics, **not Binance validation**.
Paper results never select or tune a baseline. N is fixed at ten per direction.

## Inputs and time

`--panel-root` is an operator-owned `research/crypto_universe` output root. It must
contain `universe/manifest.json`, its hash-bound PIT table, candle index and
Parquet files, including delisted history. The loader verifies hashes and row
counts, preserves historical listing episodes, and applies the shared
`research.crypto_universe.exclusions` policy at the requested completed-candle
cutoff. It excludes known stable bases, any base whose last 30 completed daily
closes all stayed within ±2% of 1.00 USDT, explicit wrapped duplicates (WBTC,
BETH, WBETH), and leveraged tokens. Rule evaluation uses only closes at or
before the cutoff; missing sessions do not prove a peg. `PAXG` stays eligible.
`daily_lists` provenance records per-symbol reason codes (`stable_list`,
`stable_peg_rule`, `wrapped_duplicate`, and `leveraged_token`). Static entries
include BFUSD and XUSD; source citations and the detailed thresholds are in
`docs/runbooks/rob1474-mock-data-refresh.md`. The loader uses native USDT
turnover and BTCUSDT as the benchmark. It never fetches prices or extends the
panel's declared coverage. Missing benchmark history, insufficient eligible
names, corrupt files and dates past coverage fail closed.
Update the panel with the existing research collector before invoking this CLI.

For `run`, `--asof D` labels the completed UTC candle D. The existing research
availability convention permits its decision at D+1 **00:05 UTC**. A confirmed
run must be within the following 24 hours. Entries are due three calendar days
after that decision by default; weekends count. `close-due --asof D` uses D
00:05 UTC as its cutoff, matching that day's decision time. Future cutoffs are
refused. Run/close are separate invocations.

## Boundaries and activation

The explicit operator decision
`decision/2026-11-02/mock-trading-all-venues-no-caps` removes paper/demo size
ceilings for this runner. Default notional is 1,000 USDT per name; Alpaca mirror
sizes the corresponding USD pair from the demo USDT reference price (USD/USDT
parity is an assumption, not an FX conversion). Venue minima/maxima and quantity
steps still apply, with floor-only rounding. Unsupported names are reported and
not replaced by lower-ranked names. ROB-993/H5/smoke strategy limits are unchanged.

- All mutations require `MOCK_STRATEGY_ENABLED=true` and `--confirm`, plus the
  existing venue gate: `BINANCE_SPOT_DEMO_ENABLED`,
  `BINANCE_FUTURES_DEMO_ENABLED` or `ALPACA_PAPER_CRYPTO_ENABLED`.
- Binance uses the existing demo credential resolvers and clients. Exact HTTPS
  bases remain `https://demo-api.binance.com` and
  `https://demo-fapi.binance.com`. Redirects remain disabled. Configure the
  corresponding `BINANCE_SPOT_DEMO_EXPECTED_FINGERPRINT` or
  `BINANCE_FUTURES_DEMO_EXPECTED_FINGERPRINT` with the operator's expected
  `demo_credential_fingerprint` value. This binds credentials, not a new account.
- Futures must already be isolated **1x**, one-way. The runner reads back all
  three settings; missing or conflicting evidence blocks submission. It never
  changes leverage, margin type or position mode. A preexisting long blocks it.
  Covers always use `reduceOnly=true`, `positionSide=BOTH`.
- Alpaca uses the existing `clean` profile. Both configured expected account ID
  and account number suffixes are required and checked before broker operations.
  It also needs the Spot Demo gate/credentials/fingerprint for reference pricing.
  Only active, tradable crypto USD assets with explicit min-size/increment data
  are eligible. It cannot route to live. Strategy attribution lives in the DB.

Alpaca asset increments come from the [Assets contract](https://docs.alpaca.markets/us/reference/get-v2-assets-1).
Its market orders use `gtc`, as specified in the
[crypto spot order contract](https://docs.alpaca.markets/us/docs/crypto-trading).

## Commands

After the operator applies the shared US migration and configures the gates:

```bash
uv run python -m scripts.mock_strategy run --venue binance_spot_demo --asof 2026-11-02 --panel-root /operator/panel --confirm
uv run python -m scripts.mock_strategy run --venue binance_futures_demo --asof 2026-11-02 --panel-root /operator/panel --confirm
uv run python -m scripts.mock_strategy run --venue alpaca_paper_crypto --asof 2026-11-02 --panel-root /operator/panel --confirm
uv run python -m scripts.mock_strategy close-due --venue binance_futures_demo --asof 2026-11-06 --confirm
uv run python -m scripts.mock_strategy status --venue binance_spot_demo --asof 2026-11-06
```

These dates illustrate arguments; they do not authorize execution. Use the
current completed candle for a real run and a nonfuture cutoff for closes.
`status` only reads the shared attribution table. `close-due --dry-run` requires
`--account-ref` and lists due entries from DB without broker calls or writes;
it does not claim their quantities are executable.

An offline fixture run requires no gates, credentials, database or exchange:

```bash
uv run python -m tests.services.mock_strategy.crypto_fixture --root /tmp/rob1474-crypto-fixture
uv run python -m scripts.mock_strategy run --venue binance_spot_demo --asof 2023-12-31 --panel-root /tmp/rob1474-crypto-fixture --market-fixture /tmp/rob1474-crypto-fixture/market.json --dry-run
```

The fixture is synthetic and cannot submit or provide fill evidence. Omit it
only for a confirmed execution; a `run --dry-run` requires local fixtures.

## Persistence, replay and recovery

All writes go through the shared `MockStrategyRecorder` in the US PR. One account
advisory lock serializes this CLI's book; other manual clients are outside that
lock. This is not exchange fencing. Deterministic IDs include strategy, venue,
account, candle date and symbol. A dated batch stores its full signal membership,
input-prefix hash, target notional and executable membership before any POST.
Reruns reuse those intents even if prices moved; revised lists cannot open a
second batch. `planned -> sending` commits before one broker call. `sending` or
`uncertain` never permits a resend, even after a missing lookup or crash.

Read-only eligibility checks run before claiming, so a temporary refusal leaves
the intent planned. The adapter repeats those checks immediately before its POST.
Only its typed pre-dispatch refusal proves that POST was never called: after a
claim this records rejected with `no_order_sent=true`, and a close may later use
a fresh indexed leg. Generic errors after entering the broker call remain
ambiguous; their keys are never reused. One episode's unavailable evidence is
reported unresolved while other due episodes continue. Cancellation and process
control exceptions propagate even if the best-effort outcome write fails.
`opened_at` records the first observed fill time and is not replaced by refreshes.

Accepted is not filled. Order ID/client ID, symbol, side, size, order type and
futures flags must match broker evidence. Due entries are refreshed first and
only terminal-entry proven fills may close. Spot buy commissions in base units
are subtracted using that order's complete `myTrades` evidence. Missing or
truncated trade coverage blocks attribution. Only the episode's quantity is
closed, bounded by fresh sellable/short quantity. No account-wide liquidation.

Each close leg has its own durable claim. Nonterminal or uncertain legs block a
new one. Terminal partial closes permit a new deterministic leg only for the
proven residual. Step/min-notional dust stays open as `dust_remaining`, with
`residual_qty` recorded. Realized P&L is gross on **closed quantity only**, excludes
fees/funding/FX, and is not a net-return claim. An unavailable/rejected close
does not mark the entry closed. Review blocked/uncertain entries manually using
the exact broker order/client ID; never reset their status to planned.
