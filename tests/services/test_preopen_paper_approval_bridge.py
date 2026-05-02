"""Tests for the ROB-81 preopen paper approval bridge."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.schemas.preopen import (
    CandidateSummary,
    PreopenBriefingArtifact,
    PreopenDecisionSessionCta,
    PreopenQaCheck,
    PreopenQaEvaluatorSummary,
    PreopenQaScore,
)
from app.services.preopen_paper_approval_bridge import (
    build_preopen_paper_approval_bridge,
)


def _candidate(**kwargs) -> CandidateSummary:
    defaults = {
        "candidate_uuid": uuid4(),
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "side": "buy",
        "candidate_kind": "proposed",
        "proposed_price": Decimal("100000000"),
        "proposed_qty": None,
        "confidence": 70,
        "rationale": "Crypto paper plumbing candidate",
        "currency": "KRW",
        "warnings": [],
    }
    defaults.update(kwargs)
    return CandidateSummary(**defaults)


def _artifact(**kwargs) -> PreopenBriefingArtifact:
    defaults = {
        "status": "ready",
        "market_scope": "crypto",
        "stage": "preopen",
        "risk_notes": [],
        "cta": PreopenDecisionSessionCta(
            state="create_available",
            label="Create decision session",
            requires_confirmation=True,
        ),
        "qa": {"read_only": True},
    }
    defaults.update(kwargs)
    return PreopenBriefingArtifact(**defaults)


def _qa(**kwargs) -> PreopenQaEvaluatorSummary:
    defaults = {
        "status": "ready",
        "generated_at": datetime.now(UTC),
        "overall": PreopenQaScore(score=90, grade="excellent", confidence="high"),
        "checks": [
            PreopenQaCheck(
                id="actionability_guardrail",
                label="Actionability guardrail",
                status="pass",
                severity="info",
                summary="Execution disabled.",
                details={"advisory_only": True, "execution_allowed": False},
            )
        ],
        "blocking_reasons": [],
        "warnings": [],
        "coverage": {"advisory_only": True, "execution_allowed": False},
    }
    defaults.update(kwargs)
    return PreopenQaEvaluatorSummary(**defaults)


def test_no_run_blocks_bridge() -> None:
    bridge = build_preopen_paper_approval_bridge(
        has_run=False,
        market_scope="crypto",
        candidates=[],
        briefing_artifact=_artifact(status="unavailable"),
        qa_evaluator=_qa(status="unavailable"),
    )

    assert bridge.status == "blocked"
    assert bridge.preview_only is True
    assert bridge.advisory_only is True
    assert bridge.execution_allowed is False
    assert "no_open_preopen_run" in bridge.blocking_reasons
    assert bridge.candidates == []


def test_high_severity_fail_blocks_even_for_crypto_candidate() -> None:
    qa = _qa(
        status="needs_review",
        checks=[
            PreopenQaCheck(
                id="readiness_safety",
                label="Readiness safety",
                status="fail",
                severity="high",
                summary="Safety gate failed.",
            )
        ],
        blocking_reasons=["readiness_safety"],
    )

    bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="crypto",
        candidates=[_candidate()],
        briefing_artifact=_artifact(),
        qa_evaluator=qa,
    )

    assert bridge.status == "blocked"
    assert "readiness_safety" in bridge.blocking_reasons
    assert "high_severity_fail:readiness_safety" in bridge.blocking_reasons
    assert bridge.eligible_count == 0
    assert bridge.candidates == []


def test_ready_crypto_buy_allowlist_builds_preview_with_provenance() -> None:
    candidate = _candidate(symbol="KRW-BTC")

    bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="crypto",
        candidates=[candidate],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "available"
    assert bridge.eligible_count == 1
    assert bridge.candidate_count == 1
    item = bridge.candidates[0]
    assert item.status == "available"
    assert item.symbol == "KRW-BTC"
    assert item.signal_symbol == "KRW-BTC"
    assert item.signal_venue == "upbit"
    assert item.execution_symbol == "BTC/USD"
    assert item.execution_venue == "alpaca_paper"
    assert item.execution_asset_class == "crypto"
    assert item.workflow_stage == "crypto_weekend"
    assert item.purpose == "paper_plumbing_smoke"
    assert item.preview_payload == {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "notional": "10",
        "limit_price": "1.00",
        "time_in_force": "gtc",
        "asset_class": "crypto",
    }
    forbidden = {
        "confirm",
        "dry_run",
        "order_id",
        "client_order_id",
        "submitted",
        "submit",
        "action",
    }
    assert forbidden.isdisjoint(item.preview_payload or {})
    assert any("Signal source: Upbit KRW-BTC" in line for line in item.approval_copy)
    assert any(
        "Execution venue: Alpaca Paper BTC/USD" in line for line in item.approval_copy
    )


def test_qa_warnings_and_degraded_artifact_make_bridge_warning() -> None:
    bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="crypto",
        candidates=[_candidate(warnings=["candidate_warning"])],
        briefing_artifact=_artifact(
            status="degraded", risk_notes=["briefing_degraded"]
        ),
        qa_evaluator=_qa(status="needs_review", warnings=["qa_warning"]),
    )

    assert bridge.status == "warning"
    assert bridge.candidates[0].status == "warning"
    assert "qa_needs_review" in bridge.warnings
    assert "qa_warning" in bridge.warnings
    assert "briefing_artifact_degraded" in bridge.warnings
    assert "candidate_warning" in bridge.warnings


def test_unsupported_crypto_symbol_is_unavailable_without_preview_payload() -> None:
    bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="crypto",
        candidates=[_candidate(symbol="KRW-XRP")],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    assert bridge.eligible_count == 0
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.preview_payload is None
    assert "unsupported crypto signal symbol" in (item.reason or "")


def test_non_crypto_market_and_sell_candidates_are_unavailable() -> None:
    kr_bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate(symbol="005930", instrument_type="equity_kr")],
        briefing_artifact=_artifact(market_scope="kr"),
        qa_evaluator=_qa(),
    )
    sell_bridge = build_preopen_paper_approval_bridge(
        has_run=True,
        market_scope="crypto",
        candidates=[_candidate(side="sell")],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert kr_bridge.status == "unavailable"
    assert kr_bridge.candidates[0].reason == "unsupported_market_scope:kr"
    assert sell_bridge.status == "unavailable"
    assert sell_bridge.candidates[0].reason == "unsupported_side:sell"


def test_bridge_module_imports_only_pure_allowed_modules() -> None:
    path = Path("app/services/preopen_paper_approval_bridge.py")
    tree = ast.parse(path.read_text())
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    forbidden_fragments = [
        "broker",
        "kis",
        "upbit",
        "mcp",
        "watch",
        "redis",
        "scheduler",
        "httpx",
        "requests",
        "paper_trading",
    ]
    assert not [
        module
        for module in imported_modules
        if any(fragment in module.lower() for fragment in forbidden_fragments)
    ]
    assert "app.services.crypto_execution_mapping" in imported_modules
