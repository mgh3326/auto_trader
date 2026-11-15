"""hk 4732 (3797 T-P5) — kiwoom_mock proposals: session, adapter, capability.

Unit tests only: a stub Kiwoom order client and the in-memory recorders from
``tests/test_mcp_kiwoom_order_variants.py`` stand in for the broker and the
database. Every refused path is proven to never reach the broker by a POST
count of zero, and every accepted path runs the same service-layer place
sequence the ``kiwoom_mock_place_order`` MCP tool runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.order_proposals import approval_window as policy
from app.services.order_proposals.auto_approve import _VETO_CAPABLE_ACCOUNT_MARKETS
from app.services.order_proposals.auto_approve_audit import _ENUM_INPUT_VALUES
from app.services.order_proposals.revalidation import _default_place_order_fn
from app.services.order_proposals.service import (
    _ACTION_CAPABILITIES,
    _SUBMITTABLE_ACCOUNT_MODE_MARKETS,
)
from tests.test_mcp_kiwoom_order_variants import (
    FakeKiwoomPlaceRecorder,
    FakeKiwoomSignalRecorder,
)

pytestmark = pytest.mark.unit

FROZEN_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # 2026-11-16 10:00 KST
ACK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}


def _group(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kiwoom_mock",
        "action": "place",
        "order_type": "limit",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --- session policy ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        # 2026-11-16 is a Monday; KRX regular is 09:00-15:30 KST.
        (datetime(2026, 11, 16, 0, 0, tzinfo=UTC), True),  # 09:00 KST open
        (datetime(2026, 11, 16, 6, 29, 59, tzinfo=UTC), True),  # 15:29:59 KST
        (datetime(2026, 11, 16, 6, 30, tzinfo=UTC), False),  # 15:30 KST close
        (datetime(2026, 11, 15, 23, 0, tzinfo=UTC), False),  # 08:00 KST pre-market
        (datetime(2026, 11, 16, 7, 0, tzinfo=UTC), False),  # 16:00 KST NXT after
        (datetime(2026, 11, 16, 11, 0, tzinfo=UTC), False),  # 20:00 KST NXT after
        (datetime(2026, 11, 14, 1, 0, tzinfo=UTC), False),  # Saturday
    ],
)
async def test_kiwoom_mock_session_is_krx_regular_only(at, expected):
    evidence = policy._resolve_kiwoom_mock_session(at)

    assert evidence.known is True
    assert evidence.allowed_now is expected
    assert evidence.allowed_sessions == ("regular",)
    assert evidence.source == "exchange_calendars:XKRX:regular_only"


def test_kiwoom_mock_session_names_the_next_regular_open():
    # 08:00 KST pre-market: NXT would allow it for KIS, but the mock lane has no
    # NXT session — the next allowed instant is the 09:00 KST KRX open.
    pre = policy._resolve_kiwoom_mock_session(datetime(2026, 11, 15, 23, 0, tzinfo=UTC))
    assert pre.current_session == "closed"
    assert pre.next_allowed_at == datetime(2026, 11, 16, 0, 0, tzinfo=UTC)

    # 16:00 KST is the NXT after-market window: still refused, next day opens.
    after = policy._resolve_kiwoom_mock_session(
        datetime(2026, 11, 16, 7, 0, tzinfo=UTC)
    )
    assert after.allowed_now is False
    assert after.next_allowed_at == datetime(2026, 11, 17, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_submission_session_routes_kiwoom_mock_to_the_regular_branch():
    evidence = await policy.resolve_submission_session(_group(), now=FROZEN_NOW)

    assert evidence.known is True
    assert evidence.allowed_now is True
    assert evidence.source == "exchange_calendars:XKRX:regular_only"


@pytest.mark.asyncio
async def test_approval_window_defers_outside_the_regular_session():
    at = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)  # 16:00 KST
    group = _group(valid_until=datetime(2026, 11, 20, tzinfo=UTC))
    decision = await policy.evaluate_approval_window(group, now=at)

    assert decision.code is policy.ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert decision.evidence.allowed_sessions == ("regular",)


# --- capability sets --------------------------------------------------------


def test_kiwoom_mock_is_place_only():
    assert ("kiwoom_mock", "equity_kr") in _SUBMITTABLE_ACCOUNT_MODE_MARKETS
    # replace/cancel still derive from SUPPORTED_TARGET_ACTIONS: the KR mock
    # lane has no targetable broker order in this ticket.
    for action in ("replace", "cancel"):
        assert ("kiwoom_mock", "equity_kr") not in _ACTION_CAPABILITIES[action]
    # No other kiwoom pair leaked in anywhere.
    assert {
        pair for pair in _SUBMITTABLE_ACCOUNT_MODE_MARKETS if pair[0] == "kiwoom_mock"
    } == {("kiwoom_mock", "equity_kr")}
    for action, pairs in _ACTION_CAPABILITIES.items():
        assert not [p for p in pairs if p[0].startswith("kiwoom")] or action == "place"


def test_auto_approve_and_veto_sets_do_not_know_kiwoom_mock():
    assert not [p for p in _VETO_CAPABLE_ACCOUNT_MARKETS if p[0].startswith("kiwoom")]
    assert "kiwoom_mock" not in _ENUM_INPUT_VALUES["account_mode"]


# --- adapter ----------------------------------------------------------------


class _StubOrderClient:
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


def _wire(monkeypatch, behaviour: list[Any] | None = None):
    from app.services import kiwoom_mock_place

    recorder = FakeKiwoomPlaceRecorder()
    signal_recorder = FakeKiwoomSignalRecorder()
    stub = _StubOrderClient(recorder, behaviour or [ACK])

    async def fake_preflight(**_kwargs):
        from app.services.brokers.kiwoom.order_preflight import (
            PREFLIGHT_OK,
            PreflightResult,
        )

        return PreflightResult(
            ok=True, error_code=PREFLIGHT_OK, checks=[], estimated_evidence={}
        )

    monkeypatch.setattr(kiwoom_mock_place, "mock_config_error", lambda: None)
    monkeypatch.setattr(kiwoom_mock_place, "new_kiwoom_mock_client", lambda: object())
    monkeypatch.setattr(kiwoom_mock_place, "run_kiwoom_mock_preflight", fake_preflight)
    monkeypatch.setattr(kiwoom_mock_place, "new_place_recorder", lambda: recorder)
    monkeypatch.setattr(
        kiwoom_mock_place, "new_signal_recorder", lambda: signal_recorder
    )
    monkeypatch.setattr(kiwoom_mock_place, "KiwoomDomesticOrderClient", stub.factory())
    monkeypatch.setattr(
        kiwoom_mock_place, "KiwoomDomesticAccountClient", lambda _client: object()
    )
    monkeypatch.setattr(kiwoom_mock_place, "utc_now", lambda: FROZEN_NOW)
    return recorder, signal_recorder, stub


async def _call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "dry_run": False,
        "account_mode": "kiwoom_mock",
        "symbol": "005930",
        "side": "buy",
        "market": "equity_kr",
        "order_type": "limit",
        "quantity": Decimal("3"),
        "price": Decimal("70000"),
        "thesis": None,
        "strategy": "unit-test",
        "exit_intent": None,
        "exit_reason": None,
        "retrospective_id": None,
        "approval_issue_id": None,
        "reason": "proposal revalidation rung 0",
        "approval_hash": None,
        "rung": 0,
        "correlation_id": "live:kiwoom_mock:testcorr",
        "pre_send_hook": None,
    }
    kwargs.update(overrides)
    return await _default_place_order_fn(**kwargs)


@pytest.mark.asyncio
async def test_preview_sends_and_writes_nothing(monkeypatch):
    recorder, signal_recorder, stub = _wire(monkeypatch)

    preview = await _call(dry_run=True)

    assert preview["success"] is True
    assert preview["preview"] is True
    assert (preview["price"], preview["quantity"]) == (70000, 3)
    assert stub.posts == []
    assert recorder.events == []
    assert signal_recorder.events == []


@pytest.mark.asyncio
async def test_submit_runs_the_shared_sequence_then_maps_resting(monkeypatch):
    recorder, signal_recorder, stub = _wire(monkeypatch)

    submit = await _call()

    # attribution -> reservation -> POST -> ledger, in that order.
    assert signal_recorder.events[:1] == ["signal"]
    assert recorder.events == ["reserve", "post", "record:accepted"]
    assert len(stub.posts) == 1
    assert stub.posts[0]["method"] == "buy"
    assert signal_recorder.rows[0]["strategy"] == "unit-test"
    assert signal_recorder.rows[0]["correlation_id"] == "live:kiwoom_mock:testcorr"

    assert submit["success"] is True
    assert submit["status"] == "resting"
    assert submit["broker_order_id"] == "0000111222"
    assert submit["correlation_id"] == "live:kiwoom_mock:testcorr"
    assert submit["idempotency_key"] == recorder.rows[0]["client_order_id"]
    assert submit["ledger_status"] == "accepted"


@pytest.mark.asyncio
async def test_missing_strategy_is_attribution_required_and_sends_nothing(monkeypatch):
    recorder, signal_recorder, stub = _wire(monkeypatch)

    submit = await _call(strategy=None)

    assert submit["success"] is False
    assert submit["error"] == "attribution_required"
    assert stub.posts == []
    assert recorder.events == []
    assert signal_recorder.events == []


@pytest.mark.asyncio
async def test_config_gate_refuses_before_any_broker_call(monkeypatch):
    from app.services import kiwoom_mock_place

    recorder, signal_recorder, stub = _wire(monkeypatch)
    monkeypatch.setattr(
        kiwoom_mock_place,
        "mock_config_error",
        lambda: {
            "success": False,
            "error": "Kiwoom mock account is disabled",
            "source": "kiwoom",
            "account_mode": "kiwoom_mock",
        },
    )

    submit = await _call()

    assert submit["success"] is False
    assert submit["error"] == "Kiwoom mock account is disabled"
    assert stub.posts == []
    assert recorder.events == []
    assert signal_recorder.events == []


@pytest.mark.asyncio
async def test_duplicate_dispatch_reserves_once_and_sends_once(monkeypatch):
    recorder, _signal_recorder, stub = _wire(monkeypatch)

    first = await _call()
    second = await _call()

    assert first["status"] == "resting"
    assert len(stub.posts) == 1
    assert recorder.reserved and list(recorder.reserved.values()) == [1]
    # Second dispatch: the reservation refuses before any broker call, and a
    # key whose earlier outcome is not proven rejected is never terminalized.
    assert second["status"] == "not_submitted"
    assert "success" not in second
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_uncertain_acceptance_is_not_terminal(monkeypatch):
    recorder, _signal_recorder, stub = _wire(
        monkeypatch, behaviour=[{"return_code": 0, "return_msg": "정상"}]
    )

    submit = await _call()

    assert len(stub.posts) == 1
    assert recorder.events == ["reserve", "post", "record:uncertain"]
    assert "success" not in submit
    assert submit["status"] == "acceptance_uncertain"
    assert submit["reconcile_required"] is True


@pytest.mark.asyncio
async def test_broker_rejection_is_an_explicit_rejection(monkeypatch):
    recorder, _signal_recorder, stub = _wire(
        monkeypatch, behaviour=[{"return_code": 20, "return_msg": "주문가능금액 부족"}]
    )

    submit = await _call()

    assert len(stub.posts) == 1
    assert recorder.rows[0]["status"] == "rejected"
    assert submit["success"] is False


@pytest.mark.asyncio
async def test_pre_send_hook_block_releases_the_reservation_and_propagates(
    monkeypatch,
):
    from app.services.brokers.kis.pre_send import PreSendFreshnessError

    recorder, _signal_recorder, stub = _wire(monkeypatch)

    async def blocked_hook():
        raise PreSendFreshnessError(("approval_window:defer_session_closed",))

    with pytest.raises(PreSendFreshnessError):
        await _call(pre_send_hook=blocked_hook)

    # The order was provably not sent: no POST, reservation released.
    assert stub.posts == []
    assert recorder.events == ["reserve", "release"]
    assert recorder.reserved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"market": "equity_us"}, "kiwoom_mock_market_unsupported"),
        ({"order_type": "market"}, "kiwoom_mock_order_type_unsupported"),
        ({"quantity": Decimal("3.5")}, "kiwoom_mock_quantity_price_invalid"),
        ({"price": Decimal("0")}, "kiwoom_mock_quantity_price_invalid"),
    ],
)
async def test_shape_refusals_happen_before_any_send(
    monkeypatch, overrides, error_code
):
    recorder, signal_recorder, stub = _wire(monkeypatch)

    submit = await _call(**overrides)

    assert submit["success"] is False
    assert submit["error_code"] == error_code
    assert stub.posts == []
    assert recorder.events == []
    assert signal_recorder.events == []


@pytest.mark.asyncio
async def test_preflight_refusal_is_a_terminal_rejection(monkeypatch):
    """hk 4732 r1: a refused preflight is not sent, so it must not stay open.

    The preflight is a read-only account/quote check that runs before the send
    reservation; a refusal there must reach ``_classify_submit`` as an explicit
    rejection (releasing the buying-power reservation) rather than as an
    ambiguous ``unverified`` send.
    """
    from app.services import kiwoom_mock_place
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    recorder, signal_recorder, stub = _wire(monkeypatch)

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="cash 1 < required 210000",
            checks=[],
        )

    monkeypatch.setattr(
        kiwoom_mock_place, "run_kiwoom_mock_preflight", failing_preflight
    )

    submit = await _call()

    # Nothing was reserved and nothing was sent.
    assert stub.posts == []
    assert recorder.events == []
    # Closed vocabulary: provably not sent, so the rung becomes rejected.
    assert submit["status"] == "not_submitted"
    assert submit["success"] is False
    assert submit["error"] == PREFLIGHT_CASH_INSUFFICIENT
    # The durable signal row says not-sent instead of "outcome unknown".
    assert signal_recorder.events == ["signal", "signal_not_sent:preflight_refused"]


def _failing_preflight(mode: str):
    """A preflight that refuses the read: result-based or exception-based."""
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    if mode == "result":

        async def _refuse(**_kwargs):
            return PreflightResult(
                ok=False,
                error_code=PREFLIGHT_CASH_INSUFFICIENT,
                error_detail="cash 1 < required 210000",
                checks=[],
            )

        return _refuse

    if mode == "pre_dispatch":
        from app.services.brokers.kiwoom.client import KiwoomPreDispatchError

        async def _refuse_pre_dispatch(**_kwargs):
            raise KiwoomPreDispatchError(
                stage="token_resolution", api_id="kt00018", cause_type="RuntimeError"
            )

        return _refuse_pre_dispatch

    async def _raise(**_kwargs):
        raise RuntimeError("balance read failed")

    return _raise


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["result", "exception", "pre_dispatch"])
async def test_replayed_preflight_refusal_keeps_the_ambiguous_shape(monkeypatch, mode):
    """hk 4732 r2: only the attempt that owns the identity may say "not sent".

    On a replayed dispatch the signal row already exists, so an earlier attempt
    under this correlation id may have placed a live order. A refusal must not
    claim not_submitted/reconcile_required=false then, or the proposal rung is
    terminalized (and its buying-power reservation released) while that order
    is still at the broker.
    """
    from app.services import kiwoom_mock_place

    recorder, signal_recorder, stub = _wire(monkeypatch)
    monkeypatch.setattr(
        kiwoom_mock_place, "run_kiwoom_mock_preflight", _failing_preflight(mode)
    )

    first = await _call()
    assert first["status"] == "not_submitted"
    assert first["success"] is False

    second = await _call()

    # Replay: the closed not-sent markers are gone, so the adapter keeps the
    # response ambiguous (success dropped -> unverified) instead of rejecting.
    assert "status" not in second
    assert "success" not in second
    assert "reconcile_required" not in second
    assert stub.posts == []
    assert recorder.events == []
    # The replayed row is never relabelled: one not-sent mark, from the owner.
    assert signal_recorder.events.count("signal_not_sent:preflight_refused") == 1


def test_not_submitted_builder_drops_the_claim_when_it_does_not_own_the_identity():
    """hk 4732 r3: the not-sent claim is a parameter, not a builder default.

    Every refusal that builds through this helper must decide explicitly; a
    replayed identity keeps the diagnostics and loses the claim.
    """
    from app.services.brokers.kiwoom.client import KiwoomPreDispatchError
    from app.services.kiwoom_mock_place import not_submitted_response

    exc = KiwoomPreDispatchError(
        stage="token_resolution", api_id="kt00018", cause_type="RuntimeError"
    )
    base = {"source": "kiwoom", "account_mode": "kiwoom_mock"}

    owned = not_submitted_response(base, exc)
    replayed = not_submitted_response(base, exc, not_sent=False)

    for key in ("status", "dispatch_started", "reconcile_required"):
        assert key in owned
        assert key not in replayed
    # Diagnostics survive either way; no secret is read from the exception.
    for response in (owned, replayed):
        assert response["stage"] == "token_resolution"
        assert response["api_id"] == "kt00018"
        assert response["cause_type"] == "RuntimeError"
        assert response["success"] is False
