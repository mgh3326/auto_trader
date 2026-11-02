# ROB-1474 daily paper-runner bars

Manual data producer only: no orders, DB, deployment, or scheduler registration.
Use a fresh operator-owned root, convention `/data/mock-strategy-bars`, shared by
refresh and the paper runner. Do not reuse the 12-symbol research sample root:
ordinary market directories are refused, never overwritten. Retain the hidden
snapshot directories: `us` / `crypto` are atomic symlinks to immutable generations.
Readers pin one generation. Refresh writers sharing a market/root use `flock`.
Do not modify generation files or delete them while readers may still use them.

US credentials: `ALPACA_DATA_API_KEY_ID`, `ALPACA_DATA_API_SECRET_KEY`.
Feed: `MOCK_DATA_ALPACA_FEED` or `--feed iex|sip`, default `iex`. IEX measures only
that feed's liquidity; SIP requires the corresponding data entitlement. There is
no automatic feed fallback. Assets are GET from Alpaca's paper assets endpoint;
only active, tradable NYSE/NASDAQ/AMEX US equities are candidates. Names/explicit
flags exclude identifiable funds, ETFs/ETNs, warrants, units and preferreds;
Alpaca does not provide a definitive common-stock classifier, so unidentified
security types can remain. SPY is the explicitly recorded benchmark proxy.
Daily bars use `adjustment=all`, and dollar volume is `close * volume`, an estimate.
See [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars).

Crypto uses unauthenticated Binance spot exchangeInfo/klines on the data-only
host, UTC daily boundaries, actual kline quote volume and BTCUSDT benchmark.
TRADING USDT spot pairs exclude known stable bases and UP/DOWN/BULL/BEAR tokens.
See [Binance public market data](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md).
No trading credentials are needed for crypto refresh.

```bash
# Offline plan: no credentials, HTTP, directories or data writes.
python -m scripts.mock_data_refresh --market us --root /data/mock-strategy-bars --dry-run
# First backfill, then daily refresh (same command without --backfill).
python -m scripts.mock_data_refresh --market us --root /data/mock-strategy-bars --backfill
python -m scripts.mock_data_refresh --market crypto --root /data/mock-strategy-bars --backfill
```

Defaults: US top 1,000 by 20-session median dollar volume, 280 sessions;
crypto top 150 by 30-complete-day summed quote volume, 400 days. `--limit`,
`--lookback` and `--top-n` configure these. Minimum lookback is 260 US sessions /
365 crypto days (the shared feature builder requires 364 crypto days). Require
at least twice `top-n` complete symbols. A candidate with a missing session is
reported in `excluded`, not forward filled. Successful empty symbol histories are also reported as excluded. Benchmark gaps,
malformed bars, or insufficient complete symbols abort publication.

Subsequent days fetch a seven-day overlap and append new bars to the candidate
cache. New assets and revised US histories get a full-window fetch; overlap
changes trigger replacement of the entire affected history to avoid mixing
adjustment vintages. Older-only provider corrections outside that overlap cannot
be detected incrementally; use `--backfill` on a new observation day to refresh all
history, including after a known older correction.
Every observed UTC date is sealed; same-date reruns (including `--backfill`) are
network/write-free. Configuration changes require a fresh root. HTTP failure,
quota denial, or staging failure leaves the previous complete snapshot readable;
exit 2 reports only the error class. All requests are GET, paced at 120/minute,
with bounded pagination, timeouts, no redirects and no automatic retries.

Each generation has `bars.csv` / `bars.json` in the exact `read_bars` layout,
`candidates.csv` for incremental history, and `membership.json` containing the
observed-date universe and ranking. The manifest records counts, SHA-256 hashes,
feed, benchmark and completed-bar cutoff. Publication is a single symlink swap.
Old generations are retained; cleanup is a manual maintenance operation.
Keep seven total generations by default (current plus six newest archives).
Stop **all** readers/runners using this root before applying retention: the writer
lock serializes against refresh, but readers pin paths without holding that lock.
Preview, review the hashes, then apply during that same maintenance window:

```bash
python -m scripts.mock_data_prune --root /data/mock-strategy-bars --market us --keep 7
python -m scripts.mock_data_prune --root /data/mock-strategy-bars --market us --keep 7 --apply
# Repeat for --market crypto when its readers are stopped.
```

This deletes only producer generation directories beyond the retention count,
always protecting the current target. Earlier dated memberships remain inside
the retained current generation. Export generations needed for exact historical
price-vintage replay before deleting them. Resume readers after pruning. Monitor
free disk and run maintenance before the next daily refresh; no scheduler is added.
The full candidate symbol list is stored once per generation in
`source_symbols.json` (manifest SHA), rather than copied into each new dated
membership. Previously sealed membership records remain unchanged.

Membership starts at the **observation date**, never at the backfilled candle's
date. A first backfill cannot reconstruct historical membership or delisted
assets: it is current-vintage feature history, not a certified historical PIT
corpus. Each next daily observation preserves earlier memberships exactly.
Current bars contain only the current selected names; prior generations preserve
prior names. The runners reject a producer snapshot whose observation date is
later than the requested run. US additionally requires the universe cutoff to
match the preceding signal session and records `universe_observed_at` in entry
metadata. Crypto requires its universe cutoff to match the requested candle and
permits the following UTC date on which the
candle becomes available. Use a separate replay root whose market symlink points to the archived generation
for historical replay,
not today's active universe. Crypto signal provenance records the actual universe
observation time and moves `prediction_at` forward to at least that time. These
inputs do not certify research readiness.

Refresh **before** the operator's existing `mock_strategy run`. US uses the same
`--cache-dir /data/mock-strategy-bars`; crypto can now use
`--cache-dir /data/mock-strategy-bars` instead of `--panel-root`. The archive panel
route remains available and keeps its existing validation. Run after the latest
completed bar's availability (equity close + 15 minutes, crypto midnight + 5 minutes).
All runner execution gates, expected-account checks and explicit confirmations
remain required. This runbook does not enable or launch an order runner.
