"""Unit tests for app.schemas.research_run."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.research_run import (
    ResearchRunCandidateCreate,
    ResearchRunCreate,
    ResearchRunPendingReconciliationCreate,
)


@pytest.mark.unit
def test_run_create_minimum_fields() -> None:
    payload = ResearchRunCreate(
        market_scope="kr",
        stage="preopen",
        source_profile="kr_morning_brief",
        generated_at=datetime.now(UTC),
    )
    assert payload.market_scope == "kr"
    assert payload.stage == "preopen"
    assert payload.advisory_links == []
    assert payload.source_warnings == []


@pytest.mark.unit
def test_run_create_rejects_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="not_a_stage",
            source_profile="x",
            generated_at=datetime.now(UTC),
        )


@pytest.mark.unit
def test_run_create_rejects_unknown_market_scope() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="forex",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
        )


@pytest.mark.unit
def test_run_create_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            unexpected="bad",
        )


@pytest.mark.unit
def test_advisory_link_must_be_advisory_only() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            advisory_links=[
                {
                    "advisory_only": False,
                    "execution_allowed": False,
                    "session_uuid": str(uuid4()),
                }
            ],
        )
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            advisory_links=[
                {
                    "advisory_only": True,
                    "execution_allowed": True,
                    "session_uuid": str(uuid4()),
                }
            ],
        )


@pytest.mark.unit
def test_candidate_create_symbol_charset() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="bad symbol with spaces",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
        )


@pytest.mark.unit
def test_candidate_create_confidence_range() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="005930",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
            confidence=150,
        )


@pytest.mark.unit
def test_candidate_create_warning_charset() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="005930",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
            warnings=["BAD-WARNING"],
        )


@pytest.mark.unit
def test_pending_reconciliation_create_required_fields() -> None:
    item = ResearchRunPendingReconciliationCreate(
        order_id="O1",
        symbol="005930",
        market="kr",
        side="buy",
        classification="maintain",
        decision_support={"current_price": "70000.0", "gap_pct": "0.0"},
    )
    assert item.classification == "maintain"
    assert item.nxt_classification is None
    assert item.gap_pct is None


@pytest.mark.unit
def test_pending_reconciliation_create_with_nxt() -> None:
    item = ResearchRunPendingReconciliationCreate(
        order_id="O2",
        symbol="005930",
        market="kr",
        side="sell",
        classification="maintain",
        nxt_classification="sell_pending_near_resistance",
        nxt_actionable=True,
        gap_pct=Decimal("0.42"),
        summary="NXT 매도 대기 — 저항선 근접 (저항선 71000)",
    )
    assert item.nxt_classification == "sell_pending_near_resistance"
    assert item.nxt_actionable is True
