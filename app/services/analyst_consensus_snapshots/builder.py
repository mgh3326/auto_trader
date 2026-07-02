from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.services.analyst_consensus_snapshots.repository import (
    AnalystConsensusSnapshotUpsert,
)

logger = logging.getLogger(__name__)

ConsensusFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]

_SOURCE_FOR_MARKET: dict[str, str] = {
    "kr": "naver_finance",
    "us": "yfinance",
}

# snapshot_date convention: the market-local calendar date (kr → Asia/Seoul,
# us → America/New_York). A 00:30 KST run must record the KST date, not the
# previous UTC date. Keep in sync with the AnalystConsensusSnapshot docstring.
_MARKET_TZ: dict[str, dt.tzinfo] = {
    "kr": ZoneInfo("Asia/Seoul"),
    "us": ZoneInfo("America/New_York"),
}

# KR consensus stats reflect only the fetched opinion rows, so fetch up to the
# handler cap (handle_get_investment_opinions clamps limit to 30) instead of
# the default 10 which silently truncates well-covered symbols (ROB-641).
OPINIONS_LIMIT = 30


@dataclass(frozen=True)
class AnalystConsensusBuildResult:
    payloads: tuple[AnalystConsensusSnapshotUpsert, ...]
    warnings: tuple[str, ...] = ()


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return dt.date.fromisoformat(text.replace(".", "-").replace("/", "-"))
        except ValueError:
            return None
    return None


def _newest_opinion_date_from_list(opinions: list[Any]) -> dt.date | None:
    """Extract the most recent opinion date from a list of opinion dicts."""
    best: dt.date | None = None
    for op in opinions:
        if not isinstance(op, dict):
            continue
        for key in ("date", "report_date", "published_date", "published_at"):
            parsed = _to_date(op.get(key))
            if parsed is not None and (best is None or parsed > best):
                best = parsed
    return best


async def default_consensus_fetcher(market: str, symbol: str) -> dict[str, Any]:
    """Fetch analyst consensus data for a symbol in a market.

    Uses ``handle_get_investment_opinions`` which routes KR → naver_finance and
    US → yfinance, returning a unified ``consensus`` dict with buy/hold/sell
    counts, target prices, upside, and current price.

    Raises on error so the builder's per-symbol exception handler can record a
    warning and continue with the next symbol.
    """
    from app.mcp_server.tooling.fundamentals._valuation import (
        handle_get_investment_opinions,
    )

    payload = await handle_get_investment_opinions(
        symbol=symbol, market=market, limit=OPINIONS_LIMIT
    )
    if payload.get("error"):
        raise ValueError(str(payload["error"]))

    consensus = payload.get("consensus") or {}
    opinions = payload.get("opinions") or []
    source = _SOURCE_FOR_MARKET.get(market.strip().lower(), market.strip().lower())

    # newest_opinion_date: KR consensus dict carries it directly; US does not
    # but the opinions list has per-row dates we can scan.
    newest = _to_date(consensus.get("newest_opinion_date"))
    if newest is None and isinstance(opinions, list):
        newest = _newest_opinion_date_from_list(opinions)

    return {
        "source": source,
        "consensus": consensus,
        "opinions": opinions,
        "opinions_limit": OPINIONS_LIMIT,
        "newest_opinion_date": newest,
    }


def _payload_from_consensus(
    *,
    market: str,
    symbol: str,
    data: dict[str, Any],
    snapshot_date: dt.date,
) -> AnalystConsensusSnapshotUpsert | None:
    consensus = data.get("consensus") or {}

    total = _to_int(consensus.get("total_count"))
    current = _to_decimal(consensus.get("current_price"))
    counts = (
        _to_int(consensus.get("buy_count")),
        _to_int(consensus.get("hold_count")),
        _to_int(consensus.get("sell_count")),
        _to_int(consensus.get("strong_buy_count")),
        total,
    )
    targets = (
        _to_decimal(consensus.get("avg_target_price")),
        _to_decimal(consensus.get("median_target_price")),
        _to_decimal(consensus.get("max_target_price")),
        _to_decimal(consensus.get("min_target_price")),
        _to_decimal(consensus.get("upside_pct")),
    )
    # Skip unless there is at least one real consensus count or target field.
    # A row carrying only current_price is a quote, not a consensus snapshot.
    if all(value is None for value in (*counts, *targets)):
        return None

    # analyst_count: KR carries the normalizer's usable-target-price count
    # (target_price_count, app/services/analyst_normalizer.py); US carries
    # yfinance's numberOfAnalystOpinions. Fall back to total_count when absent.
    analyst_count = _to_int(consensus.get("target_price_count"))
    if analyst_count is None:
        analyst_count = _to_int(consensus.get("number_of_analyst_opinions"))
    if analyst_count is None:
        analyst_count = total

    raw_payload: dict[str, Any] = {
        "consensus": consensus,
        "opinion_count": len(data.get("opinions") or []),
    }
    opinions_limit = _to_int(data.get("opinions_limit"))
    if opinions_limit is not None:
        raw_payload["opinions_limit"] = opinions_limit

    return AnalystConsensusSnapshotUpsert(
        market=market,
        symbol=symbol.strip().upper(),
        source=data.get("source", _SOURCE_FOR_MARKET.get(market, market)),
        snapshot_date=snapshot_date,
        buy_count=counts[0],
        hold_count=counts[1],
        sell_count=counts[2],
        strong_buy_count=counts[3],
        total_count=total,
        target_mean=targets[0],
        target_median=targets[1],
        target_high=targets[2],
        target_low=targets[3],
        upside_pct=targets[4],
        analyst_count=analyst_count,
        newest_opinion_date=data.get("newest_opinion_date"),
        current_price=current,
        raw_payload=raw_payload,
    )


async def build_consensus_snapshots(
    *,
    market: str,
    symbols: Iterable[str],
    now: dt.datetime | None = None,
    concurrency: int = 4,
    fetcher: ConsensusFetcher | None = None,
) -> AnalystConsensusBuildResult:
    market_norm = market.strip().lower()
    fetch = fetcher or default_consensus_fetcher
    # snapshot_date is the market-local calendar date (kr → Asia/Seoul,
    # us → America/New_York) so a KST-morning run never books the prior day.
    snapshot_tz = _MARKET_TZ.get(market_norm, dt.UTC)
    snapshot_date = (now or dt.datetime.now(dt.UTC)).astimezone(snapshot_tz).date()
    sem = asyncio.Semaphore(max(1, concurrency))
    symbols_list = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    payloads: list[AnalystConsensusSnapshotUpsert | None] = [None] * len(symbols_list)
    warnings: list[str] = []

    async def _one(idx: int, symbol: str) -> None:
        async with sem:
            try:
                data = await fetch(market_norm, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "consensus snapshot fetch failed market=%s symbol=%s: %s",
                    market_norm,
                    symbol,
                    exc,
                )
                warnings.append(f"{symbol}: fetch failed ({exc})")
                return
            payload = _payload_from_consensus(
                market=market_norm,
                symbol=symbol,
                data=data,
                snapshot_date=snapshot_date,
            )
            if payload is None:
                warnings.append(
                    f"{symbol}: skipped because consensus data is unavailable"
                )
                return
            payloads[idx] = payload

    await asyncio.gather(
        *(_one(idx, symbol) for idx, symbol in enumerate(symbols_list))
    )
    return AnalystConsensusBuildResult(
        payloads=tuple(payload for payload in payloads if payload is not None),
        warnings=tuple(warnings),
    )
