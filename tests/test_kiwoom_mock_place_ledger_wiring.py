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
    FakeKiwoomSignalRecorder,
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


class OrderingSignalRecorder(FakeKiwoomSignalRecorder):
    """Fake signal recorder that snapshots how far the order path had got.

    ``record`` runs before the reservation and before the POST, ``mark_outcome``
    after the ledger row — the two counts below prove that order.
    """

    def __init__(self, place: FakeKiwoomPlaceRecorder) -> None:
        super().__init__()
        self.place = place
        self.reservations_before_record: list[int] = []
        self.ledger_rows_before_mark: list[int] = []

    async def record(self, *, attribution, **kwargs: Any) -> int:
        self.reservations_before_record.append(len(self.place.reserved))
        return await super().record(attribution=attribution, **kwargs)

    async def mark_outcome(self, **kwargs: Any) -> int:
        self.ledger_rows_before_mark.append(len(self.place.rows))
        return await super().mark_outcome(**kwargs)


def _wire(monkeypatch, behaviour: list[Any]):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class _FakeClient:
        pass

    recorder = FakeKiwoomPlaceRecorder()
    monkeypatch.setattr(mod, "_new_place_recorder", lambda: recorder)
    signal_recorder = OrderingSignalRecorder(recorder)
    monkeypatch.setattr(mod, "_new_signal_recorder", lambda: signal_recorder)
    recorder.signal = signal_recorder  # type: ignore[attr-defined]
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
        "strategy": "unit-test",
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
    # an untrackable order is not a confirmed placement
    assert response["success"] is False
    assert response["status"] == "acceptance_uncertain"
    assert "broker_response" in response
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
    # reserve refused, then a read-only status lookup; never post or record
    assert recorder.events == ["reserve", "existing_status"]
    assert recorder.reserved == {_key(): 99}
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
async def test_rung_through_the_public_tool_allows_a_deliberate_second_order(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])

    await _place(mcp)
    refused = await _place(mcp)
    second = await _place(mcp, rung=2)
    third = await _place(mcp, rung=2)

    assert refused["error"] == "kiwoom_mock_duplicate_order_intent"
    assert second["success"] is True
    assert third["error"] == "kiwoom_mock_duplicate_order_intent"
    assert len(stub.posts) == 2
    assert {row["client_order_id"] for row in recorder.rows} == {
        _key(),
        _key(rung=2),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_answer", "expected_reconcile"),
    [
        ({"return_code": 20, "return_msg": "거부"}, False),  # proven rejection
        (ACK, True),
        (TimeoutError("read timeout"), True),
        ({"return_code": 0}, True),
    ],
    ids=["rejected", "accepted", "unknown-dispatch", "untrackable"],
)
async def test_duplicate_refusal_reports_whether_reconciliation_is_needed(
    monkeypatch, first_answer, expected_reconcile
):
    _, mcp, recorder, stub = _wire(monkeypatch, [first_answer, ACK])

    await _place(mcp)
    again = await _place(mcp)

    assert len(stub.posts) == 1
    assert again["error"] == "kiwoom_mock_duplicate_order_intent"
    assert again["reconcile_required"] is expected_reconcile
    assert again["retry_allowed"] is False
    assert again["existing_ledger_status"] == recorder.rows[0]["status"]


@pytest.mark.asyncio
async def test_duplicate_refusal_without_a_ledger_row_requires_reconciliation(
    monkeypatch,
):
    # post-ACK ledger write failed, so no row proves anything about the first send
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])
    recorder.record_error = RuntimeError("ledger insert failed")
    await _place(mcp)
    recorder.record_error = None

    again = await _place(mcp)

    assert again["existing_ledger_status"] is None
    assert again["reconcile_required"] is True
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_duplicate_refusal_with_unreadable_ledger_requires_reconciliation(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [{"return_code": 20}, ACK])
    await _place(mcp)
    recorder.lookup_error = ConnectionError("db down")

    again = await _place(mcp)

    assert again["error"] == "kiwoom_mock_duplicate_order_intent"
    assert again["reconcile_required"] is True  # a failed lookup proves nothing
    assert len(stub.posts) == 1


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


# --- pre-submit attribution gate (hk 4723, design §4.5 / I10) ----------------


