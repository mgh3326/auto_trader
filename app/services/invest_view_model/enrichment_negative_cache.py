"""ROB-1309 — bounded negative cache for screener *enrichment* provider calls.

`screen_stocks_enrich` fans out per-symbol network calls (KR/US sector fetch,
analyst-consensus fetch) for every row on the returned page. Symbols that are
delisted, renamed, or otherwise permanently failing (404/parse-miss) get
re-fetched — and re-fail — on every single call, burning latency and HTTP
quota for a result that will never change within the TTL window.

This module is a small Redis-backed cache-aside of *negative* results only:
"provider X failed for (market, symbol) at time T, classified as reason R".
A lookup within TTL skips the network call entirely. It is deliberately:

- **Bounded**: Redis TTL expiry is the only bound — no unbounded in-process
  dict, no manual eviction logic to get wrong.
- **Fail-open**: any Redis error (including no client configured) means
  "no cache, always retry" — never fabricates a failure or blocks progress.
- **Non-silent**: callers get a structured record (symbol, market, kind,
  error_class, first_seen, failure_count) they can surface to the response
  under an `enrichment_excluded` / similar reporting key — never a place to
  quietly vanish a symbol from output. Rows themselves are NEVER dropped by
  this module; only the specific enrichment field (sector/consensus) for
  that symbol is skipped.
- **Not a universe mutator**: this module never writes to
  `kr_symbol_universe` / `us_symbol_universe` / `symbol_sectors`. Chronic
  failures are only ever *reported* (`chronic_failure_candidates`) as
  operator-actionable advisory data — the actual universe stays exactly as
  it is until an operator (or a separate, explicitly-authorized job) decides
  to act. This keeps the write surface to the pre-existing
  `symbol_sectors_service` / `InvestScreenerSnapshotsRepository.upsert`
  paths only; no new DML is introduced here.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "screener_enrich_negcache"

# TTL for a negative result. Deliberately short relative to the analyst
# consensus cache's daily TTL: an enrichment failure is more likely to be a
# transient blip (timeout, rate limit) than the KR analyst consensus itself
# (which genuinely is daily-stable). 30 minutes bounds retry storms without
# permanently hiding a symbol that recovers later in the day.
NEGATIVE_CACHE_TTL_SECONDS = 30 * 60

# A symbol is surfaced as a "chronic failure candidate" (operator-actionable,
# NOT auto-removed) once it has failed this many consecutive times across
# cache windows.
_CHRONIC_FAILURE_THRESHOLD = 3

_ERROR_CLASS_NOT_FOUND = "not_found"
_ERROR_CLASS_TIMEOUT = "timeout"
_ERROR_CLASS_RATE_LIMITED = "rate_limited"
_ERROR_CLASS_UNKNOWN = "unknown"


def classify_error(exc: BaseException) -> str:
    """Best-effort, stdlib-only classification of a provider failure.

    Never raises. Falls back to "unknown" for anything not recognized —
    fail-closed on classification just means "don't skip the universe-cleanup
    reporting bucket", not "hide the failure".
    """
    try:
        text = f"{type(exc).__name__}: {exc}".lower()
    except Exception:  # noqa: BLE001
        return _ERROR_CLASS_UNKNOWN
    if "timeout" in text or isinstance(exc, TimeoutError):
        return _ERROR_CLASS_TIMEOUT
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return _ERROR_CLASS_RATE_LIMITED
    if "404" in text or "not found" in text or "no such" in text:
        return _ERROR_CLASS_NOT_FOUND
    return _ERROR_CLASS_UNKNOWN


@dataclass
class NegativeCacheEntry:
    market: str
    symbol: str
    kind: str
    error_class: str
    first_seen_epoch: float
    last_seen_epoch: float
    consecutive_failures: int

    def is_chronic(self) -> bool:
        return self.consecutive_failures >= _CHRONIC_FAILURE_THRESHOLD


def _key(kind: str, market: str, symbol: str) -> str:
    return f"{_KEY_PREFIX}:{kind}:{(market or '').strip().lower()}:{symbol.upper()}"


async def get_entry(
    redis_client: Any, *, kind: str, market: str, symbol: str
) -> NegativeCacheEntry | None:
    """Return the cached negative-result entry, or None (cache miss / fail-open)."""
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_key(kind, market, symbol))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("enrichment_negcache GET failed %s/%s: %s", kind, symbol, exc)
        return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
        return NegativeCacheEntry(**parsed)
    except (TypeError, ValueError):
        return None


async def should_skip(
    redis_client: Any, *, kind: str, market: str, symbol: str
) -> NegativeCacheEntry | None:
    """Alias for get_entry — a non-None return means "skip the network call"."""
    return await get_entry(redis_client, kind=kind, market=market, symbol=symbol)


async def record_failure(
    redis_client: Any,
    *,
    kind: str,
    market: str,
    symbol: str,
    exc: BaseException,
) -> NegativeCacheEntry:
    """Record a provider failure. Always returns the entry (even if the Redis
    write itself fails-open) so the caller can report it regardless."""
    now = time.time()
    error_class = classify_error(exc)
    prior = await get_entry(redis_client, kind=kind, market=market, symbol=symbol)
    entry = NegativeCacheEntry(
        market=(market or "").strip().lower(),
        symbol=symbol.upper(),
        kind=kind,
        error_class=error_class,
        first_seen_epoch=prior.first_seen_epoch if prior else now,
        last_seen_epoch=now,
        consecutive_failures=(prior.consecutive_failures if prior else 0) + 1,
    )
    if redis_client is not None:
        try:
            await redis_client.set(
                _key(kind, market, symbol),
                json.dumps(asdict(entry)),
                ex=NEGATIVE_CACHE_TTL_SECONDS,
            )
        except Exception as write_exc:  # noqa: BLE001 — fail-open
            logger.debug(
                "enrichment_negcache SET failed %s/%s: %s", kind, symbol, write_exc
            )
    return entry


async def record_success(
    redis_client: Any, *, kind: str, market: str, symbol: str
) -> None:
    """Clear a negative-cache entry on a successful fetch (recovery)."""
    if redis_client is None:
        return
    try:
        await redis_client.delete(_key(kind, market, symbol))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("enrichment_negcache DELETE failed %s/%s: %s", kind, symbol, exc)
