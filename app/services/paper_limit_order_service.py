"""ROB-703 — Paper resting-limit order service (Upbit shadow sim).

Additive on top of the existing ``PaperTradingService``:

* ``place_limit_order`` writes ``PaperPendingOrder`` and reserves
  ``gross + buy_fee`` KRW from ``cash_krw`` so concurrent cash reads already
  reflect the held balance.
* ``reconcile_pending_orders`` walks pending rows, fetches live Upbit OHLCV
  via ``app.services.market_data.service.get_ohlcv`` (caller-mockable in
  tests), and uses the pure ``snap_limit_down`` / ``limit_crossed`` helpers
  in ``app.services.paper_fills`` to decide fills. On fill it releases the
  reservation and books the trade through the existing
  ``PaperTradingService.execute_order`` path at exactly the limit price (no
  live re-fetch — ``order_type='limit'`` uses ``price`` as supplied).
* ``cancel_pending_order`` and ``list_pending_orders`` round out the surface.

Math invariant: on fill the reservation is released BEFORE
``execute_order`` re-charges ``total_cost`` so the cash accounting settles
exactly once. ``paper_trade_id`` is recovered by querying the most-recent
``PaperTrade`` for the same (account, symbol, side) after the
``execute_order`` commits.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import quantize_crypto_qty, quantize_money
from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
)
from app.mcp_server.tooling.paper_order_handler import _activate_paper_journal
from app.mcp_server.tooling.shared import (
    DEFAULT_MINIMUM_VALUES,
    resolve_market_type,
)
from app.models.paper_trading import (
    PaperPendingOrder,
    PaperTrade,
)
from app.services.market_data.service import get_ohlcv
from app.services.paper_correlation import paper_correlation_id
from app.services.paper_fills import limit_crossed, snap_limit_down
from app.services.paper_trading_service import (
    PaperTradingService,
    calculate_fee,
)
from app.services.trade_journal.forecast_service import save_forecast

logger = logging.getLogger(__name__)

# Upbit reserves 5000 KRW as the minimum order notional for crypto.
_MIN_CRYPTO_KRW = Decimal(str(DEFAULT_MINIMUM_VALUES["crypto"]))

_OHLCV_LOOKBACK_BARS = 200


def _quantize_fill_price(price: Decimal) -> Decimal:
    """Snap limit price down to Upbit KRW tick band + Quantize money."""
    return quantize_money(snap_limit_down(price))


def _as_aware_kst(ts: dt.datetime | None) -> dt.datetime | None:
    """Upbit candle timestamps (candle_date_time_kst) are tz-naive KST wall-clock;
    placed_at is tz-aware. Coerce naive -> KST-aware so comparisons never raise."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=KST)
    return ts


async def _latest_trade_id(
    db: AsyncSession,
    *,
    account_id: int,
    symbol: str,
    side: str,
) -> int | None:
    stmt = (
        select(PaperTrade.id)
        .where(
            PaperTrade.account_id == account_id,
            PaperTrade.symbol == symbol,
            PaperTrade.side == side,
        )
        .order_by(desc(PaperTrade.executed_at), desc(PaperTrade.id))
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return int(row[0]) if row is not None else None


def _serialize(order: PaperPendingOrder) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "account_id": order.account_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "limit_price": Decimal(order.limit_price),
        "quantity": Decimal(order.quantity),
        "reserved_krw": Decimal(order.reserved_krw),
        "status": order.status,
        "thesis": order.thesis,
        "fill_price": Decimal(order.fill_price)
        if order.fill_price is not None
        else None,
        "paper_trade_id": order.paper_trade_id,
        "placed_at": order.placed_at,
        "filled_at": order.filled_at,
        "cancelled_at": order.cancelled_at,
    }


