import logging
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert
from app.models.kr_stock_warnings import KRStockWarning
from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossWarningInfo

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: object) -> str | None:
    normalized = str(symbol or "").strip().upper()
    return normalized or None


def _holding_market(item: Any) -> str | None:
    market_country = str(getattr(item, "market_country", "") or "").strip().upper()
    if market_country == "KR":
        return "kr"
    if market_country == "US":
        return "us"
    return None


def _parse_warning_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _warning_row(
    *,
    market: str,
    symbol: str,
    warning: TossWarningInfo,
    fetched_at: datetime,
) -> KRStockWarning:
    return KRStockWarning(
        market=market,
        symbol=symbol,
        warning_type=warning.warning_type,
        exchange=warning.exchange,
        start_date=_parse_warning_date(warning.start_date),
        end_date=_parse_warning_date(warning.end_date),
        source="toss_openapi",
        fetched_at=fetched_at,
    )


async def _resolve_default_symbols(
    db: AsyncSession,
    client: TossReadClient,
    *,
    market: str,
) -> tuple[list[str], list[str]]:
    symbols: set[str] = set()
    errors: list[str] = []

    try:
        holdings = await client.holdings()
        for item in getattr(holdings, "items", []):
            if _holding_market(item) != market:
                continue
            if symbol := _normalize_symbol(getattr(item, "symbol", "")):
                symbols.add(symbol)
    except Exception as exc:
        logger.warning(
            "Failed to resolve Toss holdings for warnings sync: %s",
            exc,
            exc_info=True,
        )
        errors.append(f"holdings: {exc}")

    market_type = MarketType.KR if market == "kr" else MarketType.US
    try:
        stmt = (
            sa.select(ManualHolding.ticker)
            .join(BrokerAccount)
            .where(
                ManualHolding.market_type == market_type,
                ManualHolding.quantity > 0,
                BrokerAccount.is_active.is_(True),
            )
            .order_by(ManualHolding.ticker)
        )
        result = await db.execute(stmt)
        for row in result.all():
            if symbol := _normalize_symbol(row[0]):
                symbols.add(symbol)
    except Exception as exc:
        logger.warning(
            "Failed to resolve manual holdings for warnings sync market=%s: %s",
            market,
            exc,
            exc_info=True,
        )
        errors.append(f"manual_holdings: {exc}")

    try:
        stmt = (
            sa.select(InvestmentWatchAlert.symbol)
            .where(
                InvestmentWatchAlert.market == market,
                InvestmentWatchAlert.target_kind == "asset",
                InvestmentWatchAlert.status == "active",
                InvestmentWatchAlert.valid_until >= datetime.now(UTC),
            )
            .order_by(InvestmentWatchAlert.symbol)
        )
        result = await db.execute(stmt)
        for row in result.all():
            if symbol := _normalize_symbol(row[0]):
                symbols.add(symbol)
    except Exception as exc:
        logger.warning(
            "Failed to resolve watch symbols for warnings sync market=%s: %s",
            market,
            exc,
            exc_info=True,
        )
        errors.append(f"watch_alerts: {exc}")

    return sorted(symbols), errors


async def sync_toss_warnings(
    db: AsyncSession,
    client: TossReadClient,
    *,
    market: str,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """
    Syncs stock warnings from Toss API to the database.
    Semantic: Per-symbol replace.
    For each symbol, we delete existing warnings and insert the current ones.
    """
    if market not in ("kr", "us"):
        raise ValueError(f"Unsupported market for warnings sync: {market}")

    errors = []

    # Resolve symbols if not provided
    if symbols is None:
        symbols, resolution_errors = await _resolve_default_symbols(
            db,
            client,
            market=market,
        )
        errors.extend(resolution_errors)
    else:
        symbols = [symbol for raw in symbols if (symbol := _normalize_symbol(raw))]

    processed = 0
    inserted = 0
    deleted = 0

    for symbol in symbols:
        try:
            # 1. Fetch warnings from Toss client
            warnings = await client.warnings(symbol)
            fetched_at = datetime.now(UTC)
            warning_rows = [
                _warning_row(
                    market=market,
                    symbol=symbol,
                    warning=warning,
                    fetched_at=fetched_at,
                )
                for warning in warnings
            ]

            # 2. Delete existing warnings for this symbol from DB
            delete_stmt = sa.delete(KRStockWarning).where(
                KRStockWarning.market == market, KRStockWarning.symbol == symbol
            )
            del_res = await db.execute(delete_stmt)
            deleted += del_res.rowcount or 0

            # 3. Insert new warnings
            for warning_row in warning_rows:
                db.add(warning_row)
            inserted += len(warning_rows)
            processed += 1

        except Exception as exc:
            logger.error(
                "Failed to sync Toss warnings for symbol=%s: %s",
                symbol,
                exc,
                exc_info=True,
            )
            errors.append(f"{symbol}: {exc}")

    # Commit after processing
    await db.commit()

    return {
        "market": market,
        "symbols_processed": processed,
        "warnings_inserted": inserted,
        "warnings_deleted_count": deleted,
        "errors": errors,
    }
