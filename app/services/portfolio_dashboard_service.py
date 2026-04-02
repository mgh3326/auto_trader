from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl
from app.models.trade_journal import JournalStatus, TradeJournal


class PortfolioDashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _serialize_journal_snapshot(
        self,
        journal: TradeJournal,
        *,
        current_price: float | None = None,
    ) -> dict[str, Any]:
        serialized = {
            "id": journal.id,
            "symbol": journal.symbol,
            "instrument_type": journal.instrument_type.value
            if hasattr(journal.instrument_type, "value")
            else str(journal.instrument_type),
            "side": journal.side,
            "entry_price": float(journal.entry_price)
            if journal.entry_price is not None
            else None,
            "quantity": float(journal.quantity)
            if journal.quantity is not None
            else None,
            "amount": float(journal.amount) if journal.amount is not None else None,
            "thesis": journal.thesis,
            "strategy": journal.strategy,
            "target_price": float(journal.target_price)
            if journal.target_price is not None
            else None,
            "stop_loss": float(journal.stop_loss)
            if journal.stop_loss is not None
            else None,
            "min_hold_days": journal.min_hold_days,
            "hold_until": journal.hold_until.isoformat()
            if journal.hold_until
            else None,
            "indicators_snapshot": journal.indicators_snapshot,
            "status": journal.status,
            "trade_id": journal.trade_id,
            "exit_price": float(journal.exit_price)
            if journal.exit_price is not None
            else None,
            "exit_date": journal.exit_date.isoformat() if journal.exit_date else None,
            "exit_reason": journal.exit_reason,
            "pnl_pct": float(journal.pnl_pct) if journal.pnl_pct is not None else None,
            "account": journal.account,
            "notes": journal.notes,
            "created_at": journal.created_at.isoformat()
            if journal.created_at
            else None,
            "updated_at": journal.updated_at.isoformat()
            if journal.updated_at
            else None,
        }

        if current_price is not None and current_price > 0:
            if serialized["target_price"] is not None:
                serialized["target_distance_pct"] = round(
                    (serialized["target_price"] - current_price) / current_price * 100,
                    2,
                )
            if serialized["stop_loss"] is not None:
                serialized["stop_distance_pct"] = round(
                    (serialized["stop_loss"] - current_price) / current_price * 100,
                    2,
                )

        return serialized

    async def get_latest_journal_snapshot(
        self,
        symbol: str,
        *,
        current_price: float | None = None,
    ) -> dict[str, Any] | None:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.status.in_([JournalStatus.draft, JournalStatus.active]),
            )
            .order_by(desc(TradeJournal.created_at))
            .limit(1)
        )
        result = await self.db.execute(stmt)
        journal = result.scalars().first()

        if journal is None:
            return None

        return self._serialize_journal_snapshot(journal, current_price=current_price)

    async def get_journals_batch(
        self,
        symbols: list[str],
        *,
        current_prices: dict[str, float | None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        unique_symbols = sorted({symbol for symbol in symbols if symbol})
        if not unique_symbols:
            return {}

        stmt = select(TradeJournal).where(
            TradeJournal.symbol.in_(unique_symbols),
            TradeJournal.status.in_([JournalStatus.draft, JournalStatus.active]),
        )
        result = await self.db.execute(stmt)
        journals = result.scalars().all()

        latest_by_symbol: dict[str, TradeJournal] = {}
        for journal in journals:
            existing = latest_by_symbol.get(journal.symbol)
            if existing is None or journal.created_at > existing.created_at:
                latest_by_symbol[journal.symbol] = journal

        return {
            symbol: self._serialize_journal_snapshot(
                journal,
                current_price=(current_prices or {}).get(symbol),
            )
            for symbol, journal in latest_by_symbol.items()
        }

    async def get_cash_snapshot(self) -> dict[str, Any]:
        raw_data = await get_available_capital_impl(include_manual=True)

        accounts_raw = raw_data.get("accounts", [])
        manual_cash_raw = raw_data.get("manual_cash")
        summary_raw = raw_data.get("summary", {})
        errors = raw_data.get("errors", [])

        accounts: dict[str, Any] = {
            "kis_krw": None,
            "kis_usd": None,
            "upbit_krw": None,
        }

        for acc in accounts_raw:
            account_key = acc.get("account", "")
            currency = acc.get("currency", "")

            if account_key == "kis_domestic" and currency == "KRW":
                accounts["kis_krw"] = {
                    "broker": acc.get("broker"),
                    "account_name": acc.get("account_name"),
                    "currency": currency,
                    "balance": acc.get("balance"),
                    "orderable": acc.get("orderable"),
                    "formatted": acc.get("formatted"),
                }
            elif account_key == "kis_overseas" and currency == "USD":
                accounts["kis_usd"] = {
                    "broker": acc.get("broker"),
                    "account_name": acc.get("account_name"),
                    "currency": currency,
                    "balance": acc.get("balance"),
                    "orderable": acc.get("orderable"),
                    "krw_equivalent": acc.get("krw_equivalent"),
                    "formatted": acc.get("formatted"),
                }
            elif account_key == "upbit" and currency == "KRW":
                accounts["upbit_krw"] = {
                    "broker": acc.get("broker"),
                    "account_name": acc.get("account_name"),
                    "currency": currency,
                    "balance": acc.get("balance"),
                    "orderable": acc.get("orderable"),
                    "formatted": acc.get("formatted"),
                }

        manual_cash = None
        if manual_cash_raw:
            manual_cash = {
                "amount": manual_cash_raw.get("amount"),
                "updated_at": manual_cash_raw.get("updated_at"),
                "stale_warning": manual_cash_raw.get("stale_warning"),
            }

        return {
            "accounts": accounts,
            "manual_cash": manual_cash,
            "summary": {
                "total_available_krw": summary_raw.get("total_orderable_krw"),
                "exchange_rate_usd_krw": summary_raw.get("exchange_rate_usd_krw"),
                "as_of": summary_raw.get("as_of"),
            },
            "errors": errors,
        }