class PaperLimitOrderService:
    """Manage resting paper limit orders + reconcile against live OHLCV."""

    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.pts = PaperTradingService(session)

    async def _pending_sell_qty(self, account_id: int, symbol: str) -> Decimal:
        stmt = select(func.coalesce(func.sum(PaperPendingOrder.quantity), 0)).where(
            PaperPendingOrder.account_id == account_id,
            PaperPendingOrder.symbol == symbol,
            PaperPendingOrder.side == "sell",
            PaperPendingOrder.status == "pending",
        )
        return Decimal(str((await self.db.execute(stmt)).scalar() or 0))

    async def place_limit_order(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        limit_price: Decimal | float | int,
        quantity: Decimal | float | int | None = None,
        amount: Decimal | float | int | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: Decimal | float | int | None = None,
        stop_loss: Decimal | float | int | None = None,
        probability: float | None = None,
        review_date: str | None = None,
        artifact_uuid: str | None = None,
    ) -> dict[str, Any]:
        side_norm = side.lower()
        if side_norm not in ("buy", "sell"):
            return {
                "success": False,
                "error": f"side must be 'buy' or 'sell', got {side!r}",
            }

        # 1. Account gate
        account = await self.pts.get_account(account_id)
        if account is None:
            return {"success": False, "error": f"Account {account_id} not found"}
        if not account.is_active:
            return {"success": False, "error": f"Account {account_id} is inactive"}

        # 2. Symbol normalization + market detection
        try:
            market_type, resolved_symbol = resolve_market_type(symbol, None)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        if market_type != "crypto":
            return {
                "success": False,
                "error": (
                    "Resting-limit sim only supports crypto markets (Upbit "
                    "shadow); got market_type="
                    f"{market_type!r}"
                ),
            }

        # 3. Snap price to tick band
        snapped_price = _quantize_fill_price(Decimal(str(limit_price)))
        if snapped_price <= Decimal("0"):
            return {"success": False, "error": "limit_price must be positive"}

        # 4. Resolve quantity
        if quantity is not None:
            qty = quantize_crypto_qty(Decimal(str(quantity)))
        elif amount is not None:
            amt = Decimal(str(amount))
            if amt <= 0:
                return {"success": False, "error": "amount must be positive"}
            qty = quantize_crypto_qty(amt / snapped_price)
        else:
            return {
                "success": False,
                "error": "Either quantity or amount must be provided",
            }
        if qty <= 0:
            return {
                "success": False,
                "error": f"Computed quantity is not positive: {qty}",
            }

        # 5. Cash reservation (buy only)
        if side_norm == "buy":
            gross = quantize_money(qty * snapped_price)
            fee = calculate_fee("crypto", "buy", gross)
            reserved_krw = quantize_money(gross + fee)
            if gross < _MIN_CRYPTO_KRW:
                return {
                    "success": False,
                    "error": (
                        f"Order notional {gross} KRW is below the "
                        f"Upbit minimum {_MIN_CRYPTO_KRW} KRW"
                    ),
                }
            if Decimal(account.cash_krw) < reserved_krw:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient KRW balance: have {account.cash_krw}, "
                        f"need {reserved_krw}"
                    ),
                }
            account.cash_krw = quantize_money(Decimal(account.cash_krw) - reserved_krw)
            reserved_krw_value: Decimal | None = reserved_krw
        else:
            reserved_krw_value = Decimal("0")
            gross = quantize_money(qty * snapped_price)
            if gross < _MIN_CRYPTO_KRW:
                return {
                    "success": False,
                    "error": (
                        f"Order notional {gross} KRW is below the "
                        f"Upbit minimum {_MIN_CRYPTO_KRW} KRW"
                    ),
                }
            position = await self.pts._get_position(account_id, resolved_symbol)
            if position is None:
                return {
                    "success": False,
                    "error": f"No position to sell for {resolved_symbol}",
                }
            pending_sell = await self._pending_sell_qty(account_id, resolved_symbol)
            available = Decimal(position.quantity) - pending_sell
            if available < qty:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient sellable quantity: position "
                        f"{position.quantity}, already-pending sells {pending_sell}, "
                        f"need {qty}"
                    ),
                }

        # 6. Mint deterministic provenance spine (ROB-653 P6-B trade-day+rung
        # salt) — order, fill, journal, and forecast share one stable id.
        kst_trade_day = now_kst().strftime("%Y-%m-%d")
        corr_id = paper_correlation_id(
            account_id=account_id,
            symbol=resolved_symbol,
            side=side_norm,
            limit_price=snapped_price,
            quantity=qty,
            kst_trade_day=kst_trade_day,
            rung=0,
        )

        # 7. Persist the resting order
        order = PaperPendingOrder(
            account_id=account_id,
            symbol=resolved_symbol,
            side=side_norm,
            order_type="limit",
            limit_price=snapped_price,
            quantity=qty,
            reserved_krw=reserved_krw_value,
            status="pending",
            thesis=thesis,
            correlation_id=corr_id,
            artifact_uuid=artifact_uuid,
            placed_at=now_kst(),
        )
        self.db.add(order)
        await self.db.flush()
        await self.db.refresh(order)

        # 8. Place-time provenance — best-effort: failures are logged and
        # continued so a journal/forecast hiccup never aborts the order.
        if side_norm == "buy" and thesis:
            try:
                journal_res = await _create_trade_journal_for_buy(
                    symbol=resolved_symbol,
                    market_type="crypto",
                    preview={
                        "price": snapped_price,
                        "quantity": qty,
                        "estimated_value": quantize_money(qty * snapped_price),
                    },
                    thesis=thesis,
                    strategy=(strategy or ""),
                    target_price=(
                        float(Decimal(str(target_price)))
                        if target_price is not None
                        else None
                    ),
                    stop_loss=(
                        float(Decimal(str(stop_loss)))
                        if stop_loss is not None
                        else None
                    ),
                    min_hold_days=None,
                    notes=None,
                    indicators_snapshot=None,
                    account_type="paper",
                    account=account.name,
                    correlation_id=corr_id,
                )
                order.journal_id = journal_res.get("journal_id")
            except Exception:
                logger.exception(
                    "paper place_limit_order: failed to create draft journal "
                    "for correlation_id=%s",
                    corr_id,
                )

            if probability is not None and target_price is not None and review_date:
                try:
                    direction = "at_or_below" if side_norm == "buy" else "at_or_above"
                    _action, fc = await save_forecast(
                        self.db,
                        created_by="paper_sim",
                        symbol=resolved_symbol,
                        instrument_type="crypto",
                        forecast_target={
                            "kind": "price_target",
                            "direction": direction,
                            "target_price": float(Decimal(str(target_price))),
                        },
                        probability=float(probability),
                        review_date=review_date,
                        correlation_id=corr_id,
                        horizon=None,
                        model_label=None,
                        session_label="paper_place",
                        artifact_uuid=artifact_uuid,
                    )
                    order.forecast_id = str(getattr(fc, "forecast_id", ""))
                except Exception:
                    logger.exception(
                        "paper place_limit_order: failed to save forecast "
                        "for correlation_id=%s",
                        corr_id,
                    )

        # Snapshot updated cash for the response
        await self.db.refresh(account)
        await self.db.commit()

        return {
            "success": True,
            "status": "pending",
            "order_id": order.id,
            "account_id": account_id,
            "symbol": resolved_symbol,
            "side": side_norm,
            "limit_price": snapped_price,
            "quantity": qty,
            "reserved_krw": reserved_krw_value,
            "cash_krw": Decimal(account.cash_krw),
            "correlation_id": corr_id,
            "journal_id": order.journal_id,
            "forecast_id": order.forecast_id,
            "placed_at": order.placed_at,
        }

    async def list_pending_orders(
        self, *, account_id: int, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        stmt = select(PaperPendingOrder).where(
            PaperPendingOrder.account_id == account_id
        )
        if status is not None:
            stmt = stmt.where(PaperPendingOrder.status == status)
        stmt = stmt.order_by(PaperPendingOrder.placed_at.asc())
        rows = (await self.db.execute(stmt)).scalars().all()
        return [_serialize(o) for o in rows]

    async def get_pending_order(
        self, *, account_id: int, order_id: int
    ) -> dict[str, Any] | None:
        stmt = select(PaperPendingOrder).where(
            PaperPendingOrder.account_id == account_id,
            PaperPendingOrder.id == order_id,
        )
        order = (await self.db.execute(stmt)).scalars().first()
        return _serialize(order) if order is not None else None

    async def cancel_pending_order(
        self, *, account_id: int, order_id: int
    ) -> dict[str, Any]:
        stmt = select(PaperPendingOrder).where(
            PaperPendingOrder.account_id == account_id,
            PaperPendingOrder.id == order_id,
        )
        order = (await self.db.execute(stmt)).scalars().first()
        if order is None:
            return {"success": False, "error": f"Order {order_id} not found"}
        if order.status != "pending":
            return {
                "success": False,
                "error": f"Order {order_id} is {order.status}, cannot cancel",
            }

        # Release reservation (buy side only carries a reservation)
        if order.side == "buy":
            account = await self.pts.get_account(account_id)
            assert account is not None
            account.cash_krw = quantize_money(
                Decimal(account.cash_krw) + Decimal(order.reserved_krw)
            )
        order.status = "cancelled"
        order.cancelled_at = now_kst()
        await self.db.commit()
        await self.db.refresh(order)
        return {"success": True, **_serialize(order)}

    async def reconcile_pending_orders(
        self,
        *,
        account_id: int,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Walk pending ``PaperPendingOrder`` rows and fill any whose limit
        was crossed by the most-recent live Upbit OHLCV.

        Returns ``{"success": True, "reconciled": N, "filled": M}``.
        """
        effective_now = now or now_kst()
        stmt = (
            select(PaperPendingOrder.id)
            .where(
                PaperPendingOrder.account_id == account_id,
                PaperPendingOrder.status == "pending",
            )
            .order_by(
                PaperPendingOrder.placed_at.asc(),
                PaperPendingOrder.id.asc(),
            )
        )
        order_ids = [row[0] for row in (await self.db.execute(stmt)).all()]
        if not order_ids:
            return {"success": True, "reconciled": 0, "filled": 0}

        filled = 0
        for oid in order_ids:
            # Re-fetch each order fresh inside the loop: a mid-batch rollback
            # (e.g. from an oversell) expires the identity map, so reading
            # attributes off a pre-loaded ORM list would raise on the next
            # iteration. Loading by id avoids that poisoning entirely.
            order = await self.db.get(PaperPendingOrder, oid)
            if order is None or order.status != "pending":
                continue
            try:
                candles = await get_ohlcv(
                    order.symbol,
                    "crypto",
                    "1m",
                    _OHLCV_LOOKBACK_BARS,
                    end=None,
                )
            except Exception:
                # data unavailable -> leave pending
                continue
            placed_at = _as_aware_kst(order.placed_at)
            bars: list[tuple[Decimal, Decimal]] = []
            for c in candles:
                c_ts = _as_aware_kst(getattr(c, "timestamp", None))
                if c_ts is not None and placed_at is not None and c_ts < placed_at:
                    continue
                bars.append((Decimal(str(c.low)), Decimal(str(c.high))))
            if not bars:
                continue
            fill_price = limit_crossed(order.side, Decimal(order.limit_price), bars)
            if fill_price is None:
                continue

            # Atomic fill: flip status BEFORE execute_order so its internal
            # commit persists status='filled' + the trade + the cash change in
            # ONE transaction. A raise before that commit (e.g. oversell) rolls
            # back only this order; the id-based loop re-fetches the next order
            # fresh, so a rollback never poisons remaining iterations.
            try:
                account = await self.pts.get_account(account_id)
                assert account is not None
                if order.side == "buy":
                    account.cash_krw = quantize_money(
                        Decimal(account.cash_krw) + Decimal(order.reserved_krw)
                    )
                order.status = "filled"
                order.fill_price = fill_price
                order.filled_at = effective_now
                await self.pts.execute_order(
                    account_id=account_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type="limit",
                    price=fill_price,
                    quantity=Decimal(order.quantity),
                    reason=order.thesis or "paper resting-limit fill",
                )
            except Exception:
                await self.db.rollback()
                continue

            # The fill is now durably committed by execute_order's internal
            # commit. Count it, then link the trade id best-effort -- a failure
            # here loses only the (nullable) FK link, never the fill or count.
            filled += 1
            try:
                trade_id = await _latest_trade_id(
                    self.db,
                    account_id=account_id,
                    symbol=order.symbol,
                    side=order.side,
                )
                order.paper_trade_id = trade_id
                if trade_id is not None:
                    trade = await self.db.get(PaperTrade, trade_id)
                    if trade is not None:
                        trade.correlation_id = order.correlation_id
                        trade.journal_id = order.journal_id
                        trade.artifact_uuid = order.artifact_uuid
                        trade.forecast_id = order.forecast_id
                await self.db.commit()
            except Exception:
                await self.db.rollback()

            # Journal bridge — best-effort: a buy fill activates the draft
            # journal; a sell fill closes it with pnl_pct. Both helpers own
            # their own sessions/commits; a failure here must never undo the
            # durably-committed fill above.
            try:
                if order.side == "buy":
                    await _activate_paper_journal(
                        symbol=order.symbol, account_name=account.name
                    )
                else:
                    await _close_journals_on_sell(
                        symbol=order.symbol,
                        sell_quantity=float(order.quantity),
                        sell_price=float(fill_price),
                        exit_reason=order.thesis or "paper resting-limit fill",
                        account_type="paper",
                        account=account.name,
                    )
            except Exception:
                logger.exception(
                    "paper reconcile: journal bridge failed for correlation_id=%s",
                    order.correlation_id,
                )

        return {
            "success": True,
            "reconciled": len(order_ids),
            "filled": filled,
        }


__all__ = [
    "PaperLimitOrderService",
]
