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

import datetime
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.core.symbol import to_db_symbol
from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
from app.mcp_server.tooling.order_execution import _create_kis_client, _place_order_impl
from app.services.brokers.kis import KISClient
from app.services.brokers.kis.mock_scalping_exec.executor import Fill, Quote
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    EvidenceCategory,
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)
from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import (
    BaselineSnapshot,
    confirm_fill_from_holdings_delta,
)
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


def _is_mock_unsupported(message: str) -> bool:
    low = message.lower()
    return "mock" in low and (
        "unsupported" in low or "not available" in low or "아닙니다" in message
    )


class KisMockBroker:
    """BrokerPort over the KIS mock order path. Mock-only; confirm-gated HTTP."""

    def __init__(self, *, get_state: StateProvider, strategy_id: str = "kis-mock-v1"):
        self._get_state = get_state
        self._strategy_id = strategy_id
        self._mock_client: KISClient | None = None

    async def submit_buy(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]:
        baseline = await self._capture_baseline(
            symbol=symbol, side="buy", qty=quantity, limit_price=price
        )
        result = await _place_order_impl(
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
        if isinstance(result, dict):
            result["_baseline"] = baseline
        return result

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
        baseline = await self._capture_baseline(
            symbol=symbol, side="sell", qty=quantity, limit_price=price
        )
        result = await _place_order_impl(
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
        if isinstance(result, dict):
            result["_baseline"] = baseline
        return result

    def _get_mock_client(self) -> KISClient:
        # Mock-host client (live singleton would 401/EGW02005); cached so the
        # executor's bounded _await_fill loop does not re-construct per poll.
        if self._mock_client is None:
            self._mock_client = _create_kis_client(is_mock=True)
        return self._mock_client

    async def _read_snapshot(self, symbol: str) -> tuple[Decimal, Decimal | None]:
        """Read-only mock domestic balance snapshot -> (holdings_qty, cash).

        Holdings qty is the per-symbol position from output1; cash is
        ``dnca_tot_amt`` from output2. Mock host only (VTTC8434R).
        """
        client = self._get_mock_client()
        snap = await client.account.fetch_domestic_balance_snapshot(is_mock=True)
        target = to_db_symbol(symbol)
        qty = Decimal("0")
        for holding in snap.get("holdings") or []:
            if to_db_symbol(str(holding.get("pdno") or "")) == target:
                qty = _to_decimal(holding.get("hldg_qty")) or Decimal("0")
                break
        cash = _to_decimal((snap.get("cash") or {}).get("dnca_tot_amt"))
        return qty, cash

    async def _capture_baseline(
        self, *, symbol: str, side: str, qty: Decimal, limit_price: Decimal
    ) -> dict[str, str | None]:
        """Snapshot holdings + cash immediately before a submit.

        A read failure leaves ``holdings_qty=None`` so confirm_fill fails closed
        (we cannot prove a delta against an unknown baseline).
        """
        try:
            holdings_qty, cash = await self._read_snapshot(symbol)
            hq: str | None = str(holdings_qty)
        except Exception as exc:  # noqa: BLE001 - baseline read failure fails closed
            logger.info("kis-mock baseline snapshot failed sym=%s: %s", symbol, exc)
            hq, cash = None, None
        return {
            "symbol": symbol,
            "side": side,
            "ordered_qty": str(qty),
            "limit_price": str(limit_price),
            "holdings_qty": hq,
            "cash": (str(cash) if cash is not None else None),
        }

    async def confirm_fill(self, submit_result: dict[str, Any]) -> Fill | None:
        # ROB-341: same-day fill confirmation from the baseline-vs-post holdings
        # delta (primary) + cash delta (price). daily-ccld is NOT consulted here
        # (it can return empty rows for same-day mock fills); it remains
        # supplementary/post-settlement evidence only. Every ambiguous outcome
        # is fail-closed (None -> executor records entry_unfilled/exit_unconfirmed
        # anomaly). Never fabricates a fill.
        raw = submit_result.get("_baseline")
        if not isinstance(raw, dict):
            logger.info("kis-mock confirm: no baseline snapshot -> fail closed")
            return None
        baseline = BaselineSnapshot(
            symbol=str(raw["symbol"]),
            side=raw["side"],
            ordered_qty=Decimal(str(raw["ordered_qty"])),
            limit_price=Decimal(str(raw["limit_price"])),
            holdings_qty=(
                Decimal(str(raw["holdings_qty"]))
                if raw.get("holdings_qty") is not None
                else None
            ),
            cash=(Decimal(str(raw["cash"])) if raw.get("cash") is not None else None),
        )
        return await confirm_fill_from_holdings_delta(
            baseline, fetch_post=self._read_snapshot
        )

    async def poll_daily_ccld_diagnostic(
        self, submit_result: dict[str, Any]
    ) -> FillEvidence:
        """Supplementary, NON-GATING daily-ccld read (ROB-341).

        Retained for post-settlement evidence / the smoke packet only. Its empty
        same-day result is classified clearly but never gates or overrides the
        holdings-delta verdict in :meth:`confirm_fill`.
        """
        order_no = submit_result.get("odno") or submit_result.get("order_no")
        if not order_no:
            return FillEvidence(
                FillVerdict.NONE,
                None,
                None,
                EvidenceCategory.DATA_PRECONDITION,
                "order_no_missing",
                "submit response carried no odno",
            )
        today = datetime.datetime.now().strftime("%Y%m%d")
        try:
            client = self._get_mock_client()
            rows = await client.domestic_orders.inquire_daily_order_domestic(
                start_date=today,
                end_date=today,
                stock_code="",
                order_number=str(order_no),
                is_mock=True,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if _is_mock_unsupported(msg):
                return FillEvidence(
                    FillVerdict.UNSUPPORTED,
                    None,
                    None,
                    EvidenceCategory.UNSUPPORTED_MOCK_API,
                    "inquiry_unsupported",
                    msg[:200],
                )
            return FillEvidence(
                FillVerdict.NONE,
                None,
                None,
                EvidenceCategory.CODE,
                "inquiry_error",
                msg[:200],
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on any inquiry fault
            return FillEvidence(
                FillVerdict.NONE,
                None,
                None,
                EvidenceCategory.CODE,
                "inquiry_exception",
                str(exc)[:200],
            )
        return classify_fill_evidence(order_no=str(order_no), rows=rows)

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
