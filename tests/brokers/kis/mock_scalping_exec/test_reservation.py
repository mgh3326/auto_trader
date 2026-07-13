"""ROB-843 P1 — write-ahead reservation lifecycle for KIS mock scalping legs.

A durable reservation is recorded BEFORE the broker POST. If the durable write
fails the POST never happens; the reservation is released only when the order is
confirmed fully tracked or proven not sent, and an unresolved reservation is the
restart-safe fail-close signal.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

import app.services.brokers.kis.circuit_breaker as cb
from app.core.config import settings
from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_validation
from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory
from app.mcp_server.tooling.order_execution import OrderSendOutcomeUnknown
from app.models.review import KISMockOrderLedger, OrderSendIntent
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.domestic_orders import DomesticOrderClient
from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
)
from app.services.brokers.kis.mock_scalping.order_intent import OrderIntent
from app.services.brokers.kis.mock_scalping_exec import adapters
from app.services.brokers.kis.mock_scalping_exec.executor import (
    ExecutorConfig,
    Fill,
    MockScalpingExecutor,
    RiskInputs,
)
from app.services.brokers.kis.mock_scalping_exec.reservation import (
    has_unresolved_entries,
    reconcile_entries,
    release_entry,
    reserve_entry,
)
from app.services.brokers.kis.mock_scalping_exec.tracking_state import LedgerWriteError
from app.services.brokers.kis.mock_scalping_ws.state import MarketState
from app.services.order_send_intent_service import (
    KIS_MOCK_SCALPING_SCOPE,
    OrderSendIntentService,
)

_NXT = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"
_TEST_CID_PREFIX = "resv-test-"


class _Settings:
    kis_app_key = "k"
    kis_app_secret = "s"
    kis_access_token = "t"
    kis_account_no = "1234567890"
    api_rate_limit_retry_429_max = 0
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0


class _Parent(BaseKISClient):
    def __init__(self, execute, *, token_error: Exception | None = None) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None
        self._hdr_base = {"content-type": "application/json"}
        token = MagicMock()
        token.clear_token = AsyncMock()
        self._token_manager = token
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        self._get_limiter = AsyncMock(return_value=limiter)  # type: ignore[method-assign]
        self._ensure_client = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        self._execute_http_request = execute  # type: ignore[method-assign]
        self._token_error = token_error

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _Settings()

    def _kis_url(self, path: str) -> str:
        return f"https://mockhost{path}"

    async def _ensure_token(self) -> None:
        if self._token_error is not None:
            raise self._token_error
        return None


class _DomesticFacade:
    def __init__(self, client: DomesticOrderClient) -> None:
        self._client = client

    async def order_korea_stock(self, **kwargs):
        return await self._client.order_korea_stock(**kwargs)


def _http_response(payload: dict, *, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    response.json = lambda: payload
    if status_code >= 400:
        request = httpx.Request("POST", "https://mockhost/order")
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=request, response=response
        )
    return response


def _patch_production_domestic_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    execute,
    side: str,
    balance_error: dict | None = None,
    token_error: Exception | None = None,
) -> None:
    domestic = DomesticOrderClient(_Parent(execute, token_error=token_error))
    facade = _DomesticFacade(domestic)
    monkeypatch.setattr(order_execution, "_create_kis_client", lambda **_kw: facade)
    monkeypatch.setattr(_NXT, AsyncMock(return_value=False))
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=70000.0)
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": side,
                "order_type": "limit",
                "price": 70000.0,
                "quantity": 1.0,
                "estimated_value": 70000.0,
                "fee": 0.0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, balance_error)),
    )
    monkeypatch.setattr(
        order_execution,
        "evaluate_sector_concentration",
        AsyncMock(return_value={"verdict": "ok"}),
    )
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(
        kis_mock_ledger,
        "_fetch_kis_mock_baseline_qty",
        AsyncMock(return_value=Decimal("0")),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 70000.0, "quantity": 1.0}),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_kis_mock_shadow_exposure",
        AsyncMock(
            return_value={
                "confidence": "db_shadow_pending",
                "sell_reserved_quantity": 0,
                "buy_reserved_amount": 0,
            }
        ),
    )
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)


async def _has_key(correlation_id: str, side: str | None = None) -> bool:
    async with _order_session_factory()() as db:
        stmt = select(OrderSendIntent.id).where(
            OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE,
            OrderSendIntent.idempotency_key.contains(correlation_id),
        )
        if side is not None:
            stmt = stmt.where(func.lower(OrderSendIntent.side) == side.lower())
        found = await db.scalar(stmt)
    return found is not None


async def _ledger_rows(correlation_id: str) -> int:
    async with _order_session_factory()() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(KISMockOrderLedger)
            .where(KISMockOrderLedger.correlation_id == correlation_id)
        )
    return int(count or 0)


async def _reservation_row(correlation_id: str, side: str) -> tuple[int, str] | None:
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(OrderSendIntent.id, OrderSendIntent.idempotency_key).where(
                    OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE,
                    OrderSendIntent.idempotency_key.contains(correlation_id),
                    func.lower(OrderSendIntent.side) == side.lower(),
                )
            )
        ).one_or_none()
    return (row.id, row.idempotency_key) if row is not None else None


@pytest_asyncio.fixture(autouse=True)
async def _clear_reservations():
    async def _c():
        async with _order_session_factory()() as db:
            await db.execute(
                delete(OrderSendIntent).where(
                    OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE
                )
            )
            await db.execute(
                delete(KISMockOrderLedger).where(
                    KISMockOrderLedger.correlation_id.like(f"{_TEST_CID_PREFIX}%")
                )
            )
            await db.commit()

    cb.reset_kis_circuit_breaker()
    await _c()
    yield
    await _c()
    cb.reset_kis_circuit_breaker()


def _broker():
    now = time.monotonic()
    state = MarketState(symbol="005930", bid=69990.0, ask=70000.0, _book_updated_at=now)
    b = adapters.KisMockBroker(
        get_state=lambda _s: state,
        clock=lambda: now + 0.1,
    )
    # confirm=True normally reads the live mock balance; stub it for tests.
    b._capture_baseline = AsyncMock(return_value={"symbol": "005930"})  # type: ignore[method-assign]
    return b


async def _submit(broker, cid: str):
    return await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id=cid,
        confirm=True,
    )


async def _submit_sell(broker, cid: str):
    return await broker.submit_exit_sell(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        exit_reason="time_stop",
        strategy_id="kis-mock-v1",
        correlation_id=cid,
        confirm=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_failure_blocks_post(monkeypatch) -> None:
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)
    monkeypatch.setattr(
        adapters, "reserve_entry", AsyncMock(side_effect=RuntimeError("db down"))
    )
    result = await _submit(_broker(), "cid-reserve-fail")
    assert result["reservation_blocked"] is True
    assert "reservation_unavailable" in result["reason_codes"]
    assert place.await_count == 0  # POST 0 — durable write failed first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_reservation_blocks_post(monkeypatch) -> None:
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)
    cid = "cid-dup"
    await reserve_entry(correlation_id=cid, symbol="005930", side="buy")
    result = await _submit(_broker(), cid)
    assert result["reservation_blocked"] is True
    assert "duplicate_send" in result["reason_codes"]
    assert place.await_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_raw_same_leg_reservation_blocks_new_leg_key_post(
    monkeypatch,
) -> None:
    """A pre-leg-key unresolved BUY remains a same-leg double-send guard."""
    cid = f"{_TEST_CID_PREFIX}legacy-buy"
    async with _order_session_factory()() as db:
        await OrderSendIntentService(db).reserve(
            account_scope=KIS_MOCK_SCALPING_SCOPE,
            idempotency_key=cid,
            symbol="005930",
            side="buy",
        )
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)

    result = await _submit(_broker(), cid)

    assert result["reservation_blocked"] is True
    assert "duplicate_send" in result["reason_codes"]
    assert place.await_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_success_releases_reservation(monkeypatch) -> None:
    async def _accepted(**kwargs):
        kwargs["send_outcome"].mark_accepted()
        return {"success": True}

    monkeypatch.setattr(adapters, "_place_order_impl", AsyncMock(side_effect=_accepted))
    await _submit(_broker(), "cid-ok")
    assert await has_unresolved_entries() is False  # released — fully tracked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_lost_keeps_reservation(monkeypatch) -> None:
    async def _accepted_untracked(**kwargs):
        kwargs["send_outcome"].mark_accepted()
        return {"success": True, "ledger_tracking_unavailable": True}

    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(side_effect=_accepted_untracked),
    )
    await _submit(_broker(), "cid-native-lost")
    assert await has_unresolved_entries() is True  # kept — uncertain/lost


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uncertain_send_keeps_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(side_effect=OrderSendOutcomeUnknown(TimeoutError("t"))),
    )
    with pytest.raises(OrderSendOutcomeUnknown):
        await _submit(_broker(), "cid-uncertain")
    assert await has_unresolved_entries() is True  # kept — outcome unknown


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deterministic_rejection_releases_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(side_effect=RuntimeError("40 rejected")),
    )
    with pytest.raises(RuntimeError):
        await _submit(_broker(), "cid-rejected")
    assert await has_unresolved_entries() is False  # released — no order created


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pre_send_block_releases_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(return_value={"success": False, "pre_send_blocked": True}),
    )
    await _submit(_broker(), "cid-presend")
    assert await has_unresolved_entries() is False  # released — POST 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_production_domestic_rejection_releases_buy_reservation(
    monkeypatch,
) -> None:
    """The real domestic service raises for provider rejection, then
    _place_order_impl normalizes it to success=False. That production return shape
    is still a definitive no-order and must release the write-ahead reservation."""
    execute = AsyncMock(
        return_value=_http_response(
            {"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "order rejected"}
        )
    )
    _patch_production_domestic_path(monkeypatch, execute=execute, side="buy")

    result = await _submit(_broker(), f"{_TEST_CID_PREFIX}buy-rejected")

    assert result["success"] is False
    assert result.get("error") == "APBK0013 order rejected", result
    assert execute.await_count == 1
    assert await has_unresolved_entries() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_production_pre_send_validation_releases_buy_reservation(
    monkeypatch,
) -> None:
    execute = AsyncMock()
    _patch_production_domestic_path(
        monkeypatch,
        execute=execute,
        side="buy",
        balance_error={
            "success": False,
            "error": "insufficient balance",
            "source": "kis",
            "symbol": "005930",
            "instrument_type": "equity_kr",
        },
    )

    result = await _submit(_broker(), f"{_TEST_CID_PREFIX}buy-validation")

    assert result["success"] is False
    assert execute.await_count == 0
    assert await has_unresolved_entries() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_reserve_failure_blocks_before_place(monkeypatch) -> None:
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)
    monkeypatch.setattr(
        adapters, "reserve_entry", AsyncMock(side_effect=RuntimeError("db down"))
    )

    result = await _submit_sell(_broker(), f"{_TEST_CID_PREFIX}sell-reserve-fail")

    assert result["reservation_blocked"] is True
    assert "reservation_unavailable" in result["reason_codes"]
    assert place.await_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_provider_rejection_reserved_before_post_then_released(
    monkeypatch,
) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-rejected"
    observed = {"reserved_at_post": False}

    async def execute(*_args, **_kwargs):
        observed["reserved_at_post"] = await _has_key(cid)
        return _http_response(
            {"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "order rejected"}
        )

    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")

    result = await _submit_sell(_broker(), cid)

    assert observed["reserved_at_post"] is True
    assert result["success"] is False
    assert await _has_key(cid) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_tracked_accept_reserved_before_post_then_released(
    monkeypatch,
) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-tracked"
    observed = {"reserved_at_post": False}

    async def execute(*_args, **_kwargs):
        observed["reserved_at_post"] = await _has_key(cid)
        return _http_response(
            {
                "rt_cd": "0",
                "msg_cd": "0",
                "msg1": "accepted",
                "output": {"ODNO": "RESV-SELL-TRACKED", "ORD_TMD": "091500"},
            }
        )

    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")

    result = await _submit_sell(_broker(), cid)

    assert observed["reserved_at_post"] is True
    assert result["success"] is True, result
    assert result["ledger_tracking_unavailable"] is False
    assert await _has_key(cid) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_accepted_native_write_loss_keeps_reservation(
    monkeypatch,
) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-native-lost"
    execute = AsyncMock(
        return_value=_http_response(
            {
                "rt_cd": "0",
                "msg_cd": "0",
                "msg1": "accepted",
                "output": {"ODNO": "RESV-SELL-LOST", "ORD_TMD": "091500"},
            }
        )
    )
    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")
    monkeypatch.setattr(
        kis_mock_ledger,
        "_save_kis_mock_order_ledger",
        AsyncMock(side_effect=LedgerWriteError("db write lost")),
    )
    monkeypatch.setattr(
        kis_mock_ledger, "_native_row_exists", AsyncMock(return_value=False)
    )

    result = await _submit_sell(_broker(), cid)

    assert result["success"] is True
    assert result["ledger_tracking_unavailable"] is True
    assert await _has_key(cid) is True


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 503])
async def test_sell_server_error_keeps_reservation(monkeypatch, status_code) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-{status_code}"
    execute = AsyncMock(
        return_value=_http_response(
            {"rt_cd": "1", "msg_cd": "SERVER", "msg1": "server error"},
            status_code=status_code,
        )
    )
    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")

    result = await _submit_sell(_broker(), cid)

    assert result["success"] is False
    assert await _has_key(cid) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_timeout_keeps_reservation(monkeypatch) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-timeout"
    execute = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")

    result = await _submit_sell(_broker(), cid)

    assert result["success"] is False
    assert result.get("outcome_unknown") is True, result
    assert execute.await_count == 1
    assert await _has_key(cid, "sell") is True


class _ProcessCrash(BaseException):
    pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_crash_keeps_reservation(monkeypatch) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-crash"
    monkeypatch.setattr(
        adapters, "_place_order_impl", AsyncMock(side_effect=_ProcessCrash())
    )

    with pytest.raises(_ProcessCrash):
        await _submit_sell(_broker(), cid)

    assert await _has_key(cid) is True


class _PassRiskGate:
    async def load(self, *, symbol: str, side: str) -> RiskInputs:
        return RiskInputs(
            ledger=LedgerSnapshot(
                has_open_position_for_symbol=False,
                open_position_count=0,
                orders_today=0,
                realized_loss_today_krw=Decimal("0"),
                seconds_since_last_close_for_symbol=None,
            ),
            market=MarketConditions(spread_bps=Decimal("1"), data_age_seconds=0.1),
        )


class _RoundTripLedger:
    def __init__(self) -> None:
        self.record_entry = AsyncMock()
        self.record_exit_reconciled = AsyncMock()
        self.record_anomaly = AsyncMock()


def _round_trip_intent() -> OrderIntent:
    return OrderIntent(
        symbol="005930",
        side="BUY",
        order_type="limit",
        target_notional_krw=Decimal("70000"),
        entry_reference_price=Decimal("70000"),
        tp_price=Decimal("70210"),
        sl_price=Decimal("69860"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_round_trip_sell_uses_independent_leg_reservation_when_buy_unresolved(
    monkeypatch,
) -> None:
    """A BUY tracking loss must not consume the same-correlation SELL key.

    This runs the real round-trip executor through the real broker adapter. The
    BUY reservation intentionally remains unresolved while the SELL still
    reaches the mutation boundary, then only its fully tracked leg is released.
    """
    cid = f"{_TEST_CID_PREFIX}round-trip-legs"
    submitted_sides: list[str] = []

    async def _place(**kwargs):
        submitted_sides.append(kwargs["side"])
        kwargs["send_outcome"].mark_accepted()
        return {
            "success": True,
            "ledger_tracking_unavailable": kwargs["side"] == "buy",
        }

    monkeypatch.setattr(adapters, "_place_order_impl", AsyncMock(side_effect=_place))
    broker = _broker()
    broker.confirm_fill = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            Fill(price=Decimal("70000"), quantity=Decimal("1")),
            Fill(price=Decimal("70000"), quantity=Decimal("1")),
        ]
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    executor = MockScalpingExecutor(
        broker=broker,
        ledger=_RoundTripLedger(),
        config=ExecutorConfig(max_hold_seconds=0, max_fill_polls=1),
        sleep=_no_sleep,
        clock=lambda: 0.0,
        risk=_PassRiskGate(),
    )
    monkeypatch.setattr(executor, "_new_correlation_id", lambda: cid)

    result = await executor.execute_monitored(_round_trip_intent(), confirm=True)

    assert result.status == "reconciled"
    assert submitted_sides == ["buy", "sell"]
    assert await _has_key(cid, "buy") is True
    assert await _has_key(cid, "sell") is False
    assert await has_unresolved_entries() is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_reconcile_identifies_and_releases_only_confirmed_leg() -> None:
    cid = f"{_TEST_CID_PREFIX}reconcile-legs"
    await reserve_entry(correlation_id=cid, symbol="005930", side="BUY")
    await reserve_entry(correlation_id=cid, symbol="005930", side=" sell ")
    observed: list[tuple[str, str]] = []

    async def _confirm(correlation_id: str, side: str) -> bool:
        observed.append((correlation_id, side))
        return side == "sell"

    released = await reconcile_entries(confirm=_confirm)

    assert released == 1
    assert sorted(observed) == [(cid, "buy"), (cid, "sell")]
    assert await _has_key(cid, "buy") is True
    assert await _has_key(cid, "sell") is False
    assert await has_unresolved_entries() is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_aba_does_not_delete_replacement_row_or_count_release() -> None:
    """A stale confirmation may release only the exact row it observed.

    The reconciler lists row A, then pauses in the external broker confirmation.
    A separate task/session deletes A and reserves the same leg as row B before
    confirmation resumes. The stale reconciler must preserve B and report zero
    releases.
    """
    cid = f"{_TEST_CID_PREFIX}reconcile-aba"
    await reserve_entry(correlation_id=cid, symbol="005930", side="buy")
    original = await _reservation_row(cid, "buy")
    assert original is not None

    confirm_started = asyncio.Event()
    replacement_ready = asyncio.Event()

    async def _confirm(correlation_id: str, side: str) -> bool:
        assert (correlation_id, side) == (cid, "buy")
        confirm_started.set()
        await replacement_ready.wait()
        return True

    reconcile_task = asyncio.create_task(reconcile_entries(confirm=_confirm))
    await confirm_started.wait()

    await release_entry(correlation_id=cid, side="buy")
    await reserve_entry(correlation_id=cid, symbol="005930", side="buy")
    replacement = await _reservation_row(cid, "buy")
    assert replacement is not None
    assert replacement[0] != original[0]
    replacement_ready.set()

    released = await reconcile_task

    assert released == 0
    assert await _reservation_row(cid, "buy") == replacement
    assert await has_unresolved_entries() is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pre_dispatch_token_request_error_releases_reservation(
    monkeypatch,
) -> None:
    cid = f"{_TEST_CID_PREFIX}token-before-dispatch"
    execute = AsyncMock()
    _patch_production_domestic_path(
        monkeypatch,
        execute=execute,
        side="sell",
        token_error=httpx.ConnectError("token endpoint unavailable"),
    )

    result = await _submit_sell(_broker(), cid)

    assert result["success"] is False
    assert result.get("outcome_unknown") is not True
    assert result.get("retry_allowed") is True
    assert execute.await_count == 0
    assert await _has_key(cid, "sell") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_500_success_shaped_payload_cannot_release_reservation(
    monkeypatch,
) -> None:
    cid = f"{_TEST_CID_PREFIX}sell-500-success-payload"
    execute = AsyncMock(
        return_value=_http_response(
            {
                "rt_cd": "0",
                "msg_cd": "0",
                "msg1": "accepted-looking",
                "output": {"ODNO": "UNTRUSTED-500", "ORD_TMD": "091500"},
            },
            status_code=500,
        )
    )
    _patch_production_domestic_path(monkeypatch, execute=execute, side="sell")

    result = await _submit_sell(_broker(), cid)

    assert result["success"] is False, result
    assert result.get("outcome_unknown") is True
    assert execute.await_count == 1
    assert await _has_key(cid, "sell") is True
    assert await _ledger_rows(cid) == 0
