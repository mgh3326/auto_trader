"""ROB-273 — external-origin → ``SourceKind`` literal mapping.

The snapshot foundation enforces a closed set of ``source_kind`` literal
values (see ``app/schemas/investment_snapshots.py``). Collectors and ad-hoc
ingestion paths historically used free-form strings (``upbit_mcp``, ``db``,
``mcp_screen``, ``not_applicable``, ``finnhub_crypto``) that violate the
literal contract and would cause Pydantic ``ValidationError`` at insert
time, sometimes long after the data was collected.

This module centralises the mapping so the generator can normalise
collector results *before* they hit
:class:`SnapshotCollectResult`. Anything we don't recognise is rejected
loudly with :class:`UnsupportedSourceKindError` — the caller is responsible
for picking the correct literal rather than silently falling through to a
catch-all bucket.
"""

from __future__ import annotations

from typing import Final, get_args

from app.schemas.investment_snapshots import SourceKind

ALLOWED_SOURCE_KINDS: Final[frozenset[str]] = frozenset(get_args(SourceKind))

# Origins seen in the manual production run that the ROB-273 ticket
# explicitly forbids inventing as new literals. Each maps to an allowed
# member of ``SourceKind`` so callers can keep their internal naming while
# the persisted value remains canonical.
_EXTERNAL_ORIGIN_TO_SOURCE_KIND: Final[dict[str, str]] = {
    # Upbit/crypto MCP evidence
    "upbit_mcp": "auto_trader_mcp",
    "upbit_public": "auto_trader_mcp",
    # DB / query-service / screen evidence
    "db": "auto_trader_mcp",
    "mcp_screen": "auto_trader_mcp",
    "auto_trader_db": "auto_trader_mcp",
    "domain_db": "domain_ref",
    # KIS MCP evidence
    "kis_api": "kis_mcp",
    # Finnhub / market events derivatives (read through auto_trader)
    "finnhub_crypto": "auto_trader_mcp",
    "finnhub": "auto_trader_mcp",
    "dart": "auto_trader_mcp",
    # News ingestor
    "news": "news_ingestor",
    "research_reports": "news_ingestor",
    # Invest HTTP surface
    "invest_http": "invest_api",
    # Manual / not-applicable buckets
    "not_applicable": "manual",
    "operator_paste": "manual",
}


class UnsupportedSourceKindError(ValueError):
    """Raised when an origin string cannot be mapped to a ``SourceKind``."""

    def __init__(self, origin: str) -> None:
        super().__init__(
            f"unsupported source origin {origin!r}; "
            f"map it to one of {sorted(ALLOWED_SOURCE_KINDS)} explicitly "
            f"or extend SourceKind literal + alembic migration"
        )
        self.origin = origin


def map_source_kind(origin: str) -> str:
    """Map an external origin string to a canonical ``SourceKind`` literal.

    * Already-canonical values pass through unchanged.
    * Known aliases (see :data:`_EXTERNAL_ORIGIN_TO_SOURCE_KIND`) translate
      to their canonical literal.
    * Anything else raises :class:`UnsupportedSourceKindError` — the
      caller must pick an explicit mapping rather than invent a new
      literal at the snapshot insert site.
    """
    if not isinstance(origin, str) or not origin:
        raise UnsupportedSourceKindError(repr(origin))

    if origin in ALLOWED_SOURCE_KINDS:
        return origin

    mapped = _EXTERNAL_ORIGIN_TO_SOURCE_KIND.get(origin)
    if mapped is None:
        raise UnsupportedSourceKindError(origin)

    # Defensive: if a future contributor mis-types the right-hand side of
    # _EXTERNAL_ORIGIN_TO_SOURCE_KIND, fail loudly here rather than
    # propagating a half-canonical value.
    if mapped not in ALLOWED_SOURCE_KINDS:
        raise UnsupportedSourceKindError(origin)

    return mapped
