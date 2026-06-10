from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from app.schemas.invest_screener import (
    ScreenerAnalysisConsensus,
)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def build_rsi14_from_closes(closes: Sequence[Any]) -> float | None:
    values = [_to_float(v) for v in closes]
    clean = [v for v in values if v is not None]
    if len(clean) < 15:
        return None
    close = pd.Series(clean)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_value = rsi.iloc[-1]
    return round(float(rsi_value), 2) if pd.notna(rsi_value) else None


def normalize_consensus_payload(
    payload: dict[str, Any] | None,
) -> tuple[ScreenerAnalysisConsensus | None, list[str]]:
    if not payload or payload.get("error"):
        return None, ["analyst_consensus_unavailable"]

    raw = payload.get("consensus") or {}
    if not isinstance(raw, dict):
        return None, ["analyst_consensus_unavailable"]

    total = _to_int(raw.get("total_count") or raw.get("totalCount"))
    if total is None or total <= 0:
        return None, ["analyst_consensus_missing"]

    consensus = ScreenerAnalysisConsensus(
        source=payload.get("source") or payload.get("provider"),
        buyCount=_to_int(raw.get("buy_count") or raw.get("buyCount")),
        holdCount=_to_int(raw.get("hold_count") or raw.get("holdCount")),
        sellCount=_to_int(raw.get("sell_count") or raw.get("sellCount")),
        strongBuyCount=_to_int(
            raw.get("strong_buy_count") or raw.get("strongBuyCount")
        ),
        totalCount=total,
        avgTargetPrice=_to_float(
            raw.get("avg_target_price") or raw.get("avgTargetPrice")
        ),
        medianTargetPrice=_to_float(
            raw.get("median_target_price") or raw.get("medianTargetPrice")
        ),
        minTargetPrice=_to_float(
            raw.get("min_target_price") or raw.get("minTargetPrice")
        ),
        maxTargetPrice=_to_float(
            raw.get("max_target_price") or raw.get("maxTargetPrice")
        ),
        upsidePct=_to_float(raw.get("upside_pct") or raw.get("upsidePct")),
        currentPrice=_to_float(raw.get("current_price") or raw.get("currentPrice")),
    )

    warnings: list[str] = []
    if (
        consensus.avgTargetPrice is not None
        and consensus.currentPrice is not None
        and consensus.currentPrice > 0
    ):
        computed = round(
            (consensus.avgTargetPrice - consensus.currentPrice)
            / consensus.currentPrice
            * 100,
            2,
        )
        if (
            consensus.upsidePct is not None
            and abs(computed - consensus.upsidePct) > 1.0
        ):
            warnings.append("consensus_upside_mismatch")
        if (
            consensus.avgTargetPrice < consensus.currentPrice
            and (consensus.buyCount or 0)
            > ((consensus.holdCount or 0) + (consensus.sellCount or 0))
        ):
            warnings.append("consensus_target_below_current_with_bullish_votes")
    return consensus, warnings


def build_analyst_label(
    consensus: ScreenerAnalysisConsensus | None, *, warnings: list[str] | None = None
) -> str:
    if consensus is None or not consensus.totalCount:
        return "-"
    if warnings:
        return "컨센 확인필요"
    buy = consensus.buyCount or 0
    hold = consensus.holdCount or 0
    sell = consensus.sellCount or 0
    base = f"매수 {buy} / 보유 {hold} / 매도 {sell}"
    if consensus.upsidePct is None:
        return base
    return f"{base} · 목표 {consensus.upsidePct:+.1f}%"

import asyncio
from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.schemas.invest_screener import ScreenerAnalysisContext

OpinionProvider = Callable[..., Any]


async def _opinion_payload(
    provider: OpinionProvider, *, symbol: str, market: str
) -> dict[str, Any] | None:
    try:
        result = provider(symbol=symbol, market=market, limit=10)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=4.5)
        return result
    except TimeoutError:
        return {"error": "analyst_consensus_timeout"}
    except Exception:
        return {"error": "analyst_consensus_unavailable"}


