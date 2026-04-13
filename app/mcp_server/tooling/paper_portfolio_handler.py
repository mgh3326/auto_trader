"""Paper trading portfolio handler for MCP tools.

Keeps paper-specific collection/translation logic isolated so that the live
broker tooling files (portfolio_holdings.py, portfolio_cash.py) only need a
single delegation point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

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


__all__ = [
    "PaperAccountSelector",
    "is_paper_account_token",
    "parse_paper_account_token",
    "resolve_paper_position_name",
]
