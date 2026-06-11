from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.symbol import to_yahoo_symbol
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.schemas.invest_screener import (
    ScreenerAnalysisConsensus,
    ScreenerAnalysisContext,
)
from app.services.us_sector_korean_map import korean_sector_label

OpinionProvider = Callable[..., Any]


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
        if consensus.avgTargetPrice < consensus.currentPrice and (
            consensus.buyCount or 0
        ) > ((consensus.holdCount or 0) + (consensus.sellCount or 0)):
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


_SECTOR_FETCH_TIMEOUT = 4.5
_SECTOR_FETCH_CONCURRENCY = 4


async def _fetch_kr_sector(code: str) -> tuple[str | None, str | None]:
    """Naver 종목 메인 페이지에서 (업종번호, 한글 업종명)을 추출."""
    from app.services.naver_finance.valuation import (
        _fetch_html,
        _parse_industry_info,
    )

    soup = await _fetch_html(
        "https://finance.naver.com/item/main.naver", params={"code": code}
    )
    info = _parse_industry_info(soup)
    return info.get("sector_no"), info.get("sector")


async def _fetch_us_sector(symbol: str) -> tuple[str | None, str | None]:
    """yfinance info에서 (industry, sector) 영문 원문을 추출 (DB 심볼 입력)."""
    import yfinance as yf

    def _sync() -> tuple[str | None, str | None]:
        info = yf.Ticker(to_yahoo_symbol(symbol)).info or {}
        return info.get("industry") or None, info.get("sector") or None

    return await asyncio.to_thread(_sync)


async def _sector_labels_for_page(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    market: str,
    symbols: list[str],
    fetch_kr_sector: Callable[..., Any],
    fetch_us_sector: Callable[..., Any],
) -> dict[str, str]:
    """워밍된 섹터는 DB에서, NULL은 lazy fetch→persist 후 표시 라벨을 반환.

    실패는 전부 삼키고 해당 심볼만 빠진 dict를 반환한다(fail-open) —
    스크리너 응답 자체는 영향받지 않는다.
    """
    if not symbols or market not in {"kr", "us"}:
        return {}

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.symbol_sectors import SymbolSector
    from app.models.us_symbol_universe import USSymbolUniverse
    from app.services.symbol_sectors_service import (
        assign_symbol_sector,
        get_or_create_sector,
    )

    universe = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    labels: dict[str, str] = {}
    missing: list[str] = []

    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(universe.symbol, SymbolSector.name_kr, SymbolSector.name_en)
                .outerjoin(SymbolSector, universe.sector_id == SymbolSector.id)
                .where(universe.symbol.in_(symbols))
            )
        ).all()
        known = {row.symbol for row in rows}
        for row in rows:
            label = row.name_kr or row.name_en
            if label:
                labels[row.symbol] = label
            else:
                missing.append(row.symbol)
        # universe에 없는 심볼은 fetch 대상에서 제외(assign이 어차피 불가)
        missing = [s for s in missing if s in known]

    if not missing:
        return labels

    sem = asyncio.Semaphore(_SECTOR_FETCH_CONCURRENCY)
    fetched: dict[str, tuple[str, str | None, str | None]] = {}
    # 값: (source_key, name_kr, name_en)

    async def _one(symbol: str) -> None:
        async with sem:
            try:
                if market == "kr":
                    no, name = await asyncio.wait_for(
                        fetch_kr_sector(symbol), timeout=_SECTOR_FETCH_TIMEOUT
                    )
                    if no and name:
                        fetched[symbol] = (str(no), name, None)
                else:
                    industry, sector = await asyncio.wait_for(
                        fetch_us_sector(symbol), timeout=_SECTOR_FETCH_TIMEOUT
                    )
                    raw = industry or sector
                    if raw:
                        fetched[symbol] = (raw, korean_sector_label(raw), raw)
            except Exception:  # noqa: BLE001 — per-symbol fail-open
                return

    await asyncio.gather(*[_one(symbol) for symbol in missing])
    if not fetched:
        return labels

    source = "naver_upjong" if market == "kr" else "yfinance_industry"
    try:
        async with session_factory() as db:
            for symbol, (source_key, name_kr, name_en) in fetched.items():
                sector_id = await get_or_create_sector(
                    db,
                    market=market,
                    source=source,
                    source_key=source_key,
                    name_kr=name_kr,
                    name_en=name_en,
                )
                await assign_symbol_sector(
                    db, market=market, symbol=symbol, sector_id=sector_id
                )
                label = name_kr or name_en
                if label:
                    labels[symbol] = label
            await db.commit()
    except Exception:  # noqa: BLE001 — persist 실패도 fail-open (라벨만 미반영)
        return labels
    return labels


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
                sa.select(
                    sa.func.max(InvestCryptoScreenerSnapshot.snapshot_date)
                ).where(InvestCryptoScreenerSnapshot.symbol.in_(symbols))
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
        return {row.symbol: float(row.rsi) for row in rows if row.rsi is not None}

    return {}


async def enrich_snapshot_page(
    *,
    rows: list[dict[str, Any]],
    market: str,
    session_factory: async_sessionmaker[AsyncSession],
    opinion_provider: OpinionProvider = handle_get_investment_opinions,
    fetch_kr_sector: Callable[..., Any] = _fetch_kr_sector,
    fetch_us_sector: Callable[..., Any] = _fetch_us_sector,
) -> dict[str, Any]:
    symbols = [
        str(row.get("symbol") or "").strip() for row in rows if row.get("symbol")
    ]
    symbols = list(dict.fromkeys(symbols))
    summary = {
        "attempted": len(rows),
        "consensusSucceeded": 0,
        "rsiSucceeded": 0,
        "sectorResolved": 0,
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

    sector_labels: dict[str, str] = {}
    try:
        sector_labels = await _sector_labels_for_page(
            session_factory=session_factory,
            market=market,
            symbols=symbols,
            fetch_kr_sector=fetch_kr_sector,
            fetch_us_sector=fetch_us_sector,
        )
    except Exception:  # noqa: BLE001
        summary["warnings"].append("sector_enrichment_unavailable")

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

        sector_label = sector_labels.get(symbol)
        if sector_label is not None:
            summary["sectorResolved"] += 1

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
                **(
                    {"category": sector_label}
                    if sector_label and (row.get("category") or "-") == "-"
                    else {}
                ),
                "analystLabel": build_analyst_label(consensus, warnings=warnings),
                "analysisContext": context.model_dump(mode="json"),
            }
        )

    return {"results": enriched, "summary": summary}
