from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.tasks",
    "app.services.tradingagents_research_service",
]


def test_synthesis_modules_do_not_import_forbidden_prefixes():
    baseline = set(sys.modules)
    for module_name in [
        "app.schemas.trading_decision_synthesis",
        "app.services.trading_decision_synthesis",
        "app.services.trading_decision_synthesis_persistence",
    ]:
        importlib.import_module(module_name)
    loaded = set(sys.modules) - baseline
    violations = sorted(
        name
        for name in loaded
        for prefix in _FORBIDDEN_PREFIXES
        if name == prefix or name.startswith(prefix + ".")
    )
    assert violations == []


@pytest.mark.asyncio
async def test_persistence_composes_only_session_and_proposal_helpers(monkeypatch):
    import app.services.trading_decision_synthesis_persistence as persistence
    from app.schemas.trading_decision_synthesis import (
        AdvisoryEvidence,
        CandidateAnalysis,
    )
    from app.services.trading_decision_synthesis import (
        synthesize_candidate_with_advisory,
    )

    candidate = CandidateAnalysis(
        symbol="NVDA",
        instrument_type="equity_us",
        side="buy",
        confidence=65,
        proposal_kind="enter",
        rationale="deterministic buy",
    )
    advisory = AdvisoryEvidence(
        advisory_only=True,
        execution_allowed=False,
        advisory_action="Underweight",
        decision_text="risk off",
        final_trade_decision_text="no execution",
        provider="openai-compatible",
        model="gpt-5.5",
        base_url="http://127.0.0.1:8796/v1",
    )
    synthesized = synthesize_candidate_with_advisory(candidate, advisory)
    fake_session = SimpleNamespace(id=123, market_brief={})
    fake_proposal = SimpleNamespace(id=456)
    create_mock = AsyncMock(return_value=fake_session)
    add_mock = AsyncMock(return_value=[fake_proposal])
    monkeypatch.setattr(persistence, "create_decision_session", create_mock)
    monkeypatch.setattr(persistence, "add_decision_proposals", add_mock)

    (
        result_session,
        result_proposals,
    ) = await persistence.create_synthesized_decision_session(
        SimpleNamespace(),
        user_id=1,
        proposals=[synthesized],
        generated_at=datetime.now(UTC),
        market_scope="us",
    )

    assert result_session is fake_session
    assert result_proposals == [fake_proposal]
    create_mock.assert_awaited_once()
    add_mock.assert_awaited_once()
    create_kwargs = create_mock.await_args.kwargs
    assert create_kwargs["market_brief"]["advisory_only"] is True
    assert create_kwargs["market_brief"]["execution_allowed"] is False
    assert create_kwargs["market_brief"]["synthesis_meta"]["conflict_count"] == 1
    proposal_payload = add_mock.await_args.kwargs["proposals"][0]["original_payload"]
    assert proposal_payload["advisory_only"] is True
    assert proposal_payload["execution_allowed"] is False
    assert proposal_payload["synthesis"]["final_side"] == "none"


def test_persistence_rejects_empty_proposal_list():
    import app.services.trading_decision_synthesis_persistence as persistence

    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(
            persistence.create_synthesized_decision_session(
                SimpleNamespace(),
                user_id=1,
                proposals=[],
                generated_at=datetime.now(UTC),
            )
        )
