"""Alpaca PAPER-only strategy boundary, with explicit account and session checks."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.schemas import Asset, Order, OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.mock_strategy.us_signals import session_times


def require_enabled() -> None:
    from app.core.config import settings

    if (
        settings.mock_strategy_us_enabled is not True
        or settings.alpaca_paper_automated_submit_enabled is not True
    ):
        raise ValueError(
            "MOCK_STRATEGY_US_ENABLED and ALPACA_PAPER_AUTOMATED_SUBMIT_ENABLED required"
        )


class UsPaperBroker:
    def __init__(self, *, settings: AlpacaPaperSettings | None = None, transport=None):
        require_enabled()
        settings = settings or AlpacaPaperSettings.from_app_settings(profile="default")
        if settings.base_url != PAPER_TRADING_BASE_URL:
            raise ValueError("exact Alpaca paper endpoint required")
        if (
            not (settings.expected_account_id_suffix or "").strip()
            or not (settings.expected_account_number_suffix or "").strip()
        ):
            raise ValueError("Alpaca paper expected account identity suffixes required")
        self._service = AlpacaPaperBrokerService(settings=settings, transport=transport)
        self.account_ref: str | None = None

    async def preflight(self, asof: date) -> str:
        require_enabled()
        await self.check_session(asof)
        account = await self._service.get_account()
        if (
            not {"trading_blocked", "account_blocked", "shorting_enabled"}
            <= account.model_fields_set
        ):
            raise ValueError("paper account trading permissions unavailable")
        if (
            account.status != "ACTIVE"
            or account.trading_blocked
            or account.account_blocked
        ):
            raise ValueError("paper account is not active for trading")
        self._shorting_enabled = account.shorting_enabled
        self.account_ref = (
            "alpaca_paper:" + hashlib.sha256(account.id.encode()).hexdigest()[:24]
        )
        return self.account_ref

    async def check_session(self, asof: date) -> None:
        require_enabled()
        clock = await self._service.get_clock()
        start, end, _ = session_times(asof)
        now = datetime.now(UTC)
        if (
            clock.timestamp.utcoffset() is None
            or abs((now - clock.timestamp).total_seconds()) > 60
        ):
            raise ValueError("paper market clock stale or invalid")
        if (
            not clock.is_open
            or not start <= clock.timestamp < end
            or not start <= now < end
        ):
            raise ValueError("current US regular session must match --asof")

    async def assets(self) -> dict[str, Asset]:
        return {
            a.symbol: a
            for a in await self._service.list_assets(
                status="active", asset_class="us_equity"
            )
        }

    async def lookup(self, client_order_id: str) -> Order | None:
        return await self._service.get_order_by_client_order_id(client_order_id)

    async def eligible(self, symbol: str, side: str, qty: Decimal | None) -> None:
        require_enabled()
        asset = await self._service.get_asset(symbol)
        if (
            asset.symbol != symbol
            or asset.asset_class != "us_equity"
            or asset.status != "active"
            or not asset.tradable
        ):
            raise ValueError("asset not tradable US equity")
        if side == "short" and not (
            self._shorting_enabled and asset.shortable and asset.easy_to_borrow
        ):
            raise ValueError("short requires shorting enabled and easy-to-borrow asset")
        position = await self._service.get_position(symbol)
        held = position.qty if position is not None else Decimal(0)
        if (side == "buy" and held < 0) or (side == "short" and held > 0):
            raise ValueError("opposite account position would net another episode")
        if side in {"sell", "cover"}:
            if (
                qty is None
                or qty <= 0
                or (side == "sell" and held < qty)
                or (side == "cover" and -held < qty)
            ):
                raise ValueError("close exceeds fresh same-direction account position")
            if side == "sell" and (
                position is None
                or position.qty_available is None
                or position.qty_available < qty
            ):
                raise ValueError("close exceeds available paper quantity")

    async def submit(
        self, request: OrderRequest, *, side: str, asof: date, confirm: bool = False
    ) -> Order:
        require_enabled()
        if confirm is not True:
            raise ValueError("confirm=True required")
        if self.account_ref is None:
            raise ValueError("paper preflight required")
        if (
            side not in {"buy", "sell", "short", "cover"}
            or request.side != ("sell" if side in {"sell", "short"} else "buy")
            or request.type != "market"
            or request.time_in_force != "day"
            or request.limit_price is not None
            or request.stop_price is not None
            or re.fullmatch(r"[A-Z][A-Z0-9.]{0,31}", request.symbol) is None
            or (request.qty is None) == (request.notional is None)
        ):
            raise ValueError("invalid US paper strategy order shape")
        for value in (request.qty, request.notional):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("invalid paper size")
        if side == "short" and (
            request.qty is None or request.qty != request.qty.to_integral_value()
        ):
            raise ValueError("short requires whole shares")
        await self.eligible(request.symbol, side, request.qty)
        await self.check_session(asof)
        return await self._service.submit_order(request)
