"""Paper trading portfolio handler for MCP tools.

Keeps paper-specific collection/translation logic isolated so that the live
broker tooling files (portfolio_holdings.py, portfolio_cash.py) only need a
single delegation point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import (
    INSTRUMENT_TO_MARKET as _INSTRUMENT_TO_MARKET,
)
from app.mcp_server.tooling.shared import (
    normalize_position_symbol as _normalize_position_symbol,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.services.paper_trading_service import PaperTradingService
from app.services.stock_info_service import StockInfoService
from app.services.upbit_symbol_universe_service import (
    UpbitSymbolInactiveError,
    UpbitSymbolNotRegisteredError,
    UpbitSymbolUniverseLookupError,
    get_upbit_korean_name_by_coin,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperAccountSelector:
    """Resolved selector for paper account queries.

    account_name is None when the caller passed the bare "paper" token, which
    means "all active paper accounts".
    """

    account_name: str | None


def _strip(value: str | None) -> str:
    return (value or "").strip()


def _split_paper_token(account: str | None) -> tuple[str, str] | None:
    """Return (head_lower, raw_name) if account is a paper token, else None.

    Tolerates whitespace around the ":" delimiter.
    """
    token = _strip(account)
    if not token:
        return None
    head, sep, raw_name = token.partition(":")
    head_lower = head.strip().lower()
    if head_lower != "paper":
        return None
    if not sep:
        return ("paper", "")
    return ("paper", raw_name)


def is_paper_account_token(account: str | None) -> bool:
    return _split_paper_token(account) is not None


def parse_paper_account_token(account: str | None) -> PaperAccountSelector:
    parts = _split_paper_token(account)
    if parts is None:
        raise ValueError(f"not a paper account token: {account!r}")

    _, raw_name = parts
    name = raw_name.strip()
    return PaperAccountSelector(account_name=name or None)


async def resolve_paper_position_name(
    symbol: str,
    instrument_type: str,
    *,
    db: AsyncSession,
) -> str:
    """Resolve a human-readable name for a paper position.

    Falls back to ``symbol`` when lookup fails or the symbol is unknown, so
    callers always receive a non-empty string.
    """
    if instrument_type in ("equity_kr", "equity_us"):
        try:
            service = StockInfoService(db)
            info = await service.get_stock_info_by_symbol(symbol)
            if info is not None and info.name:
                return str(info.name)
        except Exception as exc:
            logger.debug("Failed to resolve stock_info name for %s: %s", symbol, exc)
        return symbol

    if instrument_type == "crypto":
        # symbol is in "KRW-BTC" form; extract quote currency + coin
        quote, _, coin = symbol.partition("-")
        if not coin:
            return symbol
        try:
            return await get_upbit_korean_name_by_coin(
                coin, quote_currency=quote or "KRW"
            )
        except (
            UpbitSymbolNotRegisteredError,
            UpbitSymbolInactiveError,
            UpbitSymbolUniverseLookupError,
        ):
            return symbol
        except Exception as exc:
            logger.debug("Failed to resolve upbit name for %s: %s", symbol, exc)
            return symbol

    return symbol


def _build_service(db: AsyncSession) -> PaperTradingService:
    """Construction seam so tests can swap in a fake service."""
    return PaperTradingService(db)


async def _resolve_target_accounts(
    service: PaperTradingService,
    selector: PaperAccountSelector,
) -> tuple[list[Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []

    if selector.account_name is None:
        accounts = await service.list_accounts(is_active=True)
        return list(accounts), errors

    account = await service.get_account_by_name(selector.account_name)
    if account is None or not account.is_active:
        errors.append(
            {
                "source": "paper",
                "error": f"paper account not found: {selector.account_name}",
            }
        )
        return [], errors
    return [account], errors


def _paper_position_to_canonical(
    *,
    account_name: str,
    raw_position: dict[str, Any],
    display_name: str,
) -> dict[str, Any]:
    instrument_type = str(raw_position["instrument_type"])
    symbol = _normalize_position_symbol(str(raw_position["symbol"]), instrument_type)

    return {
        "account": f"paper:{account_name}",
        "account_name": account_name,
        "broker": "paper",
        "source": "paper",
        "instrument_type": instrument_type,
        "market": _INSTRUMENT_TO_MARKET.get(instrument_type, instrument_type),
        "symbol": symbol,
        "name": display_name or symbol,
        "quantity": _to_float(raw_position.get("quantity")),
        "avg_buy_price": _to_float(raw_position.get("avg_price")),
        "current_price": _to_optional_float(raw_position.get("current_price")),
        "evaluation_amount": _to_optional_float(raw_position.get("evaluation_amount")),
        "profit_loss": _to_optional_float(raw_position.get("unrealized_pnl")),
        "profit_rate": _to_optional_float(raw_position.get("pnl_pct")),
    }


async def collect_paper_positions(
    *,
    selector: PaperAccountSelector,
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect paper positions in the canonical portfolio shape.

    Parameters
    ----------
    selector
        PaperAccountSelector from ``parse_paper_account_token``.
    market_filter
        One of ``equity_kr`` / ``equity_us`` / ``crypto`` / ``None`` (all).
    """
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        service = _build_service(db)

        target_accounts, lookup_errors = await _resolve_target_accounts(
            service, selector
        )
        errors.extend(lookup_errors)

        for account in target_accounts:
            try:
                raw_positions = await service.get_positions(
                    account_id=account.id, market=market_filter
                )
            except Exception as exc:
                errors.append(
                    {
                        "source": "paper",
                        "account": f"paper:{account.name}",
                        "error": str(exc),
                    }
                )
                continue

            for raw in raw_positions:
                # Defensive post-filter: the service is expected to filter by
                # market at the query layer, but we also filter here so the
                # canonical shape is guaranteed regardless of the service impl.
                if (
                    market_filter is not None
                    and str(raw.get("instrument_type")) != market_filter
                ):
                    continue
                try:
                    display_name = await resolve_paper_position_name(
                        str(raw["symbol"]),
                        str(raw["instrument_type"]),
                        db=db,
                    )
                except Exception as exc:
                    logger.debug(
                        "name resolution failed for paper %s: %s",
                        raw["symbol"],
                        exc,
                    )
                    display_name = str(raw["symbol"])
                positions.append(
                    _paper_position_to_canonical(
                        account_name=account.name,
                        raw_position=raw,
                        display_name=display_name,
                    )
                )

    return positions, errors


__all__ = [
    "PaperAccountSelector",
    "is_paper_account_token",
    "parse_paper_account_token",
    "resolve_paper_position_name",
    "collect_paper_positions",
    "_build_service",
]
