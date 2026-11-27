"""Closed per-symbol outcome of ``get_news`` and ``get_disclosures`` (hk 6968).

"No news" and "collection failed" are different states:

- ``ok``: the lookup answered and the response carries current items.
- ``no_items``: the lookup answered and there is nothing to report.
- ``stale_copy``: the response serves stored copies this lookup could not
  refresh (provider failed, or nothing served was received within the
  freshness window).
- ``fetch_failed``: the lookup failed and nothing is served; this is never
  evidence that no news or filing exists.
"""

SYMBOL_STATE_OK = "ok"
SYMBOL_STATE_NO_ITEMS = "no_items"
SYMBOL_STATE_STALE_COPY = "stale_copy"
SYMBOL_STATE_FETCH_FAILED = "fetch_failed"
SYMBOL_STATES: tuple[str, ...] = (
    SYMBOL_STATE_OK,
    SYMBOL_STATE_NO_ITEMS,
    SYMBOL_STATE_STALE_COPY,
    SYMBOL_STATE_FETCH_FAILED,
)
