# Toss Phase 2 combined-KRX/NXT staging

This collector creates an append-only **STAGING ONLY — NOT A BACKTEST INPUT**
Parquet corpus for Toss 1-minute candles. It does not connect to PostgreSQL and
does not load data into any table.

The product is physically separate because Toss may combine KRX and NXT bars
for NXT-eligible symbols. `session_segment` is only a KST time-of-day label
(`NXT_PRE`, `KRX_REGULAR`, `NXT_POST`, or `UNKNOWN`); it is never a trade-venue
claim and no `venue` column is produced.

Consumer rules:

- Toss's 15:30 candle omits closing-auction volume. Daily volume and derived
  daily value from this corpus understate the market total.
- `is_padding=true` means the provider returned a volume-zero placeholder. It
  is not a coverage gap and must be excluded when comparing traded-minute
  coverage.
- `value` is synthetic `close * volume`; `value_semantics` is always
  `CLOSE_X_VOLUME_SYNTHETIC`, not exchange-reported trade value.
- `pre_nxt` stays NULL/UNKNOWN without an exact, sourced NXT launch date. A
  later label review may make pre-NXT rows eligible for a separate promotion
  proposal, but this collector performs no promotion.

The collector uses a shared cached Toss token only. It cannot issue or force
reissue OAuth.

The declared call budget is staging-wide rather than process-local: a resumed
collector recovers prior chart attempts from the append-only progress log and
will not reset that budget by restarting.

## Shared chart-budget control

`MARKET_DATA_CHART` is a client × API-group budget shared with the production
Toss-first chart reader. The Phase-2 collector therefore does not treat a
documented chart cap as a source constant:

- The first chart response must expose `X-RateLimit-Limit`; that header is the
  active cap. The single startup probe and all subsequent chart calls bypass
  the runtime's static process-local chart limiter; a missing header cap fails
  closed before a second page request.
- During 09:00–20:00 KST, the collector targets at most
  `min(2 TPS, discovered-cap − 3 TPS)`, preserving three TPS for production
  chart readers. If the provider's cap cannot preserve that reservation, the
  collector stops before it consumes another page budget.
- After 20:00 KST it may use 3–4 TPS only while headers remain healthy.
  `X-RateLimit-Remaining` below 40% triggers a Reset-window recovery pause;
  `X-RateLimit-Reset` is interpreted as seconds to one-token replenishment.
- A collector 429 honors `Retry-After` and combines it with exponential
  backoff plus jitter (1 → 2 → 4 seconds …). It does not immediately stop;
  only five consecutive chart 429s stop the collector. New shared 429 markers
  are likewise non-fatal, while 401 and other upstream errors remain
  fail-closed.
- A missing shared cached token or a one-off HTTP transport/timeout failure
  retries the same checkpoint cursor with jittered 1 → 2 → 4 → 8 second
  waits. This retry path still only reads the shared token cache, never issues
  OAuth, and stops fail-closed on five consecutive failures. The staging
  `latest_summary.json` is atomically refreshed while the collector runs.