@pytest.mark.asyncio
async def test_confirmed_place_without_strategy_sends_nothing_and_reserves_nothing(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp, strategy=None)

    assert response["success"] is False
    assert response["error_code"] == "attribution_required"
    assert response["missing_attribution"] == ["strategy"]
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    # nothing persisted, nothing reserved, nothing sent
    assert recorder.signal.rows == []
    assert recorder.events == []
    assert recorder.reserved == {}
    assert stub.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["", "   ", "test", "null", "tbd"])
async def test_confirmed_place_with_blank_or_placeholder_strategy_is_refused(
    monkeypatch, strategy
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp, strategy=strategy)

    assert response["success"] is False
    assert response["error_code"] == "placeholder_strategy"
    assert response["status"] == "not_submitted"
    assert recorder.signal.rows == []
    assert recorder.events == []
    assert stub.posts == []


@pytest.mark.asyncio
async def test_signal_record_failure_refuses_before_reservation_and_broker(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])
    recorder.signal.record_error = RuntimeError("signal insert failed")

    response = await _place(mcp)

    assert response["success"] is False
    assert response["error_code"] == "signal_record_unavailable"
    assert response["status"] == "not_submitted"
    assert "signal insert failed" not in response["error"]
    assert recorder.signal.reservations_before_record == [0]
    assert recorder.reserved == {}
    assert recorder.events == []
    assert stub.posts == []


@pytest.mark.asyncio
async def test_signal_row_commits_before_reservation_and_send(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp)

    assert response["success"] is True
    # at record time the order path had reserved and posted nothing yet
    assert recorder.signal.reservations_before_record == [0]
    assert len(recorder.signal.rows) == 1
    assert recorder.signal.rows[0]["strategy"] == "unit-test"
    assert recorder.signal.rows[0]["correlation_id"] == response["correlation_id"]
    assert recorder.signal.rows[0]["decision"] == "order"
    assert recorder.events == ["reserve", "post", "record:accepted"]
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_signal_outcome_is_marked_after_the_broker_answer(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp)

    # the ledger row existed before the outcome was marked
    assert recorder.signal.ledger_rows_before_mark == [1]
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "submitted",
            "suppressed_reason": None,
            "detail_patch": {
                "order_no": "0000111222",
                "status": "accepted",
                "ledger_id": 1,
            },
        }
    ]


@pytest.mark.asyncio
async def test_rejected_place_marks_signal_failed(monkeypatch):
    _, mcp, recorder, stub = _wire(
        monkeypatch, [{"return_code": 40, "return_msg": "거부"}]
    )

    response = await _place(mcp)

    assert response["success"] is False
    assert recorder.signal.outcomes[0]["outcome_state"] == "failed"
    assert recorder.signal.outcomes[0]["suppressed_reason"] == "rejected"


@pytest.mark.asyncio
async def test_dry_run_preview_needs_no_strategy_and_writes_nothing(monkeypatch):
    """Matches kis_mock: the preview echoes the order without attribution."""
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    response = await _place(mcp, strategy=None, dry_run=True)

    assert response["dry_run"] is True
    assert recorder.signal.rows == []
    assert recorder.events == []
    assert stub.posts == []


@pytest.mark.asyncio
async def test_a_deliberate_second_rung_gets_its_own_signal_row(monkeypatch):
    """The send key is rung-salted, so the signal row must be rung-salted too.

    Otherwise the second broker order would reuse the first order's signal row
    (ON CONFLICT DO NOTHING) and overwrite its order_no/ledger_id detail.
    """
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])

    first = await _place(mcp)
    second = await _place(mcp, rung=1)

    assert first["success"] is True and second["success"] is True
    assert first["correlation_id"] != second["correlation_id"]
    assert [row["correlation_id"] for row in recorder.signal.rows] == [
        first["correlation_id"],
        second["correlation_id"],
    ]
    assert [outcome["correlation_id"] for outcome in recorder.signal.outcomes] == [
        first["correlation_id"],
        second["correlation_id"],
    ]
    assert len(recorder.reserved) == 2
    assert len(stub.posts) == 2


