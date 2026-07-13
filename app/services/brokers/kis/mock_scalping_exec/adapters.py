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
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any

from app.core.symbol import to_db_symbol
from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
from app.mcp_server.tooling.order_execution import (
    OrderSendOutcomeUnknown,
    _create_kis_client,
    _place_order_impl,
)
from app.services.brokers.kis import KISClient
from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
    Side,
)
from app.services.brokers.kis.mock_scalping_exec.executor import (
    Fill,
    Quote,
    RiskInputs,
)
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
from app.services.brokers.kis.mock_scalping_exec.ledger_state import (
    MockOrderHistory,
    load_kis_mock_order_history,
)
from app.services.brokers.kis.mock_scalping_exec.reservation import (
    release_entry,
    reserve_entry,
)
from app.services.brokers.kis.mock_scalping_ws.state import MarketState
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.brokers.kis.send_outcome import (
    OrderSendDisposition,
    OrderSendOutcomeTracker,
)
from app.services.order_send_intent_service import DuplicateOrderIntent

logger = logging.getLogger("rob321.kis_mock_scalping_exec")

StateProvider = Callable[[str], MarketState | None]
OrderHistoryLoader = Callable[..., Awaitable[MockOrderHistory]]
HoldingsProvider = Callable[[], Awaitable[Mapping[str, Any]]]
ReservedSubmit = Callable[[OrderSendOutcomeTracker | None], Awaitable[dict[str, Any]]]


