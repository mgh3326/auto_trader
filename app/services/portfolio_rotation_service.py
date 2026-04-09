"""Portfolio rotation plan service.

Classifies crypto positions into sell/locked/ignored buckets based on
strategy signals and trade journal context, then fetches screener-based
buy candidates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)

LOCKED_STRATEGIES: frozenset[str] = frozenset({
    "coinmoogi_dca",
    "staking_hold",
    "index_dca",
})
DUST_THRESHOLD_KRW: float = 5_000
PARTIAL_REDUCE_PCT: int = 30
DCA_OVERSOLD_LOSS_THRESHOLD: float = -3.0


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def _fetch_crypto_positions(
    account: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch Upbit crypto positions with current prices and strategy signals."""
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
    )

    positions, errors, _market_filter, _account_filter = (
        await _collect_portfolio_positions(
            account=account,
            market="crypto",
            include_current_price=True,
        )
    )
    return positions, errors


async def _fetch_active_journals() -> dict[str, dict[str, Any]]:
    """Fetch active/draft crypto trade journals, keyed by symbol."""
    session_maker = _session_factory()
    async with session_maker() as session:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.instrument_type == InstrumentType.crypto,
                TradeJournal.status.in_(["draft", "active"]),
            )
            .order_by(TradeJournal.created_at.desc())
        )
        result = await session.execute(stmt)
        journals = result.scalars().all()

    journal_map: dict[str, dict[str, Any]] = {}
    for j in journals:
        symbol = j.symbol
        if symbol in journal_map:
            continue  # keep most recent (ordered desc)
        journal_map[symbol] = {
            "symbol": j.symbol,
            "strategy": j.strategy,
            "status": j.status,
            "hold_until": j.hold_until.isoformat() if j.hold_until else None,
        }
    return journal_map


async def _fetch_buy_candidates(
    held_symbols: set[str],
) -> list[dict[str, Any]]:
    """Fetch screener oversold candidates, excluding already-held symbols."""
    from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl

    try:
        result = await screen_stocks_impl(
            market="crypto",
            strategy="oversold",
            limit=20,
        )
    except Exception as exc:
        logger.warning("Failed to fetch buy candidates: %s", exc)
        return []

    candidates: list[dict[str, Any]] = []
    for row in result.get("results", []):
        symbol = row.get("symbol", "")
        if symbol in held_symbols:
            continue
        candidates.append({
            "symbol": symbol,
            "name": row.get("name", ""),
            "price": row.get("price"),
            "change_rate": row.get("change_rate"),
            "trade_amount_24h": row.get("trade_amount"),
            "screen_reason": ["RSI oversold", "sufficient liquidity"],
        })
        if len(candidates) >= 10:
            break
    return candidates