@pytest.mark.asyncio
async def test_no_rung_and_explicit_zero_are_different_orders(monkeypatch):
    """The send key spells None as "" and zero as "0"; the signal id must too."""
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])

    first = await _place(mcp)
    second = await _place(mcp, rung=0)

    assert first["success"] is True and second["success"] is True
    assert first["client_order_id"] != second["client_order_id"]
    assert first["correlation_id"] != second["correlation_id"]
    assert [row["correlation_id"] for row in recorder.signal.rows] == [
        first["correlation_id"],
        second["correlation_id"],
    ]
    assert len(recorder.reserved) == 2
    assert len(stub.posts) == 2


# --- pre-send refusals close the signal out as not sent (hk 4729) -----------
#
# 'recorded' must mean "committed, no outcome yet". A refusal after the signal
# commit that provably sent no order is marked ``suppressed`` — the vocabulary's
# not-sent value, the same one kis_mock uses for ``decision='no_order'`` — so a
# 'recorded' row can no longer be mistaken for a dispatch that crashed before
# marking. The write is a compare-and-set on 'recorded' (see the DB test in
# tests/test_kiwoom_mock_attribution_db.py), so it never relabels a row an
# earlier dispatch already closed.


@pytest.mark.asyncio
async def test_preflight_refusal_marks_the_signal_not_sent(monkeypatch):
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    async def refusing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="insufficient cash",
            checks=[],
        )

    monkeypatch.setattr(mod, "_run_preflight_for_kiwoom_mock", refusing_preflight)

    response = await _place(mcp)

    assert stub.posts == []  # preflight is the only broker read; no order left
    assert response["success"] is False
    assert response["error"] == PREFLIGHT_CASH_INSUFFICIENT
    assert recorder.signal.outcomes == [
        {
            "correlation_id": recorder.signal.rows[0]["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "preflight_refused",
            "detail_patch": {
                "status": "not_submitted",
                "error_code": PREFLIGHT_CASH_INSUFFICIENT,
            },
        }
    ]


@pytest.mark.asyncio
async def test_preflight_error_marks_the_signal_not_sent(monkeypatch):
    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    async def raising_preflight(**_kwargs):
        raise KiwoomPreDispatchError(
            stage="quote", api_id="ka10004", cause_type="ConnectError"
        )

    monkeypatch.setattr(mod, "_run_preflight_for_kiwoom_mock", raising_preflight)

    response = await _place(mcp)

    assert stub.posts == []
    assert response["status"] == "not_submitted"
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "preflight_refused",
            "detail_patch": {
                "status": "not_submitted",
                "stage": "quote",
                "cause_type": "ConnectError",
            },
        }
    ]


@pytest.mark.asyncio
async def test_preflight_crash_marks_the_signal_not_sent(monkeypatch):
    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK])

    async def crashing_preflight(**_kwargs):
        raise RuntimeError("quote reader exploded")

    monkeypatch.setattr(mod, "_run_preflight_for_kiwoom_mock", crashing_preflight)

    response = await _place(mcp)

    assert stub.posts == []
    assert response["error"] == "kiwoom_mock_place_order preflight failed: RuntimeError"
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "preflight_refused",
            "detail_patch": {
                "status": "not_submitted",
                "error_class": "RuntimeError",
            },
        }
    ]


@pytest.mark.asyncio
async def test_duplicate_intent_marks_the_signal_not_sent(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])
    recorder.reserved[_key()] = 99  # an earlier attempt holds this key

    response = await _place(mcp)

    assert stub.posts == []
    assert response["error"] == "kiwoom_mock_duplicate_order_intent"
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "duplicate_intent",
            "detail_patch": {
                "status": "not_submitted",
                "client_order_id": _key(),
                "existing_ledger_status": None,
                "reconcile_required": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_reservation_unavailable_marks_the_signal_not_sent(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK])
    recorder.reserve_error = ConnectionError("db down")

    response = await _place(mcp)

    assert stub.posts == []
    assert response["status"] == "not_submitted"
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "send_intent_unavailable",
            "detail_patch": {
                "status": "not_submitted",
                "client_order_id": _key(),
                "error_class": "ConnectionError",
            },
        }
    ]


