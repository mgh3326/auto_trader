# app/services/trade_journal_coverage_service.py
"""ROB-120 — Read-only coverage aggregator for the thesis journal page.

Joins (live + manual + Upbit) holdings against the latest open journal
per (symbol, account_type='live') and the latest research_summary for
that symbol's stock_info row. Produces one row per holding.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_pipeline import ResearchSession, ResearchSummary
from app.models.trade_journal import TradeJournal
from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCoverageRow,
)
from app.services.brokers.upbit import client as upbit_client
from app.services.merged_portfolio_service import MergedPortfolioService

logger = logging.getLogger(__name__)
_OPEN_STATUSES = ("draft", "active")


class TradeJournalCoverageService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_coverage(
        self,
        *,
        user_id: int,
        market_filter: str | None = None,
    ) -> JournalCoverageResponse:
        merged_svc = MergedPortfolioService(self.db)
        holdings: list[Any] = []

        # 1. Domestic
        if market_filter in (None, "KR"):
            try:
                kr = await merged_svc.get_merged_portfolio_domestic(user_id)
                holdings.extend(kr)
            except Exception as exc:
                logger.warning(f"Failed to fetch KR holdings: {exc}")

        # 2. Overseas
        if market_filter in (None, "US"):
            try:
                us = await merged_svc.get_merged_portfolio_overseas(user_id)
                holdings.extend(us)
            except Exception as exc:
                logger.warning(f"Failed to fetch US holdings: {exc}")

        # 3. Crypto (Upbit)
        if market_filter in (None, "CRYPTO"):
            try:
                upbit_coins = await upbit_client.fetch_my_coins()
                # We need prices for evaluation/weight
                tickers = [f"KRW-{c['currency']}" for c in upbit_coins if c['currency'] != "KRW"]
                prices = await upbit_client.fetch_multiple_current_prices(tickers)
                
                for coin in upbit_coins:
                    currency = str(coin.get("currency", "")).upper()
                    if currency == "KRW":
                        continue
                    qty = float(coin.get("balance", 0) or 0) + float(coin.get("locked", 0) or 0)
                    if qty <= 0:
                        continue
                    symbol = f"KRW-{currency}"
                    price = prices.get(symbol, 0.0)
                    eval_ = qty * price
                    
                    # Mock a structure compatible with the loop below
                    holdings.append(type('obj', (object,), {
                        "ticker": symbol,
                        "name": currency,
                        "market_type": type('obj', (object,), {'value': 'CRYPTO'}),
                        "total_quantity": qty,
                        "evaluation": eval_,
                        "instrument_type": "crypto"
                    }))
            except Exception as exc:
                logger.warning(f"Failed to fetch Upbit holdings: {exc}")

        total_value = float(
            sum((getattr(h, "evaluation", 0.0) or 0.0) for h in holdings)
        )
        rows: list[JournalCoverageRow] = []
        warnings: list[str] = []

        for h in holdings:
            symbol = str(getattr(h, "ticker", "") or "")
            if not symbol:
                continue
            quantity = float(getattr(h, "total_quantity", 0.0) or 0.0)
            if quantity <= 0:
                continue

            evaluation = float(getattr(h, "evaluation", 0.0) or 0.0)
            weight = (evaluation / total_value * 100.0) if total_value else None
            market_value = str(getattr(getattr(h, "market_type", None), "value", "KR"))

            journal = await self._latest_open_journal(symbol)
            summary_row = await self._latest_summary_for_symbol(symbol)

            journal_status = "present" if journal is not None else "missing"
            decision: str | None = None
            session_id: int | None = None
            summary_id: int | None = None
            if summary_row is not None:
                summary_id, session_id, decision = summary_row

            conflict = bool(
                journal is not None
                and journal.status == "active"
                and decision == "sell"
            )

            meta: dict[str, Any] | None = (
                journal.extra_metadata if journal is not None else None
            )
            row_session_id = (
                meta.get("research_session_id") if isinstance(meta, dict) else None
            )
            row_summary_id = (
                meta.get("research_summary_id") if isinstance(meta, dict) else None
            )

            rows.append(
                JournalCoverageRow(
                    symbol=symbol,
                    name=getattr(h, "name", None),
                    market=market_value,  # type: ignore[arg-type]
                    instrument_type=getattr(h, "instrument_type", None),
                    quantity=quantity,
                    position_weight_pct=weight,
                    journal_status=journal_status,  # type: ignore[arg-type]
                    journal_id=journal.id if journal else None,
                    thesis=journal.thesis if journal else None,
                    target_price=float(journal.target_price)
                    if journal and journal.target_price is not None
                    else None,
                    stop_loss=float(journal.stop_loss)
                    if journal and journal.stop_loss is not None
                    else None,
                    min_hold_days=journal.min_hold_days if journal else None,
                    hold_until=journal.hold_until.isoformat()
                    if journal and journal.hold_until
                    else None,
                    latest_research_session_id=row_session_id or session_id,
                    latest_research_summary_id=row_summary_id or summary_id,
                    latest_summary_decision=decision,  # type: ignore[arg-type]
                    thesis_conflict_with_summary=conflict,
                )
            )

        return JournalCoverageResponse(
            generated_at=datetime.now(UTC).isoformat(),
            total=len(rows),
            rows=rows,
            warnings=warnings,
        )

    async def _latest_open_journal(self, symbol: str) -> TradeJournal | None:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.account_type == "live",
                TradeJournal.status.in_(_OPEN_STATUSES),
            )
            .order_by(desc(TradeJournal.created_at))
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _latest_summary_for_symbol(
        self, symbol: str
    ) -> tuple[int, int, str] | None:
        # research_summaries → research_sessions → stock_info.symbol
        from app.models.analysis import StockInfo  # local import to avoid cycle

        stmt = (
            select(ResearchSummary.id, ResearchSession.id, ResearchSummary.decision)
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .where(StockInfo.symbol == symbol)
            .order_by(desc(ResearchSummary.executed_at))
            .limit(1)
        )
        row = (await self.db.execute(stmt)).first()
        if row is None:
            return None
        return (int(row[0]), int(row[1]), str(row[2]))
