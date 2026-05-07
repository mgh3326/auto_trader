"""ROB-141 — assemble AccountPanelResponse for /invest/api/account-panel."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_account_panel import (
    AccountPanelMeta,
    AccountPanelResponse,
    WatchSymbol,
)
from app.services.invest_home_service import InvestHomeService
from app.services.invest_view_model.account_visual import all_visuals
from app.services.invest_view_model.relation_resolver import _TYPE_TO_MARKET


async def build_account_panel(
    *, user_id: int, db: AsyncSession, home_service: InvestHomeService,
) -> AccountPanelResponse:
    home = await home_service.get_home(user_id=user_id)
    watch_symbols, watch_available = await _load_watch_symbols(db, user_id=user_id)
    return AccountPanelResponse(
        homeSummary=home.homeSummary,
        accounts=home.accounts,
        groupedHoldings=home.groupedHoldings,
        watchSymbols=watch_symbols,
        sourceVisuals=all_visuals(),
        meta=AccountPanelMeta(
            warnings=home.meta.warnings,
            watchlistAvailable=watch_available,
        ),
    )


async def _load_watch_symbols(
    db: AsyncSession, *, user_id: int
) -> tuple[list[WatchSymbol], bool]:
    try:
        from app.models.trading import Instrument, UserWatchItem  # type: ignore
    except ImportError:
        return [], False

    stmt = (
        select(
            Instrument.symbol, Instrument.type, Instrument.name, UserWatchItem.note,
        )
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
        .order_by(Instrument.type, Instrument.symbol)
    )
    result = await db.execute(stmt)
    items: list[WatchSymbol] = []
    for sym, instrument_type, name, note in result.all():
        if not sym or instrument_type is None:
            continue
        market = _TYPE_TO_MARKET.get(str(instrument_type), "us")
        if market not in ("kr", "us", "crypto"):
            continue
        items.append(
            WatchSymbol(
                symbol=str(sym),
                market=market,  # type: ignore[arg-type]
                displayName=str(name or sym),
                note=note,
            )
        )
    return items, True
