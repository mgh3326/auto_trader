"""ROB-321 PR4b — concrete broker/ledger adapters for the executor ports.

``KisMockBroker`` submits through ``_place_order_impl(is_mock=True, ...)`` —
buys, and ``scalping_exit`` sells (the PR4a wiring bypasses the avg*1.01 floor +
current-price guard for the stop-loss). The dry-run daemon only ever calls
``submit_buy(confirm=False)`` (the executor returns right after the preview), so
the dry-run path is fully exercised; ``quote`` reads the live per-symbol
``MarketState``.

``confirm_fill`` is the documented OPEN ITEM: KIS mock does not return an
immediate fill price on submit (fills are reconciled later via holdings), so
real fill evidence requires operator validation against live KIS mock. Until
then it returns ``None`` (fail-safe: confirm mode degrades to an
``entry_unfilled`` anomaly rather than fabricating a fill). See the runbook.

``KisMockLedgerWriter`` records entry/exit/anomaly rows via the extended
``_save_kis_mock_order_ledger`` (correlation_id / scalping_role / exit_reason /
gross_pnl / net_pnl). Used only on the confirm path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
from app.mcp_server.tooling.order_execution import _place_order_impl
from app.services.brokers.kis.mock_scalping_exec.executor import Fill, Quote
from app.services.brokers.kis.mock_scalping_ws.state import MarketState

logger = logging.getLogger("rob321.kis_mock_scalping_exec")

StateProvider = Callable[[str], MarketState | None]


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


class KisMockBroker:
    """BrokerPort over the KIS mock order path. Mock-only; confirm-gated HTTP."""

    def __init__(self, *, get_state: StateProvider, strategy_id: str = "kis-mock-v1"):
        self._get_state = get_state
        self._strategy_id = strategy_id

    async def submit_buy(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]:
        return await _place_order_impl(
            symbol=symbol,
            side="buy",
            market="kr",
            order_type="limit",
            quantity=float(quantity),
            price=float(price),
            dry_run=not confirm,
            is_mock=True,
            reason=f"scalp_entry:{correlation_id}",
            strategy=self._strategy_id,
        )

    async def submit_exit_sell(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        exit_reason: str,
        strategy_id: str,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]:
        return await _place_order_impl(
            symbol=symbol,
            side="sell",
            market="kr",
            order_type="limit",
            quantity=float(quantity),
            price=float(price),
            dry_run=not confirm,
            is_mock=True,
            reason=f"scalp_exit:{correlation_id}",
            exit_reason=exit_reason,
            strategy=strategy_id,
            scalping_exit=True,
            scalping_strategy_id=strategy_id,
            scalping_exit_reason=exit_reason,
        )

    async def confirm_fill(self, submit_result: dict[str, Any]) -> Fill | None:
        # OPEN ITEM (ROB-321 §2-4): KIS mock gives no immediate fill price on
        # submit. Real fill evidence (execution WS / holdings poll) is validated
        # by operator smoke; until then we never fabricate a fill.
        logger.warning(
            "confirm_fill unavailable for KIS mock submit (fill evidence pending "
            "operator validation); treating as unfilled"
        )
        return None

    def quote(self, symbol: str) -> Quote | None:
        state = self._get_state(symbol)
        if state is None:
            return None
        return Quote(
            bid=_to_decimal(state.bid),
            ask=_to_decimal(state.ask),
            last=_to_decimal(state.last_price),
        )


class KisMockLedgerWriter:
    """LedgerPort writing round-trip rows to review.kis_mock_order_ledger."""

    def __init__(self, *, strategy_id: str = "kis-mock-v1"):
        self._strategy_id = strategy_id

    async def record_entry(
        self, *, correlation_id: str, symbol: str, strategy_id: str, fill: Fill
    ) -> None:
        await _save_kis_mock_order_ledger(
            symbol=symbol,
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            quantity=float(fill.quantity),
            price=float(fill.price),
            amount=float(fill.price * fill.quantity),
            currency="KRW",
            order_no=f"{correlation_id}-entry",
            order_time=None,
            krx_fwdg_ord_orgno=None,
            status="accepted",
            response_code=None,
            response_message=None,
            raw_response=None,
            reason="scalp_entry",
            thesis=None,
            strategy=strategy_id,
            notes=None,
            lifecycle_state="fill",
            correlation_id=correlation_id,
            scalping_role="entry",
        )

    async def record_exit_reconciled(
        self,
        *,
        correlation_id: str,
        symbol: str,
        exit_reason: str,
        entry_fill: Fill,
        exit_fill: Fill,
        gross_pnl: Decimal,
        net_pnl: Decimal,
        fees: Decimal,
    ) -> None:
        await _save_kis_mock_order_ledger(
            symbol=symbol,
            instrument_type="equity_kr",
            side="sell",
            order_type="limit",
            quantity=float(exit_fill.quantity),
            price=float(exit_fill.price),
            amount=float(exit_fill.price * exit_fill.quantity),
            currency="KRW",
            order_no=f"{correlation_id}-exit",
            order_time=None,
            krx_fwdg_ord_orgno=None,
            status="accepted",
            response_code=None,
            response_message=None,
            raw_response=None,
            reason="scalp_exit",
            thesis=None,
            strategy=self._strategy_id,
            notes=None,
            lifecycle_state="reconciled",
            fee=float(fees),
            correlation_id=correlation_id,
            scalping_role="exit",
            exit_reason=exit_reason,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
        )

    async def record_anomaly(
        self, *, correlation_id: str, symbol: str, detail: str
    ) -> None:
        await _save_kis_mock_order_ledger(
            symbol=symbol,
            instrument_type="equity_kr",
            side="sell",
            order_type="limit",
            quantity=0.0,
            price=0.0,
            amount=0.0,
            currency="KRW",
            order_no=f"{correlation_id}-anomaly",
            order_time=None,
            krx_fwdg_ord_orgno=None,
            status="unknown",
            response_code=None,
            response_message=None,
            raw_response=None,
            reason="scalp_anomaly",
            thesis=None,
            strategy=self._strategy_id,
            notes=detail,
            lifecycle_state="anomaly",
            correlation_id=correlation_id,
            scalping_role="exit",
        )
