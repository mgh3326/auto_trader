"""Tests for the ROB-95 KR/KIS mock preopen approval bridge."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.preopen import CandidateSummary, PreopenQaCheck
from app.services.kis_mock_preopen_approval_bridge import (
    build_kis_mock_preopen_approval_bridge,
)
from tests.services.preopen_approval_bridge_helpers import (
    preopen_artifact,
    preopen_candidate,
    preopen_qa,
)


def _candidate(**kwargs) -> CandidateSummary:
    payload = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "price": Decimal("229500"),
        "quantity": Decimal("1"),
        "confidence": 72,
        "rationale": "KR mock pilot candidate",
    }
    if "proposed_price" in kwargs:
        payload["price"] = kwargs.pop("proposed_price")
    if "proposed_qty" in kwargs:
        payload["quantity"] = kwargs.pop("proposed_qty")
    payload.update(kwargs)
    return preopen_candidate(**payload)


def _artifact(**kwargs):
    return preopen_artifact(kwargs.pop("market_scope", "kr"), **kwargs)


def _qa(**kwargs):
    return preopen_qa("Execution disabled before operator approval.", **kwargs)


@pytest.mark.unit
def test_ready_kr_buy_candidate_builds_kis_mock_dry_run_preview_metadata() -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate()],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "available"
    assert bridge.preview_only is True
    assert bridge.advisory_only is True
    assert bridge.execution_allowed is False
    assert bridge.market_scope == "kr"
    assert bridge.eligible_count == 1
    item = bridge.candidates[0]
    assert item.status == "available"
    assert item.symbol == "005930"
    assert item.signal_symbol == "005930"
    assert item.signal_venue == "kr_preopen"
    assert item.execution_symbol == "005930"
    assert item.execution_venue == "kis_mock"
    assert item.execution_asset_class == "equity_kr"
    assert item.workflow_stage == "kr_market_open_mock"
    assert item.purpose == "kis_mock_market_open_pilot"
    assert item.preview_payload == {
        "tool": "kis_mock_place_order",
        "symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "price": "229500",
        "account_mode": "kis_mock",
        "execution_venue": "kis_mock",
        "execution_asset_class": "equity_kr",
        "dry_run": True,
        "regular_session_only": True,
        "requires_final_mock_submit_approval": True,
    }
    approval_copy = "\n".join(item.approval_copy)
    assert "KIS official mock only" in approval_copy
    assert "No KIS live order" in approval_copy
    assert "dry_run=True" in approval_copy
    assert "dry_run=False" in approval_copy
    assert "live submission" not in approval_copy.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate", "expected_reason"),
    [
        (_candidate(symbol="AAPL"), "unsupported_symbol:AAPL"),
        (_candidate(symbol="KRW-BTC"), "unsupported_symbol:KRW-BTC"),
        (_candidate(symbol="00593A"), "unsupported_symbol:00593A"),
        (_candidate(symbol="5930"), "unsupported_symbol:5930"),
    ],
)
def test_malformed_or_non_kr_symbols_are_unavailable_without_preview_payload(
    candidate: CandidateSummary,
    expected_reason: str,
) -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[candidate],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == expected_reason
    assert item.preview_payload is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate_patch", "expected_reason"),
    [
        ({"proposed_price": Decimal("0")}, "invalid_price"),
        ({"proposed_price": Decimal("-1")}, "invalid_price"),
        ({"proposed_price": Decimal("229500.5")}, "invalid_price"),
        ({"proposed_qty": Decimal("0")}, "invalid_quantity"),
        ({"proposed_qty": Decimal("-1")}, "invalid_quantity"),
        ({"proposed_qty": Decimal("1.5")}, "invalid_quantity"),
    ],
)
def test_invalid_price_or_quantity_is_unavailable_without_preview_payload(
    candidate_patch: dict[str, Decimal],
    expected_reason: str,
) -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate(**candidate_patch)],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == expected_reason
    assert item.preview_payload is None


@pytest.mark.unit
def test_missing_limit_price_is_unavailable_without_preview_payload() -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate(proposed_price=None)],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    assert bridge.eligible_count == 0
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == "missing_price"
    assert item.preview_payload is None


@pytest.mark.unit
def test_ready_kr_sell_requires_explicit_quantity() -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate(side="sell", proposed_qty=None)],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    assert bridge.eligible_count == 0
    assert bridge.unsupported_reasons == ["missing_quantity"]
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == "missing_quantity"
    assert item.preview_payload is None


@pytest.mark.unit
def test_side_none_is_unavailable_without_preview_payload() -> None:
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate(side="none")],
        briefing_artifact=_artifact(),
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    assert bridge.eligible_count == 0
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == "unsupported_side:none"
    assert item.preview_payload is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("market_scope", "candidate", "expected_reason", "expected_bridge_scope"),
    [
        (
            "crypto",
            _candidate(symbol="KRW-BTC", instrument_type="crypto"),
            "unsupported_market_scope:crypto",
            "crypto",
        ),
        (
            "kr",
            _candidate(symbol="KRW-BTC", instrument_type="crypto"),
            "unsupported_instrument_type:crypto",
            "kr",
        ),
        (
            "us",
            _candidate(symbol="AAPL", instrument_type="equity_us"),
            "unsupported_market_scope:us",
            "us",
        ),
        (
            "jp",
            _candidate(symbol="7203", instrument_type="equity_jp"),
            "unsupported_market_scope:jp",
            None,
        ),
    ],
)
def test_non_kr_and_crypto_candidates_are_unavailable_without_kis_suggestion(
    market_scope: str,
    candidate: CandidateSummary,
    expected_reason: str,
    expected_bridge_scope: str | None,
) -> None:
    briefing_artifact = (
        None
        if market_scope not in {"kr", "us", "crypto"}
        else _artifact(market_scope=market_scope)
    )
    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope=market_scope,
        candidates=[candidate],
        briefing_artifact=briefing_artifact,
        qa_evaluator=_qa(),
    )

    assert bridge.status == "unavailable"
    assert bridge.market_scope == expected_bridge_scope
    assert bridge.eligible_count == 0
    item = bridge.candidates[0]
    assert item.status == "unavailable"
    assert item.reason == expected_reason
    assert item.preview_payload is None
    assert item.execution_venue is None
    assert "kis_mock_place_order" not in "\n".join(item.approval_copy)


@pytest.mark.unit
def test_high_severity_qa_failure_blocks_kis_mock_bridge() -> None:
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

    bridge = build_kis_mock_preopen_approval_bridge(
        has_run=True,
        market_scope="kr",
        candidates=[_candidate()],
        briefing_artifact=_artifact(),
        qa_evaluator=qa,
    )

    assert bridge.status == "blocked"
    assert "readiness_safety" in bridge.blocking_reasons
    assert "high_severity_fail:readiness_safety" in bridge.blocking_reasons
    assert bridge.eligible_count == 0
    assert bridge.candidates == []


@pytest.mark.unit
def test_kis_mock_bridge_module_imports_only_pure_allowed_modules() -> None:
    path = Path("app/services/kis_mock_preopen_approval_bridge.py")
    imported_modules = [
        node.module
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    ]

    forbidden_fragments = {
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
    }
    assert all(
        not any(fragment in module.lower() for fragment in forbidden_fragments)
        for module in imported_modules
    )
    assert set(imported_modules) == {
        "__future__",
        "datetime",
        "app.schemas.preopen",
        "app.services.preopen_approval_bridge_common",
        "app.services.preopen_approval_safety",
    }
