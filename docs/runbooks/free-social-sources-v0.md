# Free Social Sources v0 (ROB-729)

Operator-only read probe for free social/opinion sources. It prints one JSON object
intended to be copied into `evidence_snapshot["social_sentiment"]` during a manual
session.

## Safety

- No DB writes.
- No orders, order-intent, broker, watchlist, or scheduler calls.
- Social/opinion data is advisory evidence only.
- Naver discussion is aggregate-only and uses the existing gated handler.
- X CDP is disabled unless `--include-x-cdp` is passed.
- StockTwits Firestream is reported as `requires_credentials` unless the operator
  provides Firestream credentials; v0 does not scrape StockTwits web pages.

## Environment

```bash
export NAVER_CLIENT_ID=...
export NAVER_CLIENT_SECRET=...
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT='script:auto_trader.rob729:v0.1 (by /u/<operator>)'
export BSKY_HANDLE=...
export BSKY_APP_PASSWORD=...
```

Optional:

```bash
export RETAIL_SENTIMENT_LIVE_ENABLED=true
export STOCKTWITS_FIRESTREAM_USERNAME=...
export STOCKTWITS_FIRESTREAM_PASSWORD=...
```

For X CDP, launch the local logged-in Chrome profile:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/chrome-toss-debug"
```

## Examples

KR:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market kr \
  --symbol 005930 \
  --query 삼성전자 \
  --limit 5
```

US:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market us \
  --symbol NVDA \
  --query NVDA \
  --sources reddit,bluesky,stocktwits \
  --limit 5
```

X CDP opt-in:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market us \
  --symbol AAPL \
  --query "AAPL earnings" \
  --sources x_cdp \
  --include-x-cdp \
  --limit 5
```

## Acceptance

- The command exits `0`.
- The top-level JSON has `source="free_social_sources_v0"`.
- `advisory_only` is `true`.
- `cost_usd` is `0`.
- Missing credentials produce `status="missing_credentials"` or
  `status="requires_credentials"` source entries, not a process crash.
- No source item contains Naver discussion raw title, body, author, nickname, or
  comment text.
