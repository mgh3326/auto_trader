"""Order-link observations use fake sessions and never contact a database."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import decision_evidence as de

_real_observation_session = de.observation_session


@pytest.fixture(autouse=True)
def forbid_unmocked_order_databases(monkeypatch):
    from app.mcp_server.tooling import order_execution

    def forbidden():
        raise AssertionError("unit test attempted an unmocked database session")

    monkeypatch.setattr(order_execution, "_order_session_factory", forbidden)
    monkeypatch.setattr(de, "observation_session", forbidden)


class Store:
    def __init__(self):
        self.rows = []
        self.events = []
        self.fail = None

    def session(self):
        return Session(self)


class Session:
    def __init__(self, store):
        self.store = store
        self.pending = []

    async def __aenter__(self):
        self.store.events.append("open")
        return self

    async def __aexit__(self, *args):
        self.store.events.append("close")

    async def execute(self, sql, values=None):
        if str(sql).startswith("SELECT set_config"):
            return
        table = str(sql).split("INSERT INTO review.")[1].split(" ")[0]
        if self.store.fail == table:
            raise RuntimeError("sensitive exception must not be logged")
        self.pending.append((table, dict(values)))

    async def commit(self):
        self.store.events.append("commit")
        self.store.rows.extend(self.pending)


def rows(store, table):
    return [value for name, value in store.rows if name == table]


async def drain_observations():
    import asyncio

    while pending := list(de._observation_tasks):
        await asyncio.gather(*pending)
        await asyncio.sleep(0)


async def begin(store, decision_id=None, proposal_id=None):
    return await de.begin_order_attempt(
        store.session,
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        side="buy",
        strategy="swing",
        send_key="same",
        decision_id=decision_id,
        proposal_id=proposal_id,
        rung_index=0 if proposal_id else None,
        proposal_flow=proposal_id is not None,
    )


@pytest.mark.asyncio
async def test_direct_attempt_is_committed_in_own_session_and_outcome_is_separate():
    store = Store()
    attempt = await begin(store)
    assert len(rows(store, "decision_evidence")) == 1
    assert len(rows(store, "decision_order_links")) == 1
    assert store.events == ["open", "commit", "close"]
    await de.finish_order_attempt(store.session, attempt, "sent")
    assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"
    assert store.events == ["open", "commit", "close", "open", "commit", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["decision_evidence", "decision_order_links"])
async def test_begin_failure_is_fail_open_and_redacts_error(failure, caplog):
    store = Store()
    store.fail = failure
    assert await begin(store) is None
    assert not store.rows
    assert "sensitive" not in caplog.text
    assert "reason=" in caplog.text


@pytest.mark.asyncio
async def test_outcome_failure_is_fail_open_and_redacted(caplog):
    store = Store()
    attempt = await begin(store)
    store.fail = "decision_order_link_outcomes"
    await de.finish_order_attempt(store.session, attempt, "sent")
    assert not rows(store, "decision_order_link_outcomes")
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_hook_preserves_arguments_return_and_exception_with_retry_count():
    calls = []
    expected = object()
    error = ValueError("same object")

    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 2:
            raise error
        return expected

    hook = de.AttemptSendHook(original)
    assert await hook(1, name="value") is expected
    with pytest.raises(ValueError) as caught:
        await hook(2, name="value")
    assert caught.value is error
    assert calls == [((1,), {"name": "value"}), ((2,), {"name": "value"})]
    assert hook.post_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome_lost,link_lost,expected",
    [
        (False, False, "recorded"),
        (True, False, "ambiguous_attempts"),
        (True, True, "attempt_unconfirmed"),
    ],
)
async def test_two_decisions_first_refused_then_second_sent(
    outcome_lost, link_lost, expected
):
    store = Store()
    da, db, pa, pb = uuid4(), uuid4(), uuid4(), uuid4()
    a = await begin(store, da, pa)
    if outcome_lost:
        store.fail = "decision_order_link_outcomes"
    await de.finish_order_attempt(store.session, a, "proven_not_sent")
    store.fail = "decision_order_links" if link_lost else None
    b = await begin(store, db, pb)
    store.fail = None
    if b:
        await de.finish_order_attempt(store.session, b, "sent")
    outcomes = {
        r["attempt_id"]: r["outcome"]
        for r in rows(store, "decision_order_link_outcomes")
    }
    live = [
        r
        for r in rows(store, "decision_order_links")
        if outcomes.get(r["attempt_id"]) != "proven_not_sent"
    ]
    decisions = {r["decision_id"] for r in live}
    result = (
        "ambiguous_attempts"
        if len(decisions) > 1
        else (
            "recorded"
            if any(outcomes.get(r["attempt_id"]) == "sent" for r in live)
            else "attempt_unconfirmed"
        )
    )
    assert result == expected
    if expected == "recorded":
        assert decisions == {db}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "decision_evidence",
        "decision_order_links",
        "decision_order_link_outcomes",
        "setup",
        "schedule",
    ],
)
async def test_toss_direct_write_failures_preserve_broker_payload_and_response(
    monkeypatch, failure
):
    import json

    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from tests.test_mcp_toss_order_variants import MockTossClient

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(
        toss,
        "record_toss_place_order",
        AsyncMock(return_value={"ledger_id": 1, "broker_status": "accepted"}),
    )
    monkeypatch.setattr(toss, "record_buy_exit_plan", AsyncMock())

    original_begin = de.begin_order_attempt
    original_schedule = de.schedule_order_attempt_outcome

    async def run(fail):
        monkeypatch.setattr(
            de,
            "begin_order_attempt",
            AsyncMock(return_value=None)
            if fail == "no-link-baseline"
            else AsyncMock(side_effect=RuntimeError("synthetic setup failure"))
            if fail == "setup"
            else original_begin,
        )
        monkeypatch.setattr(
            de,
            "schedule_order_attempt_outcome",
            MagicMock(side_effect=RuntimeError("synthetic schedule failure"))
            if fail == "schedule"
            else original_schedule,
        )
        store = Store()
        store.fail = fail
        monkeypatch.setattr(de, "observation_session", store.session)
        client = MockTossClient(monkeypatch)
        result = await toss._toss_place_order_impl(
            symbol="AAPL",
            side="buy",
            quantity="2",
            price="190",
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            thesis="entry",
            strategy="swing",
            client_order_id_override="same-key",
        )
        await drain_observations()
        return (
            json.dumps(result, sort_keys=True, default=str),
            client.placed_payloads,
            store,
        )

    baseline, baseline_calls, _ = await run("no-link-baseline")
    result, calls, store = await run(failure)
    assert result == baseline
    assert calls == baseline_calls
    assert len(calls) == 1
    if failure is None:
        assert len(rows(store, "decision_evidence")) == 1
        assert len(rows(store, "decision_order_links")) == 1
        assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "decision_evidence",
        "decision_order_links",
        "decision_order_link_outcomes",
        "setup",
        "schedule",
    ],
)
async def test_upbit_write_failures_preserve_broker_arguments_and_return(
    monkeypatch, failure
):
    import json
    from types import SimpleNamespace

    from app.core.config import settings
    from app.mcp_server.tooling import live_order_ledger
    from app.mcp_server.tooling import order_execution as order

    monkeypatch.setattr(settings, "order_approval_hash_mode", "off")
    monkeypatch.setattr(
        order,
        "_get_crypto_trade_cooldown_service",
        lambda: SimpleNamespace(is_in_cooldown=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(order, "_fetch_current_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(
        order,
        "_build_preview",
        AsyncMock(
            return_value={
                "price": 100.0,
                "quantity": 1.0,
                "estimated_value": 100.0,
                "market": "KRW-BTC",
            }
        ),
    )
    monkeypatch.setattr(
        order, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order, "evaluate_sector_concentration", AsyncMock(return_value={})
    )
    monkeypatch.setattr(order, "_record_order_history", AsyncMock())
    monkeypatch.setattr(order, "record_buy_exit_plan", AsyncMock())
    monkeypatch.setattr(
        live_order_ledger,
        "_record_live_order",
        AsyncMock(
            return_value={
                "success": True,
                "broker_status": "accepted",
                "order_id": "broker-1",
            }
        ),
    )

    original_begin = de.begin_order_attempt
    original_schedule = de.schedule_order_attempt_outcome

    async def run(fail):
        monkeypatch.setattr(
            de,
            "begin_order_attempt",
            AsyncMock(return_value=None)
            if fail == "no-link-baseline"
            else AsyncMock(side_effect=RuntimeError("synthetic setup failure"))
            if fail == "setup"
            else original_begin,
        )
        monkeypatch.setattr(
            de,
            "schedule_order_attempt_outcome",
            MagicMock(side_effect=RuntimeError("synthetic schedule failure"))
            if fail == "schedule"
            else original_schedule,
        )
        store = Store()
        store.fail = fail
        monkeypatch.setattr(order, "_order_session_factory", lambda: store.session)
        monkeypatch.setattr(de, "observation_session", store.session)
        calls = []

        async def broker(**kwargs):
            hook = kwargs.pop("pre_send_hook")
            if hook is not None:
                await hook()
            calls.append(kwargs)
            return {"uuid": "broker-1", "market": "KRW-BTC"}

        monkeypatch.setattr(order, "_execute_order", broker)
        result = await order._place_order_impl(
            symbol="KRW-BTC",
            side="buy",
            market="crypto",
            quantity=1.0,
            price=100.0,
            dry_run=False,
            thesis="entry",
            strategy="swing",
            client_order_id="same-key",
        )
        await drain_observations()
        return json.dumps(result, sort_keys=True, default=str), calls, store

    baseline, baseline_calls, _ = await run("no-link-baseline")
    result, calls, store = await run(failure)
    assert result == baseline
    assert calls == baseline_calls
    assert len(calls) == 1
    if failure is None:
        assert len(rows(store, "decision_evidence")) == 1
        assert len(rows(store, "decision_order_links")) == 1
        assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "decision_evidence",
        "decision_order_links",
        "decision_order_link_outcomes",
        "setup",
        "schedule",
    ],
)
async def test_kis_write_failures_preserve_broker_arguments_and_return(
    monkeypatch, failure
):
    import json

    from app.core.config import settings
    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import order_execution as order

    monkeypatch.setattr(settings, "order_approval_hash_mode", "off")
    monkeypatch.setattr(order, "_fetch_current_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(
        order,
        "_build_preview",
        AsyncMock(
            return_value={"price": 100.0, "quantity": 1.0, "estimated_value": 100.0}
        ),
    )
    monkeypatch.setattr(
        order, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order, "evaluate_sector_concentration", AsyncMock(return_value={})
    )
    monkeypatch.setattr(order, "_record_order_history", AsyncMock())
    monkeypatch.setattr(order, "record_buy_exit_plan", AsyncMock())
    monkeypatch.setattr(
        kis_live_ledger,
        "_record_kis_live_order",
        AsyncMock(
            return_value={
                "success": True,
                "broker_status": "accepted",
                "order_id": "broker-1",
            }
        ),
    )
    monkeypatch.setattr(
        order,
        "OrderSendIntentService",
        lambda _: type("Reservation", (), {"reserve": AsyncMock(return_value=1)})(),
    )

    original_begin = de.begin_order_attempt
    original_schedule = de.schedule_order_attempt_outcome

    async def run(fail):
        monkeypatch.setattr(
            de,
            "begin_order_attempt",
            AsyncMock(return_value=None)
            if fail == "no-link-baseline"
            else AsyncMock(side_effect=RuntimeError("synthetic setup failure"))
            if fail == "setup"
            else original_begin,
        )
        monkeypatch.setattr(
            de,
            "schedule_order_attempt_outcome",
            MagicMock(side_effect=RuntimeError("synthetic schedule failure"))
            if fail == "schedule"
            else original_schedule,
        )
        store = Store()
        store.fail = fail
        monkeypatch.setattr(order, "_order_session_factory", lambda: store.session)
        monkeypatch.setattr(de, "observation_session", store.session)
        calls = []

        async def broker(**kwargs):
            hook = kwargs.pop("pre_send_hook")
            if hook is not None:
                await hook()
            calls.append(kwargs)
            return {"odno": "broker-1", "rt_cd": "0"}

        monkeypatch.setattr(order, "_execute_order", broker)
        result = await order._place_order_impl(
            symbol="005930",
            side="buy",
            market="kr",
            quantity=1.0,
            price=100.0,
            dry_run=False,
            thesis="entry",
            strategy="swing",
            client_order_id="same-key",
        )
        await drain_observations()
        return json.dumps(result, sort_keys=True, default=str), calls, store

    baseline, baseline_calls, _ = await run("no-link-baseline")
    result, calls, store = await run(failure)
    assert result == baseline
    assert calls == baseline_calls
    assert len(calls) == 1
    if failure is None:
        assert len(rows(store, "decision_evidence")) == 1
        assert len(rows(store, "decision_order_links")) == 1
        assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome_lost,link_lost,expected",
    [
        (False, False, "recorded"),
        (True, False, "ambiguous_attempts"),
        (True, True, "attempt_unconfirmed"),
    ],
)
async def test_toss_proposals_refused_then_sent_with_failures(
    monkeypatch, outcome_lost, link_lost, expected
):
    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from tests.test_mcp_toss_order_variants import MockTossClient

    store = Store()
    monkeypatch.setattr(de, "observation_session", store.session)
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(
        toss,
        "record_toss_place_order",
        AsyncMock(return_value={"broker_status": "accepted"}),
    )
    client = MockTossClient(monkeypatch)
    da, db, pa, pb = uuid4(), uuid4(), uuid4(), uuid4()

    async def blocked():
        raise PreSendFreshnessError(("stale",))

    def context(proposal, decision):
        return toss._bind_order_proposal_context(
            client_order_id="same-key",
            correlation_id="same",
            rung=0,
            proposal_id=proposal,
            rung_index=0,
            decision_id=decision,
        )

    args = {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "2",
        "price": "190",
        "market": "us",
        "dry_run": False,
        "confirm": True,
        "account_mode": "toss_live",
        "thesis": "entry",
        "strategy": "swing",
        "client_order_id_override": "same-key",
    }
    store.fail = "decision_order_link_outcomes" if outcome_lost else None
    with (
        context(pa, da),
        toss._bind_toss_pre_send_hook(blocked),
        pytest.raises(PreSendFreshnessError),
    ):
        await toss._toss_place_order_impl(**args)
    store.fail = "decision_order_links" if link_lost else None
    with context(pb, db):
        result = await toss._toss_place_order_impl(**args)
    assert result["success"] is True
    assert len(client.placed_payloads) == 1
    await drain_observations()
    outcome = {
        r["attempt_id"]: r["outcome"]
        for r in rows(store, "decision_order_link_outcomes")
    }
    live = [
        r
        for r in rows(store, "decision_order_links")
        if outcome.get(r["attempt_id"]) != "proven_not_sent"
    ]
    decisions = {r["decision_id"] for r in live}
    classification = (
        "ambiguous_attempts"
        if len(decisions) > 1
        else (
            "recorded"
            if any(outcome.get(r["attempt_id"]) == "sent" for r in live)
            else "attempt_unconfirmed"
        )
    )
    assert classification == expected
    if expected == "recorded":
        assert decisions == {db}


@pytest.mark.asyncio
async def test_toss_freshness_failure_after_prior_post_does_not_mark_not_sent(
    monkeypatch,
):
    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from tests.test_mcp_toss_order_variants import MockTossClient

    store = Store()
    monkeypatch.setattr(de, "observation_session", store.session)
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    client = MockTossClient(monkeypatch)
    calls = []

    async def gate():
        calls.append("gate")
        if len(calls) == 2:
            raise PreSendFreshnessError(("stale",))

    async def retrying(payload, *, pre_send_hook):
        await pre_send_hook()
        client.placed_payloads.append(payload)
        await pre_send_hook()

    client.place_order = retrying
    with toss._bind_toss_pre_send_hook(gate), pytest.raises(PreSendFreshnessError):
        await toss._toss_place_order_impl(
            symbol="AAPL",
            side="buy",
            quantity="2",
            price="190",
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            thesis="entry",
            strategy="swing",
            client_order_id_override="same-key",
        )
    assert calls == ["gate", "gate"]
    assert len(client.placed_payloads) == 1
    assert len(rows(store, "decision_order_links")) == 1
    assert rows(store, "decision_order_link_outcomes") == []


@pytest.mark.asyncio
async def test_revalidation_binds_proposal_identity_without_changing_submit_arguments():
    from datetime import UTC, datetime, timedelta

    from app.mcp_server.tooling.orders_toss_variants import _order_proposal_context
    from app.services.order_proposals.revalidation import revalidate_and_submit
    from tests.services.order_proposals.test_approval_window import (
        _allowed_window_evaluator,
        _FakeRevalidationService,
    )

    now = datetime(2026, 7, 22, 14, tzinfo=UTC)
    service = _FakeRevalidationService(valid_until=now + timedelta(hours=1))
    service.group.decision_id = uuid4()
    seen = []

    async def place(**kwargs):
        if kwargs["dry_run"]:
            assert _order_proposal_context.get() is None
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        context = _order_proposal_context.get()
        seen.append(
            (
                context.proposal_id,
                context.rung_index,
                context.decision_id,
                kwargs["rung"],
                kwargs["correlation_id"],
            )
        )
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "broker-1",
            "correlation_id": "corr-1",
            "idempotency_key": "idem-1",
            "approval_hash_digest": "digest-1",
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=place,
        correlation_mint=lambda **kwargs: "corr-1",
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
        expected_policy_stamp=(
            await _allowed_window_evaluator(service.group, now=now)
        ).policy_stamp,
    )
    assert seen == [
        (service.group.proposal_id, 0, service.group.decision_id, 0, "corr-1")
    ]
    assert [o.result for o in outcomes] == ["submitted_resting"]
    assert _order_proposal_context.get() is None


@pytest.mark.asyncio
async def test_kis_mock_order_does_not_create_live_evidence(monkeypatch):
    from app.core.config import settings
    from app.mcp_server.tooling import order_execution as order

    begin = AsyncMock(side_effect=AssertionError("mock must not write evidence"))
    monkeypatch.setattr(de, "begin_order_attempt", begin)
    monkeypatch.setattr(settings, "order_approval_hash_mode", "off")
    monkeypatch.setattr(order, "_fetch_current_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(
        order,
        "_build_preview",
        AsyncMock(
            return_value={"price": 100.0, "quantity": 1.0, "estimated_value": 100.0}
        ),
    )
    monkeypatch.setattr(
        order, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order, "evaluate_sector_concentration", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        order, "_execute_and_record", AsyncMock(return_value={"success": True})
    )
    result = await order._place_order_impl(
        symbol="005930",
        side="buy",
        market="kr",
        quantity=1.0,
        price=100.0,
        dry_run=False,
        is_mock=True,
        strategy="swing",
    )
    assert result["success"] is True
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_keeps_background_write_and_logs_late_exception(
    monkeypatch, caplog
):
    import asyncio

    release = asyncio.Event()
    monkeypatch.setattr(de, "_OBSERVATION_TIMEOUT", 0.01)

    class SlowSession(Session):
        async def execute(self, sql, values=None):
            await release.wait()
            raise RuntimeError("sensitive late failure")

    store = Store()
    attempt = await de.begin_order_attempt(
        lambda: SlowSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        side="buy",
        strategy="swing",
        send_key="key",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    assert attempt is not None
    assert "reason=attempt_timeout" in caplog.text
    pending = list(de._observation_tasks)
    release.set()
    await asyncio.gather(*pending)
    assert "reason=decision_write_failed" in caplog.text
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_kis_legacy_client_receives_original_arguments_and_hook_runs_once():
    from app.mcp_server.tooling.order_execution import _call_kis

    calls = []

    async def original_hook():
        calls.append("hook")

    async def client(*, symbol):
        calls.append(("post", symbol))
        return {"odno": "1"}

    hook = de.AttemptSendHook(original_hook)
    result = await _call_kis(client, symbol="005930", is_mock=False, pre_send_hook=hook)
    assert result == {"odno": "1"}
    assert calls == ["hook", ("post", "005930")]
    assert hook.post_count == 1


@pytest.mark.asyncio
async def test_replace_binds_original_decision_after_confirmed_cancel():
    from datetime import UTC, datetime, timedelta

    from app.mcp_server.tooling.orders_toss_variants import _order_proposal_context
    from app.services.order_proposals.revalidation import revalidate_and_submit
    from tests.services.order_proposals.test_approval_window import (
        TargetOrderSnapshot,
        _allowed_window_evaluator,
        _FakeRevalidationService,
    )

    now = datetime(2026, 7, 22, 14, tzinfo=UTC)
    service = _FakeRevalidationService(
        action="replace", valid_until=now + timedelta(days=2)
    )
    service.group.decision_id = uuid4()

    def target(status):
        return TargetOrderSnapshot(
            broker_order_id="target-1",
            symbol="VOO",
            side="sell",
            order_type="limit",
            limit_price="100",
            remaining_quantity="1",
            status=status,
            observed_at=now.isoformat(),
        )

    service.group.source_asof = {"target_order_snapshot": target("open").to_payload()}
    snapshots = iter([target("open"), target("cancelled")])
    seen = []

    async def place(**kwargs):
        if kwargs["dry_run"]:
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        context = _order_proposal_context.get()
        seen.append((context.proposal_id, context.rung_index, context.decision_id))
        return {"success": True, "status": "resting", "broker_order_id": "new-1"}

    async def fetch(**kwargs):
        return next(snapshots)

    async def cancel(**kwargs):
        return {"success": True}

    initial = await _allowed_window_evaluator(service.group, now=now)
    service.group.source_asof["approval_window_policy_stamp"] = initial.policy_stamp
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=place,
        fetch_target_fn=fetch,
        cancel_target_fn=cancel,
        correlation_mint=lambda **kwargs: "corr-1",
        opposite_pending_check_fn=lambda **kwargs: None,
        window_evaluator=_allowed_window_evaluator,
        expected_policy_stamp=initial.policy_stamp,
        now_fn=lambda: now,
    )
    assert [o.result for o in outcomes] == ["submitted_resting"]
    assert seen == [(service.group.proposal_id, 0, service.group.decision_id)]


@pytest.mark.asyncio
async def test_toss_proceeds_before_slow_link_finishes_and_late_error_is_redacted(
    monkeypatch, caplog
):
    import asyncio
    import json

    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from tests.test_mcp_toss_order_variants import MockTossClient

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(
        toss,
        "record_toss_place_order",
        AsyncMock(return_value={"ledger_id": 1, "broker_status": "accepted"}),
    )
    monkeypatch.setattr(toss, "record_buy_exit_plan", AsyncMock())
    monkeypatch.setattr(de, "_OBSERVATION_TIMEOUT", 0.01)
    args = {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "2",
        "price": "190",
        "market": "us",
        "dry_run": False,
        "confirm": True,
        "account_mode": "toss_live",
        "thesis": "entry",
        "strategy": "swing",
        "client_order_id_override": "same-key",
    }
    baseline_store = Store()
    monkeypatch.setattr(de, "observation_session", baseline_store.session)
    baseline_client = MockTossClient(monkeypatch)
    baseline = await toss._toss_place_order_impl(**args)
    release = asyncio.Event()
    store = Store()

    class SlowSession(Session):
        async def execute(self, sql, values=None):
            if "review.decision_evidence " in str(sql):
                await release.wait()
                raise RuntimeError("late sensitive text")
            await super().execute(sql, values)

    monkeypatch.setattr(de, "observation_session", lambda: SlowSession(store))
    client = MockTossClient(monkeypatch)
    try:
        result = await asyncio.wait_for(
            toss._toss_place_order_impl(**args), timeout=0.5
        )
        assert client.placed_payloads == baseline_client.placed_payloads
        assert json.dumps(result, sort_keys=True, default=str) == json.dumps(
            baseline, sort_keys=True, default=str
        )
        assert len(client.placed_payloads) == 1
        assert "reason=attempt_timeout" in caplog.text
    finally:
        pending = list(de._observation_tasks)
        release.set()
        await asyncio.gather(*pending)
    assert "reason=decision_write_failed" in caplog.text
    assert "late sensitive text" not in caplog.text


@pytest.mark.asyncio
async def test_non_live_execution_surfaces_do_not_write_live_evidence(monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.mcp_server.tooling import alpaca_paper_orders as alpaca
    from app.mcp_server.tooling import orders_kiwoom_variants as kiwoom
    from app.mcp_server.tooling import paper_order_handler as paper
    from app.services.brokers.binance.spot_demo import execution_client as binance
    from app.services.nhplug_mock import operations as nh
    from tests.test_mcp_kiwoom_order_variants import (
        _patch_fake_kiwoom_order_client,
        _patch_preflight_success,
    )

    writer = AsyncMock(side_effect=AssertionError("non-live path wrote evidence"))
    monkeypatch.setattr(de, "begin_order_attempt", writer)
    cm = AsyncMock()
    cm.__aenter__.return_value = MagicMock()
    monkeypatch.setattr(paper, "AsyncSessionLocal", lambda: cm)
    paper_service = SimpleNamespace(
        execute_order=AsyncMock(
            return_value={
                "preview": {"symbol": "005930", "instrument_type": "equity_kr"},
                "execution": {},
            }
        )
    )
    monkeypatch.setattr(paper, "PaperTradingService", lambda _: paper_service)
    monkeypatch.setattr(
        paper,
        "_resolve_paper_account",
        AsyncMock(return_value=SimpleNamespace(id=1, name="paper")),
    )
    result = await paper._place_paper_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=100,
        amount=None,
        dry_run=False,
        reason="test",
    )
    assert result["success"] is True
    paper_service.execute_order.assert_awaited_once()

    calls = _patch_fake_kiwoom_order_client(
        monkeypatch, kiwoom, {"buy": {"return_code": 0, "ord_no": "1"}, "sell": {}}
    )
    _patch_preflight_success(monkeypatch, kiwoom)
    result = await kiwoom._kiwoom_mock_place_order_impl(
        symbol="005930", side="buy", quantity=1, price=100, dry_run=False
    )
    assert result["success"] is True
    assert any(call.get("method") == "buy" for call in calls)

    monkeypatch.setattr(nh, "_verified_session", AsyncMock(return_value=object()))
    monkeypatch.setattr(nh, "_account_ref", AsyncMock(return_value="fake-account"))
    dispatch = AsyncMock(return_value={"success": True, "sent": True})
    monkeypatch.setattr(nh, "_dispatch_new_intent", dispatch)
    result = await nh.place_order(
        symbol="005930",
        side="buy",
        quantity=1,
        price=100,
        order_type="limit",
        idempotency_key="1234567890123456",
        dry_run=False,
        confirm=True,
    )
    assert result["sent"] is True
    dispatch.assert_awaited_once()

    monkeypatch.setattr(alpaca, "_session_factory", lambda: lambda: cm)
    monkeypatch.setattr(
        alpaca, "load_market_evidence", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        alpaca, "_build_manual_packet", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        alpaca, "AlpacaPaperLedgerService", lambda *args, **kwargs: object()
    )
    submit = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            submitted=True,
            status="accepted",
            reason_code=None,
            order={},
            client_order_id="fake",
            message="ok",
        )
    )
    monkeypatch.setattr(
        alpaca,
        "AlpacaPaperSubmitCoordinator",
        lambda *args, **kwargs: SimpleNamespace(submit=submit),
    )
    result = await alpaca.alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        quote_snapshot_id=1,
        confirm=True,
    )
    assert result["submitted"] is True
    submit.assert_awaited_once()

    response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"orderId": "1", "origQty": "1"}
    )
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        binance, "build_spot_demo_client", lambda **kwargs: SimpleNamespace(post=post)
    )
    client = binance.BinanceSpotDemoExecutionClient(
        api_key="test-key", api_secret="test-secret"
    )
    await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("1"),
        client_order_id="test-id",
        confirm=True,
    )
    post.assert_awaited_once()
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_attempt_commit_precedes_deferred_sent_outcome(monkeypatch):
    import asyncio

    release = asyncio.Event()
    store = Store()
    monkeypatch.setattr(de, "_OBSERVATION_TIMEOUT", 0.01)

    class DelayedLinkSession(Session):
        async def execute(self, sql, values=None):
            if str(sql).startswith("SELECT set_config"):
                return
            table = str(sql).split("INSERT INTO review.")[1].split(" ")[0]
            if table == "decision_order_links":
                await release.wait()
            if table == "decision_order_link_outcomes" and not rows(
                store, "decision_order_links"
            ):
                raise RuntimeError("foreign key absent")
            await super().execute(sql, values)

    attempt = await de.begin_order_attempt(
        lambda: DelayedLinkSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        side="buy",
        strategy="swing",
        send_key="same",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    assert attempt is not None
    assert rows(store, "decision_order_links") == []
    await de.finish_order_attempt(lambda: DelayedLinkSession(store), attempt, "sent")
    release.set()
    await drain_observations()
    assert len(rows(store, "decision_order_links")) == 1
    assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


@pytest.mark.asyncio
async def test_stalled_observation_does_not_accumulate_connections(monkeypatch, caplog):
    import asyncio

    release = asyncio.Event()
    store = Store()
    monkeypatch.setattr(de, "_OBSERVATION_TIMEOUT", 0.01)
    monkeypatch.setattr(de, "_MAX_OBSERVATION_TASKS", 2)

    class HeldSession(Session):
        async def execute(self, sql, values=None):
            await release.wait()
            await super().execute(sql, values)

    first = await de.begin_order_attempt(
        lambda: HeldSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        side="buy",
        strategy=None,
        send_key="a",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    assert first is not None
    second = await de.begin_order_attempt(
        lambda: HeldSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005005",
        side="buy",
        strategy=None,
        send_key="b",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    assert second is not None
    third = await de.begin_order_attempt(
        lambda: HeldSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005006",
        side="buy",
        strategy=None,
        send_key="c",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    assert third is None
    assert store.events == ["open", "open"]
    assert "reason=writer_busy" in caplog.text
    pending = list(de._observation_tasks)
    release.set()
    await asyncio.gather(*pending)
    assert len(rows(store, "decision_order_links")) == 2


def test_observation_uses_dedicated_nonpooled_engine(monkeypatch):
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    monkeypatch.setattr(
        settings, "DATABASE_URL", "postgresql+asyncpg://unused.invalid/test"
    )
    monkeypatch.setattr(de, "_observation_sessions", None)
    engine = object()
    constructor = MagicMock(return_value=engine)
    factory = MagicMock(return_value="fake-session")
    maker = MagicMock(return_value=factory)
    monkeypatch.setattr(de, "create_async_engine", constructor)
    monkeypatch.setattr(de, "async_sessionmaker", maker)
    assert _real_observation_session() == "fake-session"
    constructor.assert_called_once_with(
        "postgresql+asyncpg://unused.invalid/test",
        poolclass=NullPool,
        connect_args={"timeout": 5.0, "command_timeout": 5.0},
    )
    maker.assert_called_once_with(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_toss_ledger_is_not_delayed_by_blocked_sent_observation(monkeypatch):
    import asyncio

    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from tests.test_mcp_toss_order_variants import MockTossClient

    release = asyncio.Event()
    outcome_started = asyncio.Event()
    store = Store()

    class BlockedOutcomeSession(Session):
        async def execute(self, sql, values=None):
            if "INSERT INTO review.decision_order_link_outcomes " in str(sql):
                outcome_started.set()
                await release.wait()
            await super().execute(sql, values)

    monkeypatch.setattr(de, "observation_session", lambda: BlockedOutcomeSession(store))
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    ledger = AsyncMock(return_value={"ledger_id": 1, "broker_status": "accepted"})
    monkeypatch.setattr(toss, "record_toss_place_order", ledger)
    monkeypatch.setattr(toss, "record_buy_exit_plan", AsyncMock())
    client = MockTossClient(monkeypatch)
    try:
        result = await asyncio.wait_for(
            toss._toss_place_order_impl(
                symbol="AAPL",
                side="buy",
                quantity="2",
                price="190",
                market="us",
                dry_run=False,
                confirm=True,
                account_mode="toss_live",
                thesis="entry",
                strategy="swing",
                client_order_id_override="same-key",
            ),
            timeout=0.5,
        )
        assert result["success"] is True
        assert len(client.placed_payloads) == 1
        ledger.assert_awaited_once()
        await asyncio.wait_for(outcome_started.wait(), timeout=0.5)
        assert rows(store, "decision_order_link_outcomes") == []
    finally:
        release.set()
        await drain_observations()
    assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


@pytest.mark.asyncio
async def test_observation_sets_transaction_local_database_timeouts():
    store = Store()

    class InspectSession(Session):
        async def execute(self, sql, values=None):
            if str(sql).startswith("SELECT set_config"):
                store.events.append(str(sql))
            await super().execute(sql, values)

    attempt = await de.begin_order_attempt(
        lambda: InspectSession(store),
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        side="buy",
        strategy=None,
        send_key="key",
        proposal_id=None,
        rung_index=None,
        decision_id=None,
        proposal_flow=False,
    )
    await de.finish_order_attempt(lambda: InspectSession(store), attempt, "sent")
    commands = [
        event for event in store.events if event.startswith("SELECT set_config")
    ]
    assert len(commands) == 2
    assert all("'lock_timeout', '1000ms', true" in sql for sql in commands)
    assert all("'statement_timeout', '5000ms', true" in sql for sql in commands)


@pytest.mark.asyncio
async def test_upbit_transport_wrapper_preserves_original_order_arguments(monkeypatch):
    from app.mcp_server.tooling import order_execution as order

    broker = AsyncMock(return_value={"uuid": "fake"})
    monkeypatch.setattr(order.upbit_service, "place_buy_order", broker)
    monkeypatch.setattr(
        order.upbit_service, "adjust_price_to_upbit_unit", lambda value: value
    )
    await order._execute_crypto_order(
        "KRW-BTC", "buy", "limit", 1.0, 100.0, identifier="same", pre_send_hook=None
    )
    baseline = broker.await_args
    hook = de.AttemptSendHook(None)
    await order._execute_crypto_order(
        "KRW-BTC", "buy", "limit", 1.0, 100.0, identifier="same", pre_send_hook=hook
    )
    observed = broker.await_args
    assert baseline.args == observed.args
    assert baseline.kwargs == {"identifier": "same", "pre_send_hook": None}
    assert observed.kwargs == {"identifier": "same", "pre_send_hook": hook}
    assert await hook() is None
    assert hook.post_count == 1


@pytest.mark.asyncio
async def test_proposal_context_import_failure_does_not_block_submission(
    monkeypatch, caplog
):
    import builtins
    from datetime import UTC, datetime, timedelta

    from app.services.order_proposals.revalidation import revalidate_and_submit
    from tests.services.order_proposals.test_approval_window import (
        _allowed_window_evaluator,
        _FakeRevalidationService,
    )

    now = datetime(2026, 7, 22, 14, tzinfo=UTC)
    service = _FakeRevalidationService(valid_until=now + timedelta(hours=1))
    calls = []

    async def place(**kwargs):
        calls.append(kwargs["dry_run"])
        if kwargs["dry_run"]:
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "broker-1",
            "correlation_id": "corr-1",
            "idempotency_key": "idem-1",
            "approval_hash_digest": "digest-1",
        }

    original = builtins.__import__

    def fail_context(name, *args, **kwargs):
        if name == "app.mcp_server.tooling.orders_toss_variants":
            raise ImportError("sensitive detail")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_context)
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=place,
        correlation_mint=lambda **kwargs: "corr-1",
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
        expected_policy_stamp=(
            await _allowed_window_evaluator(service.group, now=now)
        ).policy_stamp,
    )
    assert calls == [True, False]
    assert [outcome.result for outcome in outcomes] == ["submitted_resting"]
    assert "reason=context_unavailable" in caplog.text
    assert "sensitive detail" not in caplog.text


@pytest.mark.parametrize("phase", ["enter", "exit"])
def test_context_binding_errors_do_not_replace_submission_result(
    monkeypatch, caplog, phase
):
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.order_proposals.revalidation import _optional_order_link_context

    class BrokenBinding:
        def __enter__(self):
            if phase == "enter":
                raise RuntimeError("private context detail")

        def __exit__(self, *args):
            if phase == "exit":
                raise RuntimeError("private context detail")

    monkeypatch.setattr(
        toss, "_bind_order_proposal_context", lambda **kwargs: BrokenBinding()
    )
    calls = []
    with _optional_order_link_context():
        calls.append("submit")
    assert calls == ["submit"]
    assert "private context detail" not in caplog.text
    assert "reason=context_" in caplog.text


@pytest.mark.asyncio
async def test_toss_outcome_setup_exception_preserves_freshness_refusal(
    monkeypatch, caplog
):
    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from tests.test_mcp_toss_order_variants import MockTossClient

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(toss, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(de, "begin_order_attempt", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        de,
        "finish_order_attempt",
        AsyncMock(side_effect=RuntimeError("private outcome detail")),
    )
    client = MockTossClient(monkeypatch)
    error = PreSendFreshnessError(("stale",))

    async def blocked():
        raise error

    with (
        toss._bind_toss_pre_send_hook(blocked),
        pytest.raises(PreSendFreshnessError) as caught,
    ):
        await toss._toss_place_order_impl(
            symbol="AAPL",
            side="buy",
            quantity="2",
            price="190",
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            thesis="entry",
            strategy="swing",
            client_order_id_override="same-key",
        )
    assert caught.value is error
    assert client.placed_payloads == []
    assert "reason=outcome_setup_failed" in caplog.text
    assert "private outcome detail" not in caplog.text


@pytest.mark.asyncio
async def test_kis_ledger_is_not_delayed_by_blocked_sent_observation(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import order_execution as order
    from tests.services.protected_quantity.test_guard_wiring import _execute_kwargs

    release = asyncio.Event()
    outcome_started = asyncio.Event()
    store = Store()

    class BlockedOutcomeSession(Session):
        async def execute(self, sql, values=None):
            if "INSERT INTO review.decision_order_link_outcomes " in str(sql):
                outcome_started.set()
                await release.wait()
            await super().execute(sql, values)

    monkeypatch.setattr(de, "observation_session", lambda: BlockedOutcomeSession(store))
    monkeypatch.setattr(order, "_order_session_factory", lambda: store.session)
    reservation = AsyncMock(return_value=1)
    monkeypatch.setattr(
        order, "OrderSendIntentService", lambda _: SimpleNamespace(reserve=reservation)
    )
    hook = de.AttemptSendHook(None)
    hook.attempt_id = uuid4()

    async def broker(**kwargs):
        await kwargs["pre_send_hook"]()
        return {"rt_cd": "0", "odno": "fake-order"}

    monkeypatch.setattr(order, "_execute_order", broker)
    monkeypatch.setattr(order, "_record_order_history", AsyncMock())
    ledger = AsyncMock(return_value={"success": True, "broker_status": "accepted"})
    monkeypatch.setattr(kis_live_ledger, "_record_kis_live_order", ledger)
    kwargs = _execute_kwargs()
    kwargs.update(side="buy", order_quantity=1.0, is_mock=False, pre_send_hook=hook)
    try:
        result = await asyncio.wait_for(
            order._execute_and_record(**kwargs), timeout=0.5
        )
        assert result["success"] is True
        reservation.assert_awaited_once()
        ledger.assert_awaited_once()
        await asyncio.wait_for(outcome_started.wait(), timeout=0.5)
        assert rows(store, "decision_order_link_outcomes") == []
    finally:
        release.set()
        await drain_observations()
    assert rows(store, "decision_order_link_outcomes")[0]["outcome"] == "sent"


def test_observation_context_preserves_submit_exception_and_resets_binding():
    from app.mcp_server.tooling.orders_toss_variants import _order_proposal_context
    from app.services.order_proposals.revalidation import _optional_order_link_context

    original = RuntimeError("original submit error")
    with pytest.raises(RuntimeError) as captured:
        with _optional_order_link_context(
            client_order_id="test",
            correlation_id=None,
            rung=0,
            proposal_id=uuid4(),
            rung_index=0,
            decision_id=uuid4(),
        ):
            assert _order_proposal_context.get() is not None
            raise original
    assert captured.value is original
    assert _order_proposal_context.get() is None


@pytest.mark.asyncio
async def test_upbit_outcome_setup_exception_preserves_approval_refusal(
    monkeypatch, caplog
):
    from types import SimpleNamespace

    from app.core.config import settings
    from app.mcp_server.tooling import order_execution as order

    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    monkeypatch.setattr(de, "begin_order_attempt", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        de,
        "finish_order_attempt",
        AsyncMock(side_effect=RuntimeError("private outcome detail")),
    )
    monkeypatch.setattr(
        order,
        "_get_crypto_trade_cooldown_service",
        lambda: SimpleNamespace(is_in_cooldown=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(order, "_fetch_current_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(
        order,
        "_build_preview",
        AsyncMock(
            return_value={"price": 100.0, "quantity": 1.0, "estimated_value": 100.0}
        ),
    )
    monkeypatch.setattr(
        order, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order, "evaluate_sector_concentration", AsyncMock(return_value={})
    )
    broker = AsyncMock()
    monkeypatch.setattr(order, "_execute_order", broker)
    result = await order._place_order_impl(
        symbol="KRW-BTC",
        side="buy",
        market="crypto",
        quantity=1.0,
        price=100.0,
        dry_run=False,
        thesis="entry",
        strategy="swing",
        client_order_id="same-key",
    )
    assert result["error_code"] == "approval_hash_required"
    broker.assert_not_awaited()
    assert "reason=outcome_setup_failed" in caplog.text
    assert "private outcome detail" not in caplog.text
