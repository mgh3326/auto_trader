from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.analyst_consensus_snapshots.repository import (
    AnalystConsensusSnapshotUpsert,
)

logger = logging.getLogger(__name__)

ConsensusFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]

_SOURCE_FOR_MARKET: dict[str, str] = {
    "kr": "naver_finance",
    "us": "yfinance",
}


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

    payload = await handle_get_investment_opinions(symbol=symbol, market=market)
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

    # Skip if there is no meaningful data at all (all counts and prices null).
    total = _to_int(consensus.get("total_count"))
    current = _to_decimal(consensus.get("current_price"))
    target_mean = _to_decimal(consensus.get("avg_target_price"))
    if total is None and current is None and target_mean is None:
        return None

    return AnalystConsensusSnapshotUpsert(
        market=market,
        symbol=symbol.strip().upper(),
        source=data.get("source", _SOURCE_FOR_MARKET.get(market, market)),
        snapshot_date=snapshot_date,
        buy_count=_to_int(consensus.get("buy_count")),
        hold_count=_to_int(consensus.get("hold_count")),
        sell_count=_to_int(consensus.get("sell_count")),
        strong_buy_count=_to_int(consensus.get("strong_buy_count")),
        total_count=total,
        target_mean=target_mean,
        target_median=_to_decimal(consensus.get("median_target_price")),
        target_high=_to_decimal(consensus.get("max_target_price")),
        target_low=_to_decimal(consensus.get("min_target_price")),
        upside_pct=_to_decimal(consensus.get("upside_pct")),
        analyst_count=total,
        newest_opinion_date=data.get("newest_opinion_date"),
        current_price=current,
        raw_payload={
            "consensus": consensus,
            "opinion_count": len(data.get("opinions") or []),
        },
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
    snapshot_dt = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    snapshot_date = snapshot_dt.date()
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
