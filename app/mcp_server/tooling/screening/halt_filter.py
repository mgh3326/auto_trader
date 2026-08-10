"""ROB-1236 — drop halt-suspect symbols out of screener results.

The screener ranks on the same daily bars ``analyze_stock_batch`` reads, so a
symbol whose series has gone inert (consecutive zero-volume / zero-variation
sessions) can surface as a normal candidate with an RSI, a "support", and a
change rate — all arithmetic over dead candles. It happened: 000880 한화 was
ranked buy candidate #2 during an eight-session 매매거래정지.

Exclusion here is deliberately **not silent**. Every dropped symbol is listed
in ``meta.halted_suspect_excluded`` with its evidence and echoed as a warning,
because a false positive removes a real buy candidate and an operator has to be
able to see that happen and overrule it.

Detection failure is fail-*open*: if the bar history cannot be read the row is
kept and a warning is recorded. A DB hiccup is not evidence of a halt, and
silently deleting candidates on infrastructure noise is the opposite mistake.

Cost gate
---------
Reading history per row is not free: ``_fetch_ohlcv_for_indicators`` is
cache-first, but during a live KRX session the daily cache is deliberately
bypassed (today's bar is still forming), so a naive check on a 100-row screen
would fire 100 live KIS candle fetches at exactly the hour an operating session
runs. So the history read is gated on the row's own latest-bar volume: a
detection requires the frozen run to end at the newest bar, and a newest bar
that traded is not zero-volume-frozen. In practice that leaves a handful of
rows per screen.

🔴 Known gap this gate accepts: a run frozen purely by *zero variation* while
still printing volume (three straight sessions with no intraday range at all,
closing unchanged) is skipped here. ``analyze_stock_batch`` and the policy-table
builders read full history unconditionally and still catch it — this shortcut
exists only on the screener's per-row hot path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.mcp_server.tooling.market_data_indicators import _fetch_ohlcv_for_indicators
from app.mcp_server.tooling.screening.enrichment import _streak_symbol
from app.services.halt_detection import HaltSuspicion, classify_ohlcv_frame

logger = logging.getLogger(__name__)

#: Enough history to see the frozen run with room to spare; the read is
#: cache-first against the daily-candle store, not a broker call.
_HALT_LOOKBACK_BARS = 20
_HALT_CONCURRENCY = 8

#: Screener market key -> the ``market_type`` the OHLCV fetchers expect.
_MARKET_TYPE_BY_SCREEN_MARKET = {
    "kr": "equity_kr",
    "kospi": "equity_kr",
    "kosdaq": "equity_kr",
    "konex": "equity_kr",
    "all": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}


#: Per-market names for "how much traded on the newest bar". Turnover counts
#: as well as share volume — either being positive proves the bar traded.
_TRADED_FIELDS = (
    "volume",
    "volume_24h",
    "daily_volume",
    "trade_amount",
    "trade_amount_24h",
    "daily_turnover",
)


def _latest_bar_traded(row: dict[str, Any]) -> bool:
    """True when the row's own newest bar shows real volume or turnover.

    A row carrying none of the known fields, or only unparseable ones, returns
    ``False`` so it still gets the full history read — the cheap gate must
    never be the reason a halt slips through.
    """
    for field in _TRADED_FIELDS:
        raw = row.get(field)
        if raw is None:
            continue
        try:
            if float(raw) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def _classify_row(
    row: dict[str, Any],
    *,
    market_type: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str | None, HaltSuspicion | None, str | None]:
    symbol = _streak_symbol(row)
    if not symbol:
        return None, None, None
    if _latest_bar_traded(row):
        return symbol, None, None
    async with semaphore:
        try:
            frame = await _fetch_ohlcv_for_indicators(
                symbol, market_type, count=_HALT_LOOKBACK_BARS
            )
        except Exception as exc:  # noqa: BLE001 — fail open, never drop the row
            return (
                symbol,
                None,
                f"{symbol}: halt check skipped ({type(exc).__name__}: {exc})",
            )
    return symbol, classify_ohlcv_frame(frame), None


async def exclude_halt_suspect_rows(
    response: dict[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Return ``response`` with halt-suspect rows removed and accounted for."""
    market_type = _MARKET_TYPE_BY_SCREEN_MARKET.get(market)
    if market_type is None:
        return response

    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        return response
    rows = [row for row in raw_results if isinstance(row, dict)]
    if not rows:
        return response

    semaphore = asyncio.Semaphore(_HALT_CONCURRENCY)
    verdicts = await asyncio.gather(
        *(
            _classify_row(row, market_type=market_type, semaphore=semaphore)
            for row in rows
        )
    )

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    check_skipped: list[str] = []
    warnings: list[str] = []
    for row, (symbol, suspicion, skip_reason) in zip(rows, verdicts, strict=True):
        if skip_reason:
            # Diagnostics, not an operator warning: ``warnings`` is a contract
            # some callers assert on exactly, and a transient candle-store
            # error is not something an operator acts on. Only an actual
            # exclusion earns a warning.
            check_skipped.append(skip_reason)
        if suspicion is None or not suspicion.suspected:
            kept.append(row)
            continue
        evidence = suspicion.to_dict()
        excluded.append({"symbol": symbol, **evidence})
        warnings.append(
            f"{symbol}: excluded as halted_suspect "
            f"({suspicion.frozen_sessions} consecutive inert sessions, "
            f"reasons={'+'.join(suspicion.reasons)}) — suspicion, not a "
            "confirmed halt; verify before overriding"
        )

    if check_skipped:
        logger.warning(
            "halt check skipped for %d screener row(s): %s",
            len(check_skipped),
            "; ".join(check_skipped[:5]),
        )
    if not excluded and not check_skipped:
        return response

    dropped = len(rows) - len(kept)
    # ``total_count`` may describe the whole matching set while ``results`` is
    # one page of it, so this only subtracts what was seen on this page. It can
    # under-report how many halted names exist overall; it never over-reports
    # how many rows were returned.
    previous_total = response.get("total_count")
    total_count = (
        max(0, int(previous_total) - dropped)
        if isinstance(previous_total, int)
        else len(kept)
    )

    meta = dict(response.get("meta") or {})
    if excluded:
        meta["halted_suspect_excluded"] = excluded
    if check_skipped:
        meta["halted_suspect_check_skipped"] = check_skipped

    merged_warnings = list(response.get("warnings") or [])
    merged_warnings.extend(warnings)

    updated = {
        **response,
        "results": kept,
        "total_count": total_count,
        "returned_count": len(kept),
        "meta": meta,
    }
    if merged_warnings:
        updated["warnings"] = merged_warnings
    return updated


__all__ = ["exclude_halt_suspect_rows"]
