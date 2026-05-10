# Runbook: news source market vs asset market (ROB-172)

**Scope**: operator reference for the `sourceMarket` / `market` dual-emission
introduced in ROB-172.  No DB mutations, no broker or order state.

---

## Background

Before ROB-172 every `FeedNewsItem` had a single `market` field that served
two purposes:

1. **Source/feed market** — which feed the article came from (`kr`, `us`,
   `crypto`).
2. **Implied asset market** — the market assumed when looking up related
   symbols (e.g. only search KR aliases for a `market=kr` article).

The two meanings collided when a KR-feed article mentioned a US company
(e.g. "엔비디아 신제품" → NVDA/us).  The old code restricted alias matching
to the article's source market, so cross-market entities were silently dropped
from `relatedSymbols`.

ROB-172 separates the concepts:

| Field | Location | Meaning |
|---|---|---|
| `FeedNewsItem.market` | item | Source/feed market of the article. Kept for backward compat. |
| `FeedNewsItem.sourceMarket` | item | Same as `market` during the compat window. Prefer this in new code. |
| `NewsRelatedSymbol.market` | per related symbol | Asset's own market. May differ from `sourceMarket`. |

---

## What changed

### Backend (`app/services/invest_view_model/feed_news_service.py`)

`match_symbols_for_article` is now called with `market=None` instead of
`market=market_value`.  This searches `ALL_ALIASES` (all markets) so a
KR-feed article that mentions `엔비디아` can surface `NVDA` in
`relatedSymbols` with `market="us"`.

The article's source market (`market_typed`) is still written to
`FeedNewsItem.market` and also to the new `FeedNewsItem.sourceMarket` field.

### Schema (`app/schemas/invest_feed_news.py`)

- `NewsRelatedSymbol.market` — unchanged type; now documented as the *asset's*
  market.
- `FeedNewsItem.sourceMarket` — new optional field.  Defaults to `market` via
  `model_validator` so callers that omit it (e.g. existing tests, old API
  clients) continue to work without any code change.

### Frontend (`frontend/invest/src/types/feedNews.ts`)

`FeedNewsItem.sourceMarket` added as `"kr" | "us" | "crypto" | undefined`.
Existing code that reads `item.market` is unaffected.

---

## Dual-emission window

During the window both `market` and `sourceMarket` are emitted with the same
value.  The frontend can migrate readers to `sourceMarket` incrementally:

```ts
// safe during and after the window
const feedMarket = item.sourceMarket ?? item.market;
```

Once all frontend readers use `sourceMarket`, the legacy `market` field can be
retired in a follow-up ticket.

---

## Smoke-script behaviour (ROB-172)

`scripts/news_feed_readonly_smoke.py` issues **warnings** (not errors) for:

- `source_market_missing_on_N_items` — item lacks `sourceMarket`; expected
  during the window on older deployments.
- `source_market_diverges_from_market_on_N_items` — `sourceMarket` ≠ `market`;
  should not happen in steady state, investigate if seen after full rollout.

These warnings do not set `ok=False`.  After the frontend migration is complete,
move `"sourceMarket"` from `_OPTIONAL_ADDITIVE_FIELDS_WARN` into
`_ADDITIVE_FIELDS` (required) and remove the warn constant.

---

## Troubleshooting

### NVDA (or other cross-market symbol) missing from KR article

1. Check `relatedSymbols` on the item — is `symbol="NVDA"` present with
   `market="us"`?
2. Confirm the article title/summary/keywords contain `엔비디아` or `NVDA`.
3. Run the entity matcher directly:

```python
from app.services.news_entity_matcher import match_symbols_for_article
matches = match_symbols_for_article(
    title="엔비디아 신제품 공개", market=None
)
print([(m.symbol, m.market, m.reason) for m in matches])
```

If `NVDA` is missing here, check `app/services/news_entity_matcher.py` alias
dictionaries for the `엔비디아` entry.

### `sourceMarket` missing from API response

If the backend is on the pre-ROB-172 version, `sourceMarket` will be absent.
The smoke script will warn but not fail.  After deploying ROB-172 the field
should appear automatically.

### `sourceMarket` diverges from `market`

This should not happen in steady state.  If seen after full rollout, check
whether `market_typed` and the value passed to `sourceMarket=` in
`FeedNewsItem(...)` construction differ.  The construction site is in
`feed_news_service.py` around the `items.append(FeedNewsItem(...))` call.

---

## Related tickets

- ROB-172 — this runbook's parent ticket
- ROB-155 — additive scope/tags/category fields (predecessor pattern)
- ROB-169 — KR society noise filtering

## Related files

- `app/schemas/invest_feed_news.py`
- `app/services/invest_view_model/feed_news_service.py`
- `app/services/news_entity_matcher.py`
- `scripts/news_feed_readonly_smoke.py`
- `frontend/invest/src/types/feedNews.ts`
