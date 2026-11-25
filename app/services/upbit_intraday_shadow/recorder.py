"""One recorder run: fetch, evaluate closed bars, resolve virtual trades (hk 6036).

Without a session the run is a dry run: nothing is read from or written to the
database, and only this run's own candidates are resolved. With a session the
candidates are committed first, then every recorded candidate that still lacks a
horizon is resolved and committed.
"""

from __future__ import annotations

import http.client
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow import outcomes, writer
from app.services.upbit_intraday_shadow.candidates import evaluate_market
from app.services.upbit_intraday_shadow.public_data import (
    CandlePage,
    FetchError,
    MarketList,
    MarketUnavailable,
    UpbitPublicData,
    slot_of,
    slot_open,
    utc_now,
)

#: A market's failure is reported and does not stop the other markets.
_MARKET_ERRORS = (
    FetchError,
    MarketUnavailable,
    OSError,
    http.client.HTTPException,
    ValueError,
)


def newest_settled_slot(now: datetime) -> int:
    """The newest bar that had closed ``SETTLE`` before ``now``."""
    return slot_of(now - K.SETTLE) - 1


def _error(market: str, stage: str, exc: Exception) -> dict[str, str]:
    return {
        "market": market,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}"[:300],
    }


def _resolve_all(
    data: UpbitPublicData,
    pending: Sequence[K.PendingCandidate],
    pages: dict[str, CandlePage],
    listing: MarketList,
    *,
    now: datetime,
    errors: list[dict[str, str]],
) -> list[K.VirtualTrade]:
    end_slot = newest_settled_slot(now)
    boundary = slot_of(now)
    by_market: dict[str, list[K.PendingCandidate]] = defaultdict(list)
    for item in pending:
        by_market[item.market].append(item)
    trades: list[K.VirtualTrade] = []
    for market, items in sorted(by_market.items()):
        known = [pages[market]] if market in pages else []
        for item in sorted(items, key=lambda p: p.signal_bar_open_at):
            signal_slot = slot_of(item.signal_bar_open_at)
            # The page must reach the longest horizon (or the present) and still
            # speak for the entry bar.
            reach = min(signal_slot + max(K.HORIZONS) + 1, boundary)
            page = next(
                (
                    p
                    for p in known
                    if p.before_slot >= reach and p.covers(signal_slot + 1)
                ),
                None,
            )
            if page is None:
                try:
                    page = data.candles(market, slot_open(reach))
                except MarketUnavailable as exc:
                    # Permanent only for a market Upbit no longer lists.
                    if market in listing.market_events:
                        errors.append(_error(market, "outcome", exc))
                        break
                    trades += outcomes.resolve(item, None, end_slot=end_slot, now=now)
                    continue
                except _MARKET_ERRORS as exc:
                    errors.append(_error(market, "outcome", exc))
                    break  # stays pending; the next run tries again
                known.append(page)
            trades += outcomes.resolve(item, page, end_slot=end_slot, now=now)
    return trades


async def run_once(
    data: UpbitPublicData,
    *,
    run_id: str,
    session: AsyncSession | None = None,
    catchup_bars: int = K.DEFAULT_CATCHUP_BARS,
    markets: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    K.verify_frozen_rules()
    started = now or utc_now()
    end_slot = newest_settled_slot(started)
    before = slot_open(slot_of(started))  # never ask for the forming bar
    selected = K.selected_config_ids()
    errors: list[dict[str, str]] = []

    listing = data.markets()
    wanted = sorted(listing.market_events) if markets is None else sorted(set(markets))
    pages: dict[str, CandlePage] = {}
    candidates: list[K.Candidate] = []
    evaluated = 0
    for market in wanted:
        if market not in listing.market_events:
            errors.append(
                {"market": market, "stage": "signal", "error": "market not listed"}
            )
            continue
        try:
            page = data.candles(market, before)
            pages[market] = page
            candidates += evaluate_market(
                page,
                end_slot=end_slot,
                catchup_bars=catchup_bars,
                market_event=listing.market_events[market],
                market_event_observed_at=listing.observed_at,
                selected=selected,
            )
        except _MARKET_ERRORS as exc:
            errors.append(_error(market, "signal", exc))
            continue
        evaluated += 1

    report: dict[str, Any] = {
        "run_id": run_id,
        "mode": "dry_run" if session is None else "commit",
        "rules_version": K.RULES_VERSION,
        "run_started_at": started,
        "evaluated_bar_opens": [
            slot_open(end_slot - catchup_bars + 1),
            slot_open(end_slot),
        ],
        "markets_listed": len(listing.market_events),
        "markets_evaluated": evaluated,
        "market_event_observed_at": listing.observed_at,
        "candidates": {
            "signal": sum(c.disposition == "signal" for c in candidates),
            "excluded": sum(c.disposition == "excluded" for c in candidates),
            "by_exclusion_reason": dict(
                Counter(r for c in candidates for r in c.exclusion_reasons)
            ),
        },
    }

    if session is None:
        pending = [outcomes.pending_from(c) for c in candidates]
    else:
        report["inserted_candidates"] = await writer.insert_candidates(
            session, candidates, run_id=run_id
        )
        await session.commit()
        pending = await writer.load_pending(session, markets)
        await session.commit()

    trades = _resolve_all(data, pending, pages, listing, now=started, errors=errors)
    report["virtual_trades"] = {
        "with_entry": sum(t.missing_reason is None for t in trades),
        "missing": dict(Counter(t.missing_reason for t in trades if t.missing_reason)),
    }
    if session is None:
        report["rows"] = [asdict(c) for c in candidates]
        report["virtual_rows"] = [asdict(t) for t in trades]
    else:
        report["inserted_virtual_trades"] = await writer.insert_virtual_trades(
            session, trades, run_id=run_id
        )
        await session.commit()
    report["errors"] = errors
    return report
