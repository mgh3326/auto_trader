# ROB-155 News Feed Read-only Smoke Runbook

Purpose: validate deployed `/invest/api/feed/news` response shape after ROB-155 without mutating DB, broker, order, watch, or scheduler state.

Safety guarantees:
- Uses GET only.
- Does not submit, cancel, preview, or reconcile any order.
- Does not update/delete/backfill DB rows.
- Does not print cookie/token/header values; pass them through an env var only.

Local schema validation:

```bash
uv run pytest tests/test_news_feed_readonly_smoke.py -v
```

Production invocation after merge/deploy (operator-authenticated, read-only):

```bash
# Put the auth header value in an env var without echoing it.
export INVEST_AUTH_HEADER='[REDACTED]'
uv run python scripts/news_feed_readonly_smoke.py \
  --base-url https://paperclip.robinco.dev \
  --auth-header-env INVEST_AUTH_HEADER
unset INVEST_AUTH_HEADER
```

Validated endpoints:
- `/invest/api/feed/news?tab=latest&limit=20`
- `/invest/api/feed/news?tab=us&limit=20`
- `/invest/api/feed/news?tab=crypto&limit=20`

Checks:
- Response has an `items` array (or `data.items`).
- Every item includes additive ROB-155 fields: `scope`, `tags`, `category`, `noiseReason`.
- `scope` is one of `market_wide`, `symbol_specific`, `mixed`.
- `tags` is a list.
- Warns when crypto rows exist but no category distribution is present.
- Warns when market-wide US rows still expose many related-symbol chips.

Stop conditions:
- Any HTTP non-2xx response.
- Missing additive fields.
- Invalid `scope` or non-list `tags`.
- Any accidental request path beyond the three GET endpoints above.

Artifact handling:
- Save stdout JSON to `/tmp/rob155_news_feed_readonly_smoke_<timestamp>.json` if attaching evidence to Linear.
- Redact auth/cookie/header values in any copied command or comment.
