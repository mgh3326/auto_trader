"""hk 4712 (3797 T-P2) — KR Kiwoom mock place path: reservation + ledger outcomes.

Unit tests with a stub Kiwoom client and an in-memory recorder. No database, no
network, no broker. Every refused path is proven to never reach the broker by a
stub call count of zero.

Invariants (design §6.1) mapped to tests at the bottom of the module docstring
of the PR report; each test names the invariant it proves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.brokers.kiwoom.client import KiwoomPreDispatchError
from app.services.kiwoom_mock_order_ledger_service import (
    CLIENT_ORDER_ID_PREFIX,
    build_client_order_id,
    kr_trading_day,
)
from tests.test_mcp_kiwoom_order_variants import (
    DummyMCP,
    FakeKiwoomPlaceRecorder,
    _install_fake_place_recorder,
    _patch_preflight_success,
)

pytestmark = pytest.mark.unit

FROZEN_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # 2026-11-16 10:00 KST


class StubOrderClient:
    """Counts broker POSTs; ``behaviour`` decides each answer in order."""

    def __init__(self, recorder: FakeKiwoomPlaceRecorder, behaviour: list[Any]):
        self.recorder = recorder
        self.behaviour = behaviour
        self.posts: list[dict[str, Any]] = []

    def factory(self):
        stub = self

        class _OrderClient:
            def __init__(self, _client):
                pass

            async def _post(self, method: str, **kwargs):
                stub.posts.append({"method": method, **kwargs})
                stub.recorder.events.append("post")
                step = stub.behaviour[min(len(stub.posts), len(stub.behaviour)) - 1]
                if isinstance(step, BaseException):
                    raise step
                return step

            async def place_buy_order(self, **kwargs):
                return await self._post("buy", **kwargs)

            async def place_sell_order(self, **kwargs):
                return await self._post("sell", **kwargs)

        return _OrderClient


def _wire(monkeypatch, behaviour: list[Any]):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class _FakeClient:
        pass

    recorder = _install_fake_place_recorder(monkeypatch, mod)
    stub = StubOrderClient(recorder, behaviour)
    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", lambda: _FakeClient())
    monkeypatch.setattr(mod, "KiwoomDomesticOrderClient", stub.factory())
    monkeypatch.setattr(mod, "_utc_now", lambda: FROZEN_NOW)
    _patch_preflight_success(monkeypatch, mod)
    mcp = DummyMCP()
    mod.register(mcp)
    return mod, mcp, recorder, stub


async def _place(mcp, **overrides):
    kwargs: dict[str, Any] = {
        "symbol": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "dry_run": False,
        "confirm": True,
    }
    kwargs.update(overrides)
    return await mcp.tools["kiwoom_mock_place_order"](**kwargs)


def _key(**overrides) -> str:
    args: dict[str, Any] = {
        "symbol": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "exchange": "KRX",
        "trading_day": kr_trading_day(FROZEN_NOW),
        "rung": None,
    }
    args.update(overrides)
    return build_client_order_id(**args)


ACK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}


# --- client_order_id (design §2.2) -------------------------------------------


def test_client_order_id_is_deterministic_and_prefixed():
    first = _key()
    assert first == _key()
    prefix, _, digest = first.partition("-")
    assert prefix == CLIENT_ORDER_ID_PREFIX == "kwoomm"
    assert len(digest) == 16 and int(digest, 16) >= 0


def test_client_order_id_salt_rung_and_order_content_separate_keys():
    base = _key()
    assert _key(trading_day="20261117") != base  # next trading day is new
    assert _key(rung=2) != base  # a deliberate second identical order
    assert _key(rung=2) != _key(rung=3)
    assert _key(rung="2") == _key(rung=2)
    assert _key(price=70100) != base
    assert _key(quantity=4) != base
    assert _key(side="sell") != base
    assert _key(symbol="000660") != base


def test_kr_trading_day_is_the_kst_date():
    # 2026-11-15 16:00 UTC is already 2026-11-16 01:00 KST
    assert kr_trading_day(datetime(2026, 11, 15, 16, 0, tzinfo=UTC)) == "20261116"
    assert kr_trading_day(datetime(2026, 11, 15, 14, 59, tzinfo=UTC)) == "20261115"


# --- five outcomes (invariants I1, I2, I3) -----------------------------------


@pytest.mark.asyncio
async def test_accepted_reserves_before_post_then_records_accepted(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp)

    # I2: the reservation is committed before the broker POST.
    assert recorder.events == ["reserve", "post", "record:accepted"]
    assert len(stub.posts) == 1
    row = recorder.rows[0]
    assert row["status"] == "accepted"
    assert row["broker_order_id"] == "0000111222"
    assert row["client_order_id"] == row["idempotency_key"] == _key()
    assert row["trading_day"] == "20261116"
    assert (row["symbol"], row["side"], row["quantity"], row["price"]) == (
        "005930",
        "buy",
        3,
        70000,
    )
    # response shape unchanged, plus the ledger row id/status
    assert response["success"] is True
    assert response["broker_response"] == ACK
    assert response["ord_no"] == "0000111222"
    assert response["ledger_id"] == 1
    assert response["ledger_status"] == "accepted"
    assert response["client_order_id"] == _key()
    assert recorder.reserved == {_key(): 1}  # consumed, not released


@pytest.mark.asyncio
async def test_non_zero_return_code_records_rejected_and_keeps_reservation(
    monkeypatch,
):
    reject = {"return_code": 20, "return_msg": "주문가능금액 부족"}
    _, mcp, recorder, stub = _wire(monkeypatch, [reject])

    response = await _place(mcp)

    assert recorder.events == ["reserve", "post", "record:rejected"]
    assert recorder.rows[0]["status"] == "rejected"
    assert recorder.rows[0]["broker_order_id"] is None
    assert recorder.rows[0]["response_code"] == "20"
    assert response["success"] is False
    assert response["ledger_status"] == "rejected"
    # the broker answered: the reservation stays consumed
    assert _key() in recorder.reserved
    assert len(stub.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"return_code": 0, "return_msg": "정상"},  # no order number
        {"return_code": 0, "ord_no": ""},
        {"return_code": 0, "ord_no": "../1"},  # unsafe
        {"return_code": 0, "ord_no": "0001", "order_no": "0002"},  # conflicting
        {"return_msg": "?"},  # no return_code at all proves nothing
        {"return_code": "x", "ord_no": "0001"},  # unparseable return_code
    ],
)
async def test_unprovable_acknowledgement_is_uncertain_and_not_retryable(
    monkeypatch, answer
):
    _, mcp, recorder, stub = _wire(monkeypatch, [answer])

    response = await _place(mcp)

    assert recorder.events == ["reserve", "post", "record:uncertain"]
    assert recorder.rows[0]["status"] == "uncertain"
    assert response["ledger_status"] == "uncertain"
    assert response["reconcile_required"] is True
    assert response["retry_allowed"] is False
    assert _key() in recorder.reserved
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_pre_dispatch_error_writes_no_row_and_releases_the_reservation(
    monkeypatch,
):
    pre = KiwoomPreDispatchError(
        stage="token", api_id="kt10000", cause_type="ConnectError"
    )
    _, mcp, recorder, stub = _wire(monkeypatch, [pre, ACK])

    first = await _place(mcp)

    assert first["success"] is False
    assert first["status"] == "not_submitted"
    assert first["dispatch_started"] is False
    assert first["reservation_released"] is True
    assert recorder.events == ["reserve", "post", "release"]
    assert recorder.rows == []
    assert recorder.reserved == {}

    # the same key may retry once the release proved nothing was sent
    second = await _place(mcp)

    assert second["success"] is True
    assert second["ledger_status"] == "accepted"
    assert len(stub.posts) == 2
    assert list(recorder.reserved) == [_key()]


@pytest.mark.asyncio
async def test_failed_release_after_pre_dispatch_error_is_reported_not_raised(
    monkeypatch,
):
    pre = KiwoomPreDispatchError(
        stage="token", api_id="kt10000", cause_type="ConnectError"
    )
    _, mcp, recorder, _ = _wire(monkeypatch, [pre])
    recorder.release_error = RuntimeError("db down")

    response = await _place(mcp)

    assert response["status"] == "not_submitted"
    assert response["reservation_released"] is False
    assert recorder.rows == []


@pytest.mark.asyncio
async def test_unknown_dispatch_records_uncertain_and_is_never_resent(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [TimeoutError("read timeout"), ACK])

    first = await _place(mcp)

    assert first["success"] is False
    assert first["status"] == "acceptance_uncertain"
    assert first["reconcile_required"] is True
    assert first["retry_allowed"] is False
    assert first["ledger_status"] == "uncertain"
    assert recorder.events == ["reserve", "post", "record:uncertain"]
    assert recorder.rows[0]["status"] == "uncertain"
    assert recorder.rows[0]["broker_order_id"] is None
    assert _key() in recorder.reserved  # kept
    assert len(stub.posts) == 1

    # I3: a second identical call must not resend
    second = await _place(mcp)

    assert second["success"] is False
    assert second["error"] == "kiwoom_mock_duplicate_order_intent"
    assert len(stub.posts) == 1
    assert len(recorder.rows) == 1


# --- refused paths never reach the broker (I2) -------------------------------


@pytest.mark.asyncio
async def test_duplicate_key_is_refused_before_any_broker_call(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])
    recorder.reserved[_key()] = 99  # someone already holds this key

    response = await _place(mcp)

    assert len(stub.posts) == 0
    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_duplicate_order_intent"
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert response["client_order_id"] == _key()
    assert recorder.events == ["reserve"]
    assert recorder.rows == []


@pytest.mark.asyncio
async def test_second_identical_confirmed_call_is_refused_after_accept(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])

    await _place(mcp)
    again = await _place(mcp)

    assert len(stub.posts) == 1
    assert again["error"] == "kiwoom_mock_duplicate_order_intent"
    assert len(recorder.rows) == 1


@pytest.mark.asyncio
async def test_rung_allows_a_deliberate_second_identical_order(monkeypatch):
    # ``rung`` is an internal discriminator (the public tool signature is pinned
    # by test_static_boundaries); a proposal caller passes it to the impl.
    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])
    impl_kwargs: dict[str, Any] = {
        "symbol": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "exchange": "KRX",
        "dry_run": False,
    }

    await mod._kiwoom_mock_place_order_impl(**impl_kwargs)
    refused = await mod._kiwoom_mock_place_order_impl(**impl_kwargs)
    second = await mod._kiwoom_mock_place_order_impl(**impl_kwargs, rung=2)

    assert refused["error"] == "kiwoom_mock_duplicate_order_intent"
    assert second["success"] is True
    assert len(stub.posts) == 2
    assert {row["client_order_id"] for row in recorder.rows} == {
        _key(),
        _key(rung=2),
    }


@pytest.mark.asyncio
async def test_reservation_failure_fails_closed_without_a_broker_call(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])
    recorder.reserve_error = ConnectionError("db down")

    response = await _place(mcp)

    assert len(stub.posts) == 0
    assert response["success"] is False
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert "kiwoom_mock_send_intent_unavailable" in response["error"]
    assert recorder.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"dry_run": True, "confirm": False},
        {"dry_run": True, "confirm": True},
        {"dry_run": False, "confirm": False},
    ],
    ids=["dry-run", "dry-run-with-confirm", "confirm-missing"],
)
async def test_dry_run_and_missing_confirm_write_and_reserve_nothing(
    monkeypatch, kwargs
):
    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    def _forbidden():
        raise AssertionError("persistence must not be touched")

    monkeypatch.setattr(mod, "_new_place_recorder", _forbidden)

    response = await _place(mcp, **kwargs)

    assert len(stub.posts) == 0
    assert recorder.events == []
    assert "ledger_id" not in response
    if not kwargs["dry_run"]:
        assert "confirm=True" in response["error"]


@pytest.mark.asyncio
async def test_preflight_failure_reserves_nothing(monkeypatch):
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="insufficient cash",
            checks=[],
        )

    monkeypatch.setattr(mod, "_run_preflight_for_kiwoom_mock", failing_preflight)

    response = await _place(mcp)

    assert response["success"] is False
    assert len(stub.posts) == 0
    assert recorder.events == []


# --- recording failure after the broker answered (acceptance 4) --------------


@pytest.mark.asyncio
async def test_ledger_write_failure_after_ack_keeps_broker_result_and_never_resends(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])
    recorder.record_error = RuntimeError("ledger insert failed")

    response = await _place(mcp)

    # the broker accepted: that truth is not downgraded to "not sent"
    assert response["success"] is True
    assert response["broker_response"] == ACK
    assert response["ord_no"] == "0000111222"
    assert response["ledger_recorded"] is False
    assert response["ledger_recording_error"] == "RuntimeError"
    assert response["reconcile_required"] is True
    assert response["retry_allowed"] is False
    assert response.get("status") != "not_submitted"
    assert "ledger_id" not in response
    # the reservation is kept and the broker was called exactly once
    assert recorder.events == ["reserve", "post", "record:accepted"]
    assert _key() in recorder.reserved
    assert len(stub.posts) == 1

    # a retry of the same order is refused before the broker
    retry = await _place(mcp)

    assert retry["error"] == "kiwoom_mock_duplicate_order_intent"
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_ledger_write_failure_after_unknown_dispatch_stays_uncertain(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [TimeoutError("boom"), ACK])
    recorder.record_error = RuntimeError("ledger insert failed")

    response = await _place(mcp)

    assert response["status"] == "acceptance_uncertain"
    assert response["ledger_recorded"] is False
    assert response["retry_allowed"] is False
    assert _key() in recorder.reserved
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_sell_side_uses_the_sell_endpoint_and_its_own_key(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp, side="sell")

    assert stub.posts[0]["method"] == "sell"
    assert response["client_order_id"] == _key(side="sell")
    assert recorder.rows[0]["side"] == "sell"