async def _default_mock_holdings_snapshot() -> Mapping[str, Any]:
    """Fresh KIS mock domestic balance snapshot ({holdings, cash}). Mock host only.

    ``holdings`` is pre-filtered to ``hldg_qty > 0`` by the account reader, so
    every entry is an actual position.
    """
    client = _create_kis_client(is_mock=True)
    return await client.fetch_domestic_balance_snapshot(is_mock=True)


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

    def __init__(
        self,
        *,
        get_state: StateProvider,
        strategy_id: str = "kis-mock-v1",
        limits: ScalpingRiskLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._get_state = get_state
        self._strategy_id = strategy_id
        self._limits = limits or ScalpingRiskLimits()
        self._clock = clock
        self._mock_client: KISClient | None = None

    def _make_pre_send_hook(self, symbol: str):
        """A pre-send freshness re-check bound to ``symbol`` (ROB-843 P1-1).

        Invoked by ``_place_order_impl`` immediately before the real KIS POST —
        after the risk gate's holdings/history awaits AND the broker's own
        baseline/preflight awaits — so a book that went stale/crossed in between
        blocks the send with ZERO POSTs.
        """

        async def _hook() -> None:
            assert_market_fresh_for_send(
                self._get_state(symbol),
                now=self._clock(),
                max_data_age_seconds=self._limits.max_data_age_seconds,
                max_spread_bps=self._limits.max_spread_bps,
            )

        return _hook

    async def _safe_release_reservation(self, correlation_id: str) -> None:
        try:
            await release_entry(correlation_id=correlation_id)
        except Exception as exc:  # noqa: BLE001 — a stale reservation is fail-safe
            logger.warning(
                "scalping reservation release failed cid=%s: %s", correlation_id, exc
            )

    async def _submit_with_reservation(
        self,
        *,
        symbol: str,
        side: str,
        correlation_id: str,
        confirm: bool,
        submit: ReservedSubmit,
    ) -> dict[str, Any]:
        """Apply one explicit reservation lifecycle to BUY and SELL mutations."""
        if not confirm:
            return await submit(None)

        try:
            await reserve_entry(correlation_id=correlation_id, symbol=symbol, side=side)
        except DuplicateOrderIntent:
            return {
                "success": False,
                "reservation_blocked": True,
                "reason_codes": ["duplicate_send"],
                "detail": f"scalping order already reserved: {correlation_id}",
                "dry_run": False,
            }
        except Exception as exc:  # noqa: BLE001 — durable write lost → POST 0
            logger.warning(
                "scalping reservation failed sym=%s side=%s: %s", symbol, side, exc
            )
            return {
                "success": False,
                "reservation_blocked": True,
                "reason_codes": ["reservation_unavailable"],
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "dry_run": False,
            }

        outcome = OrderSendOutcomeTracker()
        try:
            result = await submit(outcome)
        except OrderSendOutcomeUnknown:
            # A timeout/network failure after dispatch may have created an order.
            raise
        except Exception:
            # Only an explicitly proven pre-send/definitive no-order exception
            # releases. Any post-dispatch exception leaves UNKNOWN and keeps it.
            if outcome.disposition is OrderSendDisposition.NOT_CREATED:
                await self._safe_release_reservation(correlation_id)
            raise

        if outcome.disposition is OrderSendDisposition.NOT_CREATED:
            await self._safe_release_reservation(correlation_id)
        elif outcome.disposition is OrderSendDisposition.ACCEPTED:
            tracked = bool(result.get("success")) and not result.get(
                "ledger_tracking_unavailable"
            )
            if tracked:
                await self._safe_release_reservation(correlation_id)
        # UNKNOWN or accepted-but-untracked: KEEP until explicit reconciliation.
        return result

    async def submit_buy(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]:
        # Baseline is only consumed by confirm_fill on the confirm path; a
        # dry-run returns before confirm, so skip the balance read entirely.
        baseline = (
            await self._capture_baseline(
                symbol=symbol, side="buy", qty=quantity, limit_price=price
            )
            if confirm
            else None
        )

        async def _submit(
            send_outcome: OrderSendOutcomeTracker | None,
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
                # Share the executor correlation_id so native + synthetic evidence
                # link for the daily-count de-dup (ROB-843 P1-2).
                correlation_id=correlation_id,
                # Final freshness re-check right before the POST (entry only; an
                # exit must always be allowed to close a live position).
                pre_send_hook=self._make_pre_send_hook(symbol),
                send_outcome=send_outcome,
            )

        result = await self._submit_with_reservation(
            symbol=symbol,
            side="buy",
            correlation_id=correlation_id,
            confirm=confirm,
            submit=_submit,
        )

        if isinstance(result, dict) and baseline is not None:
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
        baseline = (
            await self._capture_baseline(
                symbol=symbol, side="sell", qty=quantity, limit_price=price
            )
            if confirm
            else None
        )

        async def _submit(
            send_outcome: OrderSendOutcomeTracker | None,
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
                # Link native + synthetic exit evidence (ROB-843 P1-2). No
                # freshness hook on exits: a live position remains closable.
                correlation_id=correlation_id,
                send_outcome=send_outcome,
            )

        result = await self._submit_with_reservation(
            symbol=symbol,
            side="sell",
            correlation_id=correlation_id,
            confirm=confirm,
            submit=_submit,
        )
        if isinstance(result, dict) and baseline is not None:
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
        snap = await client.fetch_domestic_balance_snapshot(is_mock=True)
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
            rows = await client.inquire_daily_order_domestic(
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


def _is_finite_positive(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def assert_market_fresh_for_send(
    state: MarketState | None,
    *,
    now: float,
    max_data_age_seconds: float,
    max_spread_bps: Decimal,
) -> None:
    """Re-validate the CURRENT live book immediately before the broker POST.

    Raises :class:`PreSendFreshnessError` on a missing/stale/invalid quote so a
    BUY that passed the earlier risk gate is not sent against a book that went
    stale/crossed during the intervening holdings/history/baseline awaits.
    """
    if state is None:
        raise PreSendFreshnessError(("no_market_state",))
    book_age = state.book_age_seconds(now=now)
    bid, ask = state.bid, state.ask
    reasons: list[str] = []
    if book_age is None or not math.isfinite(book_age) or book_age < 0:
        reasons.append("invalid_book_timestamp")
    elif book_age > max_data_age_seconds:
        reasons.append(ReasonCode.STALE_DATA)
    if not _is_finite_positive(bid) or not _is_finite_positive(ask):
        reasons.append("invalid_quote")
    elif ask < bid:
        reasons.append("crossed_book")
    else:
        spread = state.spread_bps()
        if spread is None or not math.isfinite(spread) or spread < 0:
            reasons.append("invalid_spread")
        elif Decimal(str(spread)) > max_spread_bps:
            reasons.append(ReasonCode.SPREAD_TOO_WIDE)
    if reasons:
        raise PreSendFreshnessError(tuple(reasons))


class KisMockRiskGate:
    """RiskGatePort: fresh position + market + order-history snapshot (ROB-843).

    ``load`` raises on any missing/stale/malformed market field, holdings read
    fault, or order-history read fault so the executor fail-closes to zero
    broker mutation. Sources:

    * **Position** (has-open / open-count): a fresh KIS mock *holdings* snapshot
      — the authoritative record of what is actually held. Order lifecycle rows
      are NOT treated as positions (a filled buy later sold is flat).
    * **Market** (spread / data age): the live per-symbol ``MarketState``.
    * **Order history** (daily count / realized loss / cooldown): the mock
      order ledger.
    """

    def __init__(
        self,
        *,
        get_state: StateProvider,
        holdings_provider: HoldingsProvider = _default_mock_holdings_snapshot,
        clock: Callable[[], float] = time.monotonic,
        order_history_loader: OrderHistoryLoader = load_kis_mock_order_history,
    ) -> None:
        self._get_state = get_state
        self._holdings = holdings_provider
        self._clock = clock
        self._load_history = order_history_loader

    def _market(self, symbol: str) -> MarketConditions:
        """Validate the live book and build ``MarketConditions``, else raise.

        Fail-closes on a missing state, missing/negative/NaN/Inf book age,
        non-positive or non-finite bid/ask, or a crossed book (ask < bid) — the
        last would otherwise pass ``evaluate_risk`` as a negative spread.
        """
        state = self._get_state(symbol)
        if state is None:
            raise RuntimeError(f"no live market state for {symbol}")
        now = self._clock()
        book_age = state.book_age_seconds(now=now)
        bid, ask = state.bid, state.ask
        if book_age is None or not math.isfinite(book_age) or book_age < 0:
            raise RuntimeError(f"missing/invalid book timestamp for {symbol}")
        if not _is_finite_positive(bid) or not _is_finite_positive(ask):
            raise RuntimeError(
                f"non-positive/non-finite quote for {symbol} (bid={bid} ask={ask})"
            )
        if ask < bid:  # crossed book — never trade a negative spread
            raise RuntimeError(f"crossed book for {symbol} (bid={bid} ask={ask})")
        spread = state.spread_bps()
        if spread is None or not math.isfinite(spread) or spread < 0:
            raise RuntimeError(f"invalid spread for {symbol} (spread={spread})")
        return MarketConditions(
            spread_bps=Decimal(str(spread)),
            data_age_seconds=float(book_age),
        )

    async def _position(self, symbol: str) -> tuple[bool, int]:
        """(has_open_for_symbol, open_position_count) from fresh holdings."""
        snap = await self._holdings()
        holdings = snap.get("holdings") or []
        target = to_db_symbol(symbol)
        open_count = 0
        has_open = False
        for holding in holdings:
            qty = _to_decimal(holding.get("hldg_qty")) or Decimal("0")
            if qty <= 0:
                continue
            open_count += 1
            if to_db_symbol(str(holding.get("pdno") or "")) == target:
                has_open = True
        return has_open, open_count

    async def load(self, *, symbol: str, side: Side) -> RiskInputs:
        # ROB-843 P1: the durable fail-close (unresolved write-ahead reservation)
        # is enforced inside the order-history load below, so it survives restart
        # and a fresh gate instance.
        market = self._market(symbol)
        has_open, open_count = await self._position(symbol)
        history = await self._load_history(symbol=to_db_symbol(symbol))
        ledger = LedgerSnapshot(
            has_open_position_for_symbol=has_open,
            open_position_count=open_count,
            orders_today=history.orders_today,
            realized_loss_today_krw=history.realized_loss_today_krw,
            seconds_since_last_close_for_symbol=(
                history.seconds_since_last_close_for_symbol
            ),
        )
        return RiskInputs(ledger=ledger, market=market)


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
        self, *, correlation_id: str, symbol: str, side: Side, detail: str
    ) -> None:
        # ROB-843 P2: persist the anomaly's real leg/side so it de-dups with the
        # native row for the same order in the daily count (entry_unfilled=buy,
        # exit_unconfirmed=sell) rather than counting as a phantom second order.
        db_side = "buy" if side == "BUY" else "sell"
        scalping_role = "entry" if side == "BUY" else "exit"
        await _save_kis_mock_order_ledger(
            symbol=symbol,
            instrument_type="equity_kr",
            side=db_side,
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
            scalping_role=scalping_role,
        )
