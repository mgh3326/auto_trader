"""ROB-843 P1 — write-ahead reservation lifecycle for KIS mock scalping entries.

A durable reservation is recorded BEFORE the broker POST. If the durable write
fails the POST never happens; the reservation is released only when the order is
confirmed fully tracked or proven not sent, and an unresolved reservation is the
restart-safe fail-close signal.
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.services.brokers.kis.circuit_breaker as cb
from app.core.config import settings
from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_validation
from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory
from app.mcp_server.tooling.order_execution import OrderSendOutcomeUnknown
from app.models.review import KISMockOrderLedger, OrderSendIntent
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.domestic_orders import DomesticOrderClient
from app.services.brokers.kis.mock_scalping_exec import adapters
from app.services.brokers.kis.mock_scalping_exec.reservation import (
    has_unresolved_entries,
    reserve_entry,
)
from app.services.brokers.kis.mock_scalping_exec.tracking_state import LedgerWriteError
from app.services.brokers.kis.mock_scalping_ws.state import MarketState
from app.services.order_send_intent_service import KIS_MOCK_SCALPING_SCOPE

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
    def __init__(self, execute) -> None:  # type: ignore[override]
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

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _Settings()

    def _kis_url(self, path: str) -> str:
        return f"https://mockhost{path}"

    async def _ensure_token(self) -> None:
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
) -> None:
    domestic = DomesticOrderClient(_Parent(execute))
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


async def _has_key(correlation_id: str) -> bool:
    async with _order_session_factory()() as db:
        found = await db.scalar(
            select(OrderSendIntent.id).where(
                OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE,
                OrderSendIntent.idempotency_key == correlation_id,
            )
        )
    return found is not None


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
    assert await _has_key(cid) is True


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
