import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

import app.mcp_server.tooling.kis_mock_ledger as kis_mock_ledger
import app.mcp_server.tooling.order_execution as oe
from app.models.review import OrderSendIntent
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_order_send_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()
    yield
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


@pytest.fixture
def _coordinated_route():
    """ROB-1263 r4 §3: a KIS mock send needs a coordinated route to be authorized.

    These tests are about what happens to the *reservation* around a send, so the
    send has to be allowed to occur. Installing a route is the caller doing what
    the adapter now requires; the route-less refusal itself is unaffected and is
    covered by `test_without_a_route_the_lane_sends_nothing_at_all`.
    """
    import app.services.kis_mock_runner.singleton as singleton
    from app.services.mock_integration.coordination import DurableSendClaimAdapter
    from tests.services.mock_integration.test_coordination import (
        ConnectionFactory,
        FakeIntents,
        FakeLockConnection,
        FakeLockSpace,
        FakeUncertaintyGate,
        RecordingDispatchEvidence,
        RecordingPersistence,
        _attempt_envelope,
        _bound_registry,
    )

    physical = singleton.kis_mock_account_fingerprint(
        app_key="rob750-fixture-key", account_no="5088888801"
    )
    _, envelope = _attempt_envelope()
    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    route = singleton.KISMockCoordinationRoute(
        envelope=envelope,
        ports=singleton.KISMockLanePorts(
            persistence=RecordingPersistence(),
            dispatch_evidence=RecordingDispatchEvidence(),
            uncertainty_gate=FakeUncertaintyGate(),
            evidence_kinds=singleton.KIS_MOCK_LANE_EVIDENCE_KINDS,
        ),
        claims=DurableSendClaimAdapter(FakeIntents()),
        connection_factory=ConnectionFactory(connection),
        registry=_bound_registry(envelope, "kr.kis.mock", physical_account_id=physical),
    )
    singleton.set_kis_mock_coordination_route_provider(lambda **_ctx: route)
    try:
        yield route
    finally:
        singleton.set_kis_mock_coordination_route_provider(None)


def _execute_kwargs(*, key: str, is_mock: bool) -> dict:
    return {
        "normalized_symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "order_quantity": 2,
        "price": 70000,
        "market_type": "equity_kr",
        "current_price": 70000,
        "avg_price": 0.0,
        "dry_run_result": {
            "price": 70000,
            "quantity": 2,
            "estimated_value": 140000,
        },
        "order_amount": 140000,
        "reason": "ROB-750 mirror retry regression",
        "exit_reason": None,
        "thesis": "counterfactual mirror",
        "strategy": "mirror_counterfactual",
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": "source_bucket=place_original",
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": is_mock,
        "correlation_id": key if is_mock else None,
        "report_item_uuid": None,
        "approval_hash_digest": None,
        "idempotency_key": None if is_mock else key,
        "mirror_cohort": "mock_counterfactual" if is_mock else None,
        "mirror_source_bucket": "place_original" if is_mock else None,
    }


def _stub_kis_mock_baseline(monkeypatch):
    async def fake_baseline_qty(**kwargs):
        return None

    monkeypatch.setattr(
        kis_mock_ledger,
        "_fetch_kis_mock_baseline_qty",
        fake_baseline_qty,
    )


@pytest.mark.asyncio
async def test_mock_mirror_intent_is_retained_when_the_send_is_not_proven_unsent(
    monkeypatch,
    db_session,
    _coordinated_route,
):
    """ROB-1263 B-5 supersedes ROB-750's mirror-retry exemption.

    ROB-750 released the mirror reservation on any transport error and told the
    caller to retry, on the theory that mock money carries no risk. The risk is
    not the money: it is a duplicate order at the broker and a lineage that can
    no longer say which send produced it. Without proof the send never left, the
    outcome is unknown, so the claim is retained and no retry is advertised.
    """
    _stub_kis_mock_baseline(monkeypatch)
    key = "mirror:rob750-request-error"

    async def fail_send(**kwargs):
        raise httpx.ConnectError("temporary mock broker outage")

    monkeypatch.setattr(oe, "_execute_order", fail_send)

    with pytest.raises(oe.OrderSendOutcomeUnknown) as excinfo:
        await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=True))

    assert getattr(excinfo.value, "retry_allowed", False) is False
    with pytest.raises(DuplicateOrderIntent):
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_mock",
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_live_intent_is_not_released_after_unknown_send_outcome(
    monkeypatch,
    db_session,
):
    key = "rob750-live-unknown"

    async def fail_send(**kwargs):
        raise httpx.ReadTimeout("live outcome unknown")

    monkeypatch.setattr(oe, "_execute_order", fail_send)

    with pytest.raises(oe.OrderSendOutcomeUnknown) as excinfo:
        await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=False))

    assert getattr(excinfo.value, "retry_allowed", False) is False
    with pytest.raises(DuplicateOrderIntent):
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_live",
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_mock_mirror_duplicate_message_does_not_claim_next_day_retry(
    monkeypatch,
    db_session,
    _coordinated_route,
):
    _stub_kis_mock_baseline(monkeypatch)
    key = "mirror:rob750-duplicate-message"
    sent = {"count": 0}

    await OrderSendIntentService(db_session).reserve(
        account_scope="kis_mock",
        idempotency_key=key,
    )

    async def fake_send(**kwargs):
        sent["count"] += 1
        return {"odno": "SHOULD-NOT-SEND", "rt_cd": "0", "msg": "ok"}

    monkeypatch.setattr(oe, "_execute_order", fake_send)

    result = await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=True))

    assert result["success"] is False
    assert "미러" in result["error"]
    assert "익일" not in result["error"]
    assert sent["count"] == 0


@pytest.mark.asyncio
async def test_mock_mirror_unknown_outcome_message_never_advertises_a_retry(
    monkeypatch, _coordinated_route
):
    """The operator-facing message must not invite a re-send of an unknown POST."""
    _stub_kis_mock_baseline(monkeypatch)
    key = "mirror:rob750-retry-message"

    async def fail_send(**kwargs):
        raise httpx.ConnectError("temporary mock broker outage")

    monkeypatch.setattr(oe, "_execute_order", fail_send)

    with pytest.raises(oe.OrderSendOutcomeUnknown) as excinfo:
        await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=True))

    result = oe._augment_error_for_unknown_outcome(
        {
            "success": False,
            "error": "ConnectError",
            "source": "kis",
            "symbol": "005930",
            "instrument_type": "equity_kr",
        },
        excinfo.value,
        market_type="equity_kr",
        is_mock=True,
    )

    assert result["outcome_unknown"] is True
    assert result.get("retry_allowed", False) is False
