# ROB-1474 US paper list strategy

Run the frozen US up/down lists through Alpaca paper and retain strategy-attributed
entry, exit and fill evidence. This is a prospective paper experiment, with no
market-wide readiness claim from the bounded survivor-only research report.

## Deployment prerequisites (operator only)

Apply the additive migration `20261102_rob1474_mock_orders` after merge using the
normal migration runner. It creates `review.mock_strategy_orders`, its unique
client-order IDs, journal guards, and conditional `at_app` grants. Workers do not
apply migrations, deploy or send smoke orders.

The paper process needs these configuration keys (values stay in the operator's
secret environment): `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`,
`ALPACA_PAPER_EXPECTED_ACCOUNT_ID_SUFFIX`, and
`ALPACA_PAPER_EXPECTED_ACCOUNT_NUMBER_SUFFIX`. Both identity suffixes are mandatory
for this CLI; they cannot be empty. `ALPACA_PAPER_BASE_URL` must be exactly
`https://paper-api.alpaca.markets`. The existing physical-account check runs before
other endpoints. The recorder stores a hash-based account reference, not credentials.

Both `MOCK_STRATEGY_US_ENABLED` and `ALPACA_PAPER_AUTOMATED_SUBMIT_ENABLED` default
false and must be explicitly armed by the operator. Each writing invocation also
requires `--confirm`. None of these flags registers a schedule.

Mount a fresh, hash-verified stock-selection cache at an operator-chosen path and
set `STOCK_SELECTION_CACHE` or pass `--cache-dir`. The input is the existing
`us/bars.csv` plus `us/bars.json` contract, including a benchmark and at least 252
sessions of usable history for enough names. The signal date must be the immediately
preceding XNYS session. A stale cache, insufficient names or invalid input refuses
the run. The CLI never downloads or silently substitutes stale data. Prepare the
cache with the existing `research/stock_selection/README.md` workflow in a source
checkout, then mount it into the image. The image ships only the pure list-builder
dependency files, with no datasets or model artifacts.

## Commands

`--asof` is the **execution session date in New York**, not a signal-bar date.
For Monday 2026-11-02 the signal is Friday 2026-10-30. The writing commands require
a fresh Alpaca clock and local clock both inside that date's regular session;
orders are not queued outside the session.

```bash
python -m scripts.mock_strategy run --venue alpaca_paper --asof 2026-11-02 \
  --cache-dir /mounted/stock-selection --confirm
python -m scripts.mock_strategy close-due --venue alpaca_paper --asof 2026-11-09 --confirm
python -m scripts.mock_strategy status --venue alpaca_paper --asof 2026-11-09
```

Defaults: ten up-list longs, ten down-list shorts, USD 10,000 target per name,
five trading days. `--top-n`, `--notional` and `--horizon {1,3,5,10}` are explicit
parameters. A committed strategy/date plan refuses different sizing or N on replay.
An existing plan is reused without rebuilding or replacing names. Attribution is
in the DB, inside the existing account.

For a completely offline preview, provide an asset fixture JSON with `account_ref`
and `assets` (Alpaca asset fields, including explicit `shortable`, `easy_to_borrow`
and `fractionable` booleans). Fixtures are accepted **only** for `run --dry-run`:

```bash
python -m scripts.mock_strategy run --venue alpaca_paper --asof 2026-11-02 \
  --cache-dir /synthetic/stock-selection --dry-run --asset-fixture /synthetic/assets.json
python -m scripts.mock_strategy close-due --venue alpaca_paper --asof 2026-11-09 \
  --dry-run --account-ref '<recorded account_ref>'
```

The first preview reads local files only: no DB, broker or file writes. The second
reads due DB rows without mutations and does not claim that broker checks passed.
`status` reads the DB only and reports recorded evidence, including unresolved
`sending` and `uncertain`; it does not perform broker reconciliation.

## Frozen selection and execution

The operator brief selects the highest reported walk-forward baseline IC in
`research/experiments/reports/rob-1474-stock-selection.md` §5 exactly once:

| Horizon | Baseline | Strategy ID |
|---|---|---|
| 1 | high_distance_52w | rob1474.us.high_distance_52w.h1.v1 |
| 3 | relative_volatility_20 | rob1474.us.relative_volatility_20.h3.v1 |
| 5 | relative_volatility_20 | rob1474.us.relative_volatility_20.h5.v1 |
| 10 | relative_volatility_20 | rob1474.us.relative_volatility_20.h10.v1 |

Do not tune these on accumulating paper results. They retain the research
orientations, including low volatility ranked high. Execution results come from
broker fills and remain separate from statistical close-to-close research labels.

All orders are MARKET/DAY during the regular session. Fractionable longs use
notional orders. Shorts use whole shares, flooring the target notional divided by
the prior session's close; nonfractionable longs use the same whole-share sizing.
The actual short notional varies with the market fill price, so equal notional is
a sizing target, with integer rounding and price movement reported through fills.
No price improvement or exact execution price is assumed. See Alpaca's
[order documentation](https://docs.alpaca.markets/us/docs/orders-at-alpaca) and
[client-ID lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid).

Only active tradable US equities qualify. A short additionally requires the
account's shorting permission and current `shortable` AND `easy_to_borrow` flags.
Unavailable names are explicitly skipped, without substituting lower-ranked names.
Those flags and fresh same-direction account positions are checked again at send.
An entry that would net an opposite account position is refused. Alpaca accounts
are netted: unrelated manual activity can still interfere between reads and POST.
Operate this paper book with that limitation in mind; the account lock coordinates
this consumer only, and cannot fence unrelated clients.

Exit due time is the regular open of the h-th XNYS session **after** the entry
session, including holiday handling and daylight saving. `close-due` refreshes each
due entry's broker order by its bound client ID. It closes only proven filled
quantity after the entry is terminal (filled, or partially filled then canceled/
expired). An accepted or still-working entry cannot supply close quantity.
Fresh account position and available long quantity must also cover the exit.
It never liquidates the account's entire symbol holding. A close ACK leaves the
episode open. Only terminal close fill evidence covering the entry fill quantity
closes the entry and records gross USD P&L; borrow costs and fees are not fabricated.

## Idempotency and recovery

Every entry and exit has a deterministic strategy/date/symbol/side client ID with
a strategy-encoding prefix. The whole entry plan commits before any POST. A unique
ID and committed `planned` → `sending` compare-and-set permits exactly one sender.
The migration blocks intent changes, resetting a sent row to planned, reopening
terminal rows and erasing the journal. The service is the only table writer.

Timeout, bad broker echo, failed result commit or crash leaves `uncertain` or the
already committed `sending`. Replay only looks up evidence by client ID; missing
or failed lookup is never permission to resubmit. An ambiguous or partially filled
close is not automatically replaced. Retain its reservation and reconcile the
original order; unresolved residuals require operator investigation. Do not delete
rows, reset statuses or change IDs to force a retry.

`run` reconciles existing entry evidence on same-session replay. `close-due`
reconciles due entry/exit evidence and safely resumes bookkeeping after a crash.
Use `status` to identify unresolved orders. Broker exceptions and DB DSNs are not
printed: the CLI emits a fixed error and exception class, and returns nonzero for
unresolved sends. No schedules, TaskIQ jobs, cron entries or deployments are added.
