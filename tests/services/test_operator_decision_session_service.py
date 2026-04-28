from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_no_advisory_path_persists_via_raw_helpers(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    fake_session = SimpleNamespace(
        id=42,
        session_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        status="open",
        market_brief={},
    )
    fake_proposals = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    create_session_mock = AsyncMock(return_value=fake_session)
    add_proposals_mock = AsyncMock(return_value=fake_proposals)

    monkeypatch.setattr(
        svc.trading_decision_service,
        "create_decision_session",
        create_session_mock,
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "add_decision_proposals",
        add_proposals_mock,
    )
    monkeypatch.setattr(
        svc,
        "create_synthesized_decision_session",
        AsyncMock(side_effect=AssertionError("must not run")),
    )
    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(side_effect=AssertionError("must not run")),
    )

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                confidence=70,
                proposal_kind="enter",
                rationale="test",
            ),
            OperatorCandidate(
                symbol="000660",
                instrument_type="equity_kr",
                side="none",
                confidence=40,
                proposal_kind="pullback_watch",
            ),
        ],
        include_tradingagents=False,
        notes="op session",
    )

    result = await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=7, request=req
    )

    assert result.advisory_used is False
    assert result.advisory_skipped_reason == "include_tradingagents=False"
    assert result.proposal_count == 2

    create_session_mock.assert_awaited_once()
    create_kwargs = create_session_mock.await_args.kwargs
    assert create_kwargs["user_id"] == 7
    assert create_kwargs["source_profile"] == "operator_request"
    assert create_kwargs["market_scope"] == "kr"
    assert create_kwargs["notes"] == "op session"
    assert create_kwargs["market_brief"]["advisory_only"] is True
    assert create_kwargs["market_brief"]["execution_allowed"] is False
    assert "synthesis_meta" not in create_kwargs["market_brief"]

    add_proposals_mock.assert_awaited_once()
    proposals_arg = add_proposals_mock.await_args.kwargs["proposals"]
    assert len(proposals_arg) == 2
    for proposal in proposals_arg:
        payload = proposal["original_payload"]
        assert payload["advisory_only"] is True
        assert payload["execution_allowed"] is False
        assert "synthesis" not in payload
        assert payload["operator_request"]["applied_policies"] == ["no_advisory"]


@pytest.mark.asyncio
async def test_no_advisory_path_uses_now_callable(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    fake_session = SimpleNamespace(
        id=1, session_uuid="zz", status="open", market_brief={}
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "create_decision_session",
        AsyncMock(return_value=fake_session),
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "add_decision_proposals",
        AsyncMock(return_value=[SimpleNamespace(id=1)]),
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(symbol="AAPL", instrument_type="equity_us", confidence=50)
        ],
    )
    fixed = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await svc.create_operator_decision_session(
        SimpleNamespace(),
        user_id=1,
        request=req,
        now=lambda: fixed,
    )
    create_kwargs = (
        svc.trading_decision_service.create_decision_session.await_args.kwargs
    )
    assert create_kwargs["generated_at"] == fixed


@pytest.mark.asyncio
async def test_advisory_path_uses_synthesis_persistence(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.schemas.tradingagents_research import (
        TradingAgentsConfigSnapshot,
        TradingAgentsLLM,
        TradingAgentsRunnerResult,
        TradingAgentsWarnings,
    )

    fake_runner_result = TradingAgentsRunnerResult(
        status="ok",
        symbol="NVDA",
        as_of_date=date(2026, 4, 28),
        decision="Underweight",
        advisory_only=True,
        execution_allowed=False,
        analysts=["market"],
        llm=TradingAgentsLLM(
            provider="openai-compatible",
            model="gpt-5.5",
            base_url="http://127.0.0.1:8796/v1",
        ),
        config=TradingAgentsConfigSnapshot(
            max_debate_rounds=1,
            max_risk_discuss_rounds=1,
            max_recur_limit=30,
            output_language="English",
            checkpoint_enabled=False,
        ),
        warnings=TradingAgentsWarnings(),
        final_trade_decision="no execution",
        raw_state_keys=["k1", "k2"],
    )

    fake_session = SimpleNamespace(
        id=99,
        session_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        status="open",
    )
    fake_proposals = [SimpleNamespace(id=10)]
    runner_mock = AsyncMock(return_value=fake_runner_result)
    synth_persist_mock = AsyncMock(return_value=(fake_session, fake_proposals))

    monkeypatch.setattr(svc, "run_tradingagents_research", runner_mock)
    monkeypatch.setattr(svc, "create_synthesized_decision_session", synth_persist_mock)
    monkeypatch.setattr(
        svc.trading_decision_service,
        "create_decision_session",
        AsyncMock(side_effect=AssertionError("must not run")),
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
        analysts=["market"],
        strategy_name="op_us",
    )

    result = await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=7, request=req
    )

    assert result.advisory_used is True
    assert result.advisory_skipped_reason is None
    assert result.proposal_count == 1
    runner_mock.assert_awaited_once()
    synth_persist_mock.assert_awaited_once()
    persist_kwargs = synth_persist_mock.await_args.kwargs
    assert persist_kwargs["user_id"] == 7
    assert persist_kwargs["market_scope"] == "us"
    assert persist_kwargs["source_profile"] == "operator_request+tradingagents"
    assert persist_kwargs["strategy_name"] == "op_us"
    assert persist_kwargs["market_brief"]["advisory_only"] is True
    assert persist_kwargs["market_brief"]["execution_allowed"] is False
    assert (
        persist_kwargs["market_brief"]["operator_request"]["include_tradingagents"]
        is True
    )
    synthesized = persist_kwargs["proposals"]
    assert len(synthesized) == 1
    assert synthesized[0].advisory.advisory_only is True
    assert synthesized[0].advisory.execution_allowed is False
    assert synthesized[0].final_proposal_kind == "pullback_watch"
    assert synthesized[0].final_side == "none"


@pytest.mark.asyncio
async def test_advisory_missing_config_raises_without_persistence(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.services.tradingagents_research_service import (
        TradingAgentsNotConfigured,
    )

    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(side_effect=TradingAgentsNotConfigured("missing")),
    )
    no_persistence = AsyncMock(side_effect=AssertionError("must not persist"))
    monkeypatch.setattr(svc, "create_synthesized_decision_session", no_persistence)
    monkeypatch.setattr(
        svc.trading_decision_service, "create_decision_session", no_persistence
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
    )

    with pytest.raises(TradingAgentsNotConfigured):
        await svc.create_operator_decision_session(
            SimpleNamespace(), user_id=1, request=req
        )


@pytest.mark.asyncio
async def test_advisory_runner_error_propagates_without_persistence(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.services.tradingagents_research_service import TradingAgentsRunnerError

    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(side_effect=TradingAgentsRunnerError("crashed")),
    )
    monkeypatch.setattr(
        svc,
        "create_synthesized_decision_session",
        AsyncMock(side_effect=AssertionError("must not persist")),
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
    )

    with pytest.raises(TradingAgentsRunnerError):
        await svc.create_operator_decision_session(
            SimpleNamespace(), user_id=1, request=req
        )
