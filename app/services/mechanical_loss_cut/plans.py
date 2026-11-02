"""Write a lot's exit plan when a buy is accepted (ROB-1460).

Runs after the broker accepted the order, so a failure here must never change
what the caller sees: it logs a code and returns. A lot left without a plan gets
a backfilled one from the next evaluation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.services.mechanical_loss_cut.gate import POLICY_MARKET
from app.services.mechanical_loss_cut.positions import (
    load_bars,
    load_position,
    lot_symbol,
)
from app.services.mechanical_loss_cut.rules import (
    Bar,
    ExitPlanInputError,
    LotKey,
    add_plan,
    entry_plan,
    is_parking_instrument,
    parse_exit_plan_input,
)
from app.services.mechanical_loss_cut.store import MechanicalLossCutStore
from app.services.trading_policy_service import load_trading_policy

logger = logging.getLogger(__name__)

#: Where ``order_proposal_create`` keeps the stop stated for a buy proposal.
EXIT_PLAN_SOURCE_KEY = "exit_plan"

_PLANNABLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("kis_live", "equity_kr"),
        ("kis_live", "equity_us"),
        ("toss_live", "equity_kr"),
        ("toss_live", "equity_us"),
        ("upbit", "crypto"),
    }
)
# A buy reconciles into the ledger late. A plan this recent means the earlier
# buy may be filled but unseen, so the next buy is still an add.
_UNRECONCILED_ENTRY_WINDOW = timedelta(days=1)


def _positive(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


async def record_buy_exit_plan(
    *,
    account_mode: str,
    market: str,
    symbol: str,
    price: Any,
    quantity: Any,
    now: datetime,
    stated: Any = None,
    proposal_id: Any = None,
    rung_index: int | None = None,
    session_factory: Any = AsyncSessionLocal,
) -> str:
    """Append the next plan version. Returns a closed status. Never raises."""
    if settings.LOT_EXIT_PLAN_WRITE_ENABLED is not True:
        return "disabled"
    try:
        if (account_mode, market) not in _PLANNABLE:
            return "not_plannable"
        if is_parking_instrument(symbol, market):
            return "parking_instrument"
        buy_price = _positive(price)
        buy_quantity = _positive(quantity)
        if buy_price is None or buy_quantity is None:
            return "price_or_quantity_unavailable"
        stated_plan = None
        if stated is not None:
            try:
                stated_plan = parse_exit_plan_input(stated)
            except ExitPlanInputError as exc:
                # Validated at proposal creation, so this is an old or altered
                # row. The order already went out; fall back to the defaults.
                logger.warning(
                    "lot_exit_plan.stated_plan_ignored", extra={"code": str(exc)}
                )
        key = LotKey(
            account_mode,
            market,
            lot_symbol(
                symbol if market == "crypto" else to_db_symbol(symbol),
                market,
            ),
        )
        params = load_trading_policy().mechanical_loss_cut.markets[
            POLICY_MARKET[market]
        ]
        async with session_factory() as session:
            store = MechanicalLossCutStore(session)
            await store.lock_lot(key)
            previous = await store.latest_plan(key)
            held = await load_position(session, key)
            recent = (
                previous is not None
                and previous.created_at is not None
                and now - previous.created_at < _UNRECONCILED_ENTRY_WINDOW
            )
            if previous is not None and (held is not None or recent):
                draft = add_plan(
                    previous=previous,
                    params=params,
                    held_quantity=held.quantity if held is not None else None,
                    held_average_cost=held.average_cost if held is not None else None,
                    price=buy_price,
                    quantity=buy_quantity,
                    stated=stated_plan,
                    proposal_id=proposal_id,
                    rung_index=rung_index,
                )
            else:
                bars: list[Bar] = []
                if stated_plan is None and params.default_stop.basis != "pct_floor":
                    loaded = await load_bars(
                        session,
                        market=market,
                        symbols=[key.symbol],
                        count=params.default_stop.support_lookback_sessions + 1,
                        now=now,
                    )
                    bars = next(iter(loaded.values()), ([], False))[0]
                draft = entry_plan(
                    key=key,
                    params=params,
                    price=buy_price,
                    quantity=buy_quantity,
                    bars=bars,
                    stated=stated_plan,
                    proposal_id=proposal_id,
                    rung_index=rung_index,
                )
            await store.append_plan(draft)
            await session.commit()
        return draft.event
    except Exception as exc:  # noqa: BLE001 - the order is already accepted
        logger.warning(
            "lot_exit_plan.write_failed", extra={"exception_type": type(exc).__name__}
        )
        return "write_failed"
