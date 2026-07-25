"""ROB-629 B2: market_cap backfill + liquidity filter for the KR foreigners
(foreign_net_buy / foreign_net_sell) ranking.

The KIS foreign-buying-rank payload reliably carries the foreign net-flow KRW
value (frgn_ntby_tr_pbmn -> foreign_net_amount) but usually OMITS market cap.
So we (1) backfill market_cap from invest_kr_fundamentals_snapshots, falling
back to shares_outstanding x price, honest null when neither is available; and
(2) drop clear-junk illiquid rows using the ALWAYS-PRESENT foreign_net_amount
as the primary signal (NOT the null-prone market_cap)."""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import to_optional_float as _to_optional_float
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """Parse a float env override, falling back to ``default`` (logging a
    warning) on missing/blank/non-numeric input.

    These constants are read at IMPORT time, and this module is imported at the
    top of ``analysis_tool_handlers``. A bare ``float(os.getenv(...))`` would
    raise ``ValueError`` at import on a bad operator value, taking down ALL
    get_top_stocks rankings — not just KR foreigners. Fail soft to the default
    instead.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to default %s", name, raw, default)
        return default


# Operator-tunable. Foreign net-flow KRW magnitude below this == clear junk.
MIN_FOREIGN_NET_AMOUNT_KRW: float = _env_float(
    "FOREIGNERS_MIN_NET_AMOUNT_KRW",
    100000000.0,  # 1억 KRW
)
# Optional market-cap floor, applied ONLY where market_cap is known (never
# excludes a row just because cap is null).
MIN_MARKET_CAP_KRW: float = _env_float(
    "FOREIGNERS_MIN_MARKET_CAP_KRW",
    30000000000.0,  # 300억 KRW
)

_FOREIGNERS_RANKING_TYPES: frozenset[str] = frozenset(
    {"foreign_net_buy", "foreign_net_sell", "foreigners"}
)


def is_foreigners_ranking(ranking_type: str) -> bool:
    return (ranking_type or "").strip().lower() in _FOREIGNERS_RANKING_TYPES


def _row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "").strip()


def _abs_foreign_amount(row: dict[str, Any]) -> float | None:
    """Magnitude of foreign net flow (KRW), read ONLY from B1's
    ``foreign_net_amount`` (frgn_ntby_tr_pbmn).

    F6: post-B1 ``_map_kr_foreign_row`` ALWAYS emits ``foreign_net_amount`` and a
    DISTINCT ``trade_amount`` sourced from ``acml_tr_pbmn`` (whole-market
    accumulated trade value — NOT foreign net flow). We deliberately do NOT fall
    back to ``trade_amount``: judging foreign liquidity by whole-market volume
    would be the wrong signal. No row in the shipped pipeline reaches this filter
    without a ``foreign_net_amount`` key, so honest null (excluded) is correct."""
    val = _to_optional_float(row.get("foreign_net_amount"))
    return abs(val) if val is not None else None


async def _fetch_market_cap_maps(
    symbols: list[str],
    *,
    session_factory: Any = AsyncSessionLocal,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(snapshot_market_caps, shares_outstanding_map) for ``symbols``.

    Snapshot caps come from the latest fundamentals partition; shares come from
    kr_symbol_universe only for symbols missing a snapshot cap. fail-open: any
    DB error returns ({}, {}) so the foreigners path never breaks on a DB hiccup
    (market_cap simply stays null)."""
    if not symbols:
        return {}, {}
    try:
        async with session_factory() as db:
            repo = InvestKrFundamentalsSnapshotsRepository(db)
            snapshot_caps = await repo.market_cap_by_symbols(symbols)
            need_fallback = [s for s in symbols if s not in snapshot_caps]
            shares_map: dict[str, Decimal] = {}
            if need_fallback:
                rows = (
                    await db.execute(
                        sa.select(
                            KRSymbolUniverse.symbol,
                            KRSymbolUniverse.shares_outstanding,
                        ).where(KRSymbolUniverse.symbol.in_(need_fallback))
                    )
                ).all()
                shares_map = {
                    r.symbol: r.shares_outstanding
                    for r in rows
                    if r.shares_outstanding is not None
                }
            return snapshot_caps, shares_map
    except Exception:  # noqa: BLE001 — fail-open, leave market_cap untouched
        logger.warning("foreigners market_cap fetch failed", exc_info=True)
        return {}, {}


def apply_market_cap_backfill(
    rows: list[dict[str, Any]],
    *,
    snapshot_caps: dict[str, Decimal],
    shares_map: dict[str, Decimal],
) -> None:
    """Pure in-place backfill of market_cap with provenance, NEVER fabricated.

    Precedence: keep existing (KIS payload) value -> fundamentals snapshot cap
    -> shares_outstanding x price -> honest null."""
    for row in rows:
        existing = row.get("market_cap")
        if existing is not None:
            row.setdefault("market_cap_source", "kis_payload")
            continue
        symbol = _row_symbol(row)
        cap = snapshot_caps.get(symbol)
        if cap is not None:
            row["market_cap"] = float(cap)
            row["market_cap_source"] = "fundamentals_snapshot"
            continue
        shares = shares_map.get(symbol)
        price = _to_optional_float(row.get("price"))
        if shares is not None and price is not None:
            row["market_cap"] = float(shares) * price
            row["market_cap_source"] = "shares_outstanding_x_price"
            continue
        row["market_cap"] = None
        row["market_cap_source"] = None


async def backfill_foreigners_market_cap(
    rows: list[dict[str, Any]],
    *,
    session_factory: Any = AsyncSessionLocal,
) -> None:
    """Bounded (top-N rows already clamped to <=50) batched market_cap backfill."""
    symbols = list(dict.fromkeys(s for r in rows if (s := _row_symbol(r))))
    snapshot_caps, shares_map = await _fetch_market_cap_maps(
        symbols, session_factory=session_factory
    )
    apply_market_cap_backfill(rows, snapshot_caps=snapshot_caps, shares_map=shares_map)


def filter_illiquid_foreigners(
    rows: list[dict[str, Any]],
    *,
    include_illiquid: bool = False,
    min_foreign_net_amount_krw: float = MIN_FOREIGN_NET_AMOUNT_KRW,
    min_market_cap_krw: float | None = MIN_MARKET_CAP_KRW,
) -> tuple[list[dict[str, Any]], int]:
    """Default-ON liquidity filter. Robust signal = |foreign_net_amount| (KRW,
    always present), with an OPTIONAL market_cap floor applied only where cap is
    known. ``include_illiquid=True`` bypasses. Returns (kept_rows, excluded)."""
    if include_illiquid:
        return list(rows), 0
    kept: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        amount = _abs_foreign_amount(row)
        if amount is None or amount < min_foreign_net_amount_krw:
            excluded += 1
            continue
        cap = _to_optional_float(row.get("market_cap"))
        if (
            min_market_cap_krw is not None
            and cap is not None
            and cap < min_market_cap_krw
        ):
            excluded += 1
            continue
        kept.append(row)
    return kept, excluded