@pytest.mark.asyncio
async def test_post_pre_dispatch_error_marks_the_signal_not_sent(monkeypatch):
    pre = KiwoomPreDispatchError(
        stage="token", api_id="kt10000", cause_type="ConnectError"
    )
    _, mcp, recorder, stub = _wire(monkeypatch, [pre])

    response = await _place(mcp)

    # the order client was entered once; KiwoomPreDispatchError is raised
    # before the HTTP send, which is what makes this "provably never sent".
    assert len(stub.posts) == 1
    assert response["status"] == "not_submitted"
    assert response["reservation_released"] is True
    assert recorder.signal.outcomes == [
        {
            "correlation_id": response["correlation_id"],
            "outcome_state": "suppressed",
            "suppressed_reason": "pre_dispatch_error",
            "detail_patch": {
                "status": "not_submitted",
                "client_order_id": _key(),
                "stage": "token",
                "cause_type": "ConnectError",
                "reservation_released": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_unknown_dispatch_is_never_marked_not_sent(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [TimeoutError("read timeout")])

    response = await _place(mcp)

    assert len(stub.posts) == 1
    assert response["reconcile_required"] is True
    # a dispatch that may have reached the broker keeps today's label
    assert [
        (outcome["outcome_state"], outcome["suppressed_reason"])
        for outcome in recorder.signal.outcomes
    ] == [("failed", "uncertain")]


@pytest.mark.asyncio
async def test_duplicate_refusal_never_relabels_an_accepted_signal(monkeypatch):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])

    first = await _place(mcp)
    second = await _place(mcp)

    assert second["error"] == "kiwoom_mock_duplicate_order_intent"
    assert [outcome["outcome_state"] for outcome in recorder.signal.outcomes] == [
        "submitted"
    ]
    assert recorder.signal.outcomes[0]["correlation_id"] == first["correlation_id"]
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_marking_failure_leaves_the_refusal_response_unchanged(monkeypatch):
    _, mcp_ok, recorder_ok, stub_ok = _wire(monkeypatch, [ACK])
    recorder_ok.reserve_error = ConnectionError("db down")
    baseline = await _place(mcp_ok)

    _, mcp_broken, recorder_broken, stub_broken = _wire(monkeypatch, [ACK])
    recorder_broken.reserve_error = ConnectionError("db down")
    recorder_broken.signal.mark_error = RuntimeError("signal update failed")
    degraded = await _place(mcp_broken)

    assert degraded == baseline
    assert degraded["status"] == "not_submitted"
    assert degraded["error"].startswith("kiwoom_mock_send_intent_unavailable")
    assert stub_broken.posts == []
    assert recorder_broken.signal.outcomes == []


# --- a replayed correlation id is never relabelled (mergewake round 1) ------
#
# 'recorded' means "outcome unknown", not "not sent". A replay reuses the first
# attempt's row, and that attempt may already have placed the order (an
# accepted ACK whose best-effort mark failed, or a send still in flight).
# Only a row THIS call created proves nothing was ever dispatched under the id.


@pytest.mark.asyncio
async def test_replayed_signal_is_not_marked_not_sent_by_a_duplicate_refusal(
    monkeypatch,
):
    _, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])
    recorder.signal.mark_error = RuntimeError("signal mark failed")

    first = await _place(mcp)

    assert first["success"] is True  # the order WAS accepted at the broker
    assert recorder.signal.outcomes == []  # its mark failed: the row is 'recorded'

    recorder.signal.mark_error = None
    second = await _place(mcp)

    assert second["error"] == "kiwoom_mock_duplicate_order_intent"
    assert second["reconcile_required"] is True
    # the replay must not claim "never sent" for the earlier attempt's order
    assert recorder.signal.outcomes == []
    assert len(recorder.signal.rows) == 1
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_replayed_signal_is_not_marked_not_sent_by_a_preflight_refusal(
    monkeypatch,
):
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    mod, mcp, recorder, stub = _wire(monkeypatch, [ACK, ACK])
    recorder.signal.mark_error = RuntimeError("signal mark failed")
    await _place(mcp)  # accepted at the broker; the row stays 'recorded'

    recorder.signal.mark_error = None

    async def refusing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="insufficient cash",
            checks=[],
        )

    monkeypatch.setattr(mod, "_run_preflight_for_kiwoom_mock", refusing_preflight)

    second = await _place(mcp)

    assert second["success"] is False
    assert second["error"] == PREFLIGHT_CASH_INSUFFICIENT
    assert recorder.signal.outcomes == []
    assert len(recorder.signal.rows) == 1
    assert len(stub.posts) == 1