def _classify_position(
    position: dict[str, Any],
    journal: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Classify a single position into a bucket.

    Returns:
        (bucket_name, detail_dict) where bucket_name is one of:
        "locked", "ignored", "sell", "healthy"
    """
    symbol = position.get("symbol", "")
    name = position.get("name", "")
    evaluation = position.get("evaluation_amount") or 0
    profit_rate = position.get("profit_rate") or 0
    strategy_signal = position.get("strategy_signal")
    journal_strategy = journal.get("strategy") if journal else None

    # 1. Dust check first
    if evaluation < DUST_THRESHOLD_KRW:
        return "ignored", {
            "symbol": symbol,
            "name": name,
            "evaluation_amount": evaluation,
            "ignore_reason": f"dust position (< {DUST_THRESHOLD_KRW:,.0f} KRW)",
        }

    # 2. Locked strategy check
    if journal_strategy and journal_strategy in LOCKED_STRATEGIES:
        return "locked", {
            "symbol": symbol,
            "name": name,
            "journal_strategy": journal_strategy,
            "lock_reason": "locked strategy",
        }

    # 3. Hold-until not expired
    if journal:
        hold_until_str = journal.get("hold_until")
        if hold_until_str:
            try:
                hold_until = datetime.fromisoformat(hold_until_str)
                if hold_until > now_kst():
                    return "locked", {
                        "symbol": symbol,
                        "name": name,
                        "journal_strategy": journal_strategy,
                        "lock_reason": f"hold until {hold_until_str[:10]}",
                    }
            except (ValueError, TypeError):
                pass

    # 4. Sell candidates
    reasons: list[str] = []
    is_stop_loss = False

    # 4a. Strategy signal says sell
    if isinstance(strategy_signal, dict) and strategy_signal.get("action") == "sell":
        signal_reason = strategy_signal.get("reason", "signal")
        reasons.append(f"{signal_reason} signal")
        if signal_reason == "stop_loss":
            is_stop_loss = True

    # 4b. dca_oversold with significant loss
    if journal_strategy == "dca_oversold" and profit_rate < DCA_OVERSOLD_LOSS_THRESHOLD:
        reasons.append("dca_oversold with significant loss")

    # 4c. No journal + negative P&L
    if not journal and profit_rate < 0:
        reasons.append("no active journal")

    if reasons:
        action = "reduce_full" if is_stop_loss else "reduce_partial"
        reduce_pct = 100 if is_stop_loss else PARTIAL_REDUCE_PCT
        return "sell", {
            "symbol": symbol,
            "name": name,
            "current_price": position.get("current_price"),
            "profit_rate": profit_rate,
            "evaluation_amount": evaluation,
            "action": action,
            "reduce_pct": reduce_pct,
            "reason": reasons,
            "journal_strategy": journal_strategy,
        }

    # 5. Healthy — not surfaced
    return "healthy", {}


class PortfolioRotationService:
    """Build rotation plans for crypto portfolios."""

    async def build_rotation_plan(
        self,
        *,
        market: str = "crypto",
        account: str | None = None,
    ) -> dict[str, Any]:
        if market != "crypto":
            return {
                "supported": False,
                "market": market,
                "warning": "Rotation plan is currently supported for crypto only.",
            }

        warnings: list[str] = []

        # 1. Fetch positions
        positions, pos_errors = await _fetch_crypto_positions(account=account)
        for err in pos_errors:
            warnings.append(str(err.get("error", err)))

        # 2. Fetch journals
        try:
            journal_map = await _fetch_active_journals()
        except Exception as exc:
            logger.warning("Failed to fetch journals: %s", exc)
            journal_map = {}
            warnings.append(f"Journal fetch failed: {exc}")

        # 3. Classify positions
        sell_candidates: list[dict[str, Any]] = []
        locked_positions: list[dict[str, Any]] = []
        ignored_positions: list[dict[str, Any]] = []

        held_symbols: set[str] = set()
        for pos in positions:
            symbol = pos.get("symbol", "")
            held_symbols.add(symbol)
            journal = journal_map.get(symbol)
            bucket, detail = _classify_position(pos, journal)
            if bucket == "sell":
                sell_candidates.append(detail)
            elif bucket == "locked":
                locked_positions.append(detail)
            elif bucket == "ignored":
                ignored_positions.append(detail)

        # 4. Fetch buy candidates
        buy_candidates = await _fetch_buy_candidates(held_symbols)

        generated_at = now_kst().isoformat()

        return {
            "supported": True,
            "market": "crypto",
            "account": account or "upbit",
            "generated_at": generated_at,
            "summary": {
                "total_positions": len(positions),
                "actionable_positions": len(sell_candidates),
                "locked_positions": len(locked_positions),
                "ignored_positions": len(ignored_positions),
                "buy_candidates": len(buy_candidates),
            },
            "sell_candidates": sell_candidates,
            "buy_candidates": buy_candidates,
            "locked_positions": locked_positions,
            "ignored_positions": ignored_positions,
            "warnings": warnings,
        }
