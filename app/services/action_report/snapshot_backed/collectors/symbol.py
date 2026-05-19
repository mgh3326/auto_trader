"""Symbol snapshot collector (read-only, optional).

Reads ``stock_info`` master rows for the symbols the caller asked about.
If ``request.symbols`` is empty/None we have no scope to read against, so
the collector returns ``unavailable`` and the optional bucket records it
in ``unavailable_sources`` — consistent with the policy that optional
kinds degrade the bundle to ``partial`` but never block.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockInfo
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class SymbolSnapshotCollector:
    """Optional ``symbol`` collector backed by ``stock_info``."""

    snapshot_kind: str = "symbol"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        symbols = list(request.symbols or [])
        if not symbols:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no symbols supplied; symbol snapshot has no scope",
                    as_of=now,
                )
            ]

        try:
            stmt = select(StockInfo).where(StockInfo.symbol.in_(symbols))
            rows = (await self._session.execute(stmt)).scalars().all()
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"stock_info query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        results: list[SnapshotCollectResult] = []
        seen_symbols: set[str] = set()
        for row in rows:
            seen_symbols.add(row.symbol)
            payload: dict[str, Any] = {
                "symbol": row.symbol,
                "name": row.name,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "sector": row.sector,
                "market_cap": row.market_cap,
                "is_active": row.is_active,
            }
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    symbol=row.symbol,
                    coverage={"resolved": True},
                )
            )

        missing = [s for s in symbols if s not in seen_symbols]
        if missing:
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"missing_symbols": missing},
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(missing)},
                )
            )

        if not results:
            # Every symbol missed — return a single partial summary so the
            # caller still records the attempt without forcing 'unavailable'.
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"missing_symbols": symbols},
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(symbols)},
                )
            )
        return results
