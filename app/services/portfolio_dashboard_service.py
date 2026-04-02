from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl
from app.models.trade_journal import JournalStatus, TradeJournal


class PortfolioDashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

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
                    (serialized["stop_loss"] - current_price) / current_price * 100, 2
                )

        return serialized

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

    async def calculate_allocation_metrics(
        self, positions: list[dict[str, Any]], cash_summary: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Calculate weight and weight warnings for positions."""
        total_available = (cash_summary.get("summary") or {}).get("total_available_krw")
        if total_available is None:
            return positions

        total_evaluation = sum(p.get("evaluation") or 0.0 for p in positions)
        total_capital = total_evaluation + total_available

        if total_capital <= 0:
            return positions

        for p in positions:
            evaluation = p.get("evaluation") or 0.0
            weight = evaluation / total_capital
            p["weight"] = weight
            # Warning if a single position > 25% of total capital
            if weight > 0.25:
                p["weight_warning"] = f"비중 과다 ({weight*100:.1f}%)"

        return positions

    async def enrich_positions_with_journal_status(
        self, positions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Enrich positions with basic journal status (target/stop hits)."""
        if not positions:
            return positions

        symbols = [p["symbol"] for p in positions]
        stmt = select(TradeJournal).where(
            TradeJournal.symbol.in_(symbols),
            TradeJournal.status.in_([JournalStatus.draft, JournalStatus.active]),
        )
        result = await self.db.execute(stmt)
        journals = result.scalars().all()

        journal_map = {}
        for j in journals:
            # Prefer the latest one
            if j.symbol not in journal_map or j.created_at > journal_map[j.symbol].created_at:
                journal_map[j.symbol] = j

        for p in positions:
            j = journal_map.get(p["symbol"])
            if j:
                p["has_journal"] = True
                p["target_price"] = float(j.target_price) if j.target_price else None
                p["stop_loss"] = float(j.stop_loss) if j.stop_loss else None

                current_price = p.get("current_price")
                if current_price and current_price > 0:
                    if p["target_price"]:
                        p["target_dist_pct"] = (p["target_price"] - current_price) / current_price * 100
                    if p["stop_loss"]:
                        p["stop_dist_pct"] = (p["stop_loss"] - current_price) / current_price * 100

        return positions

    async def simulate_sell_order(
        self,
        *,
        user_id: int,
        symbol: str,
        market_type: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        """Simulate a sell order and return the expected outcome."""
        # This is a stub for now, but returns the expected structure
        return {
            "success": True,
            "symbol": symbol,
            "market_type": market_type,
            "order_quantity": quantity,
            "order_price": price,
            "expected_proceeds": quantity * price,
            "status": "simulated",
            "message": "매도 시뮬레이션 결과입니다. 실제 주문은 발생하지 않았습니다.",
        }