async def _rsi_by_symbol(
    db: AsyncSession, *, market: str, symbols: list[str]
) -> dict[str, float]:
    if not symbols:
        return {}
    if market in {"kr", "us"}:
        from app.models.invest_screener_snapshot import InvestScreenerSnapshot

        latest_date = (
            await db.execute(
                sa.select(sa.func.max(InvestScreenerSnapshot.snapshot_date)).where(
                    InvestScreenerSnapshot.market == market,
                    InvestScreenerSnapshot.symbol.in_(symbols),
                )
            )
        ).scalar_one_or_none()
        if latest_date is None:
            return {}
        rows = (
            await db.execute(
                sa.select(
                    InvestScreenerSnapshot.symbol,
                    InvestScreenerSnapshot.closes_window,
                ).where(
                    InvestScreenerSnapshot.market == market,
                    InvestScreenerSnapshot.snapshot_date == latest_date,
                    InvestScreenerSnapshot.symbol.in_(symbols),
                )
            )
        ).all()
        return {
            row.symbol: rsi
            for row in rows
            if (rsi := build_rsi14_from_closes(row.closes_window or [])) is not None
        }

    if market == "crypto":
        from app.models.invest_crypto_screener_snapshot import (
            InvestCryptoScreenerSnapshot,
        )

        latest_date = (
            await db.execute(
                sa.select(sa.func.max(InvestCryptoScreenerSnapshot.snapshot_date)).where(
                    InvestCryptoScreenerSnapshot.symbol.in_(symbols)
                )
            )
        ).scalar_one_or_none()
        if latest_date is None:
            return {}
        rows = (
            await db.execute(
                sa.select(
                    InvestCryptoScreenerSnapshot.symbol,
                    InvestCryptoScreenerSnapshot.rsi,
                ).where(
                    InvestCryptoScreenerSnapshot.snapshot_date == latest_date,
                    InvestCryptoScreenerSnapshot.symbol.in_(symbols),
                )
            )
        ).all()
        return {
            row.symbol: float(row.rsi)
            for row in rows
            if row.rsi is not None
        }

    return {}


async def enrich_snapshot_page(
    *,
    rows: list[dict[str, Any]],
    market: str,
    session_factory: async_sessionmaker[AsyncSession],
    opinion_provider: OpinionProvider = handle_get_investment_opinions,
) -> dict[str, Any]:
    symbols = [str(row.get("symbol") or "").strip() for row in rows if row.get("symbol")]
    symbols = list(dict.fromkeys(symbols))
    summary = {
        "attempted": len(rows),
        "consensusSucceeded": 0,
        "rsiSucceeded": 0,
        "warnings": [],
    }
    if not rows:
        return {"results": rows, "summary": summary}

    async with session_factory() as db:
        try:
            rsi_map = await _rsi_by_symbol(db, market=market, symbols=symbols)
        except Exception:
            rsi_map = {}
            summary["warnings"].append("rsi_enrichment_unavailable")

    consensus_map: dict[str, tuple[ScreenerAnalysisConsensus | None, list[str]]] = {}
    if market in {"kr", "us"}:
        sem = asyncio.Semaphore(4)

        async def _one(symbol: str) -> None:
            async with sem:
                payload = await _opinion_payload(
                    opinion_provider, symbol=symbol, market=market
                )
                consensus_map[symbol] = normalize_consensus_payload(payload)

        await asyncio.gather(*[_one(symbol) for symbol in symbols])

    enriched: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        consensus, warnings = consensus_map.get(symbol, (None, []))
        rsi14 = rsi_map.get(symbol)
        if consensus is not None:
            summary["consensusSucceeded"] += 1
        if rsi14 is not None:
            summary["rsiSucceeded"] += 1
        context = ScreenerAnalysisContext(
            consensus=consensus,
            rsi14=rsi14,
            dataState=(
                "fresh"
                if consensus is not None and rsi14 is not None
                else "partial"
                if consensus is not None or rsi14 is not None
                else "missing"
            ),
            warnings=warnings,
        )
        enriched.append(
            {
                **row,
                "analystLabel": build_analyst_label(consensus, warnings=warnings),
                "analysisContext": context.model_dump(mode="json"),
            }
        )

    return {"results": enriched, "summary": summary}
