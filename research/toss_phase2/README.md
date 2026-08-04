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
reissue OAuth, and it stops itself on a new shared Toss 401/429/upstream error.
