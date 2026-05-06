# tests/schemas/test_trade_journal_schemas.py
import pytest
from pydantic import ValidationError

from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCoverageRow,
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)


def test_create_request_rejects_closed_status() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="meaningful thesis",
            status="closed",
        )


def test_create_request_accepts_draft_and_active() -> None:
    for status in ("draft", "active"):
        req = JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="meaningful thesis",
            status=status,
        )
        assert req.status == status


def test_create_request_rejects_negative_min_hold_days() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="thesis",
            min_hold_days=-1,
        )


def test_create_request_rejects_empty_thesis() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="   ",
        )


def test_update_request_allows_partial_payload() -> None:
    req = JournalUpdateRequest(thesis="updated thesis")
    assert req.thesis == "updated thesis"
    assert req.target_price is None


def test_update_request_rejects_terminal_status() -> None:
    with pytest.raises(ValidationError):
        JournalUpdateRequest(status="stopped")


def test_coverage_response_round_trip() -> None:
    row = JournalCoverageRow(
        symbol="005930",
        name="삼성전자",
        market="KR",
        instrument_type="equity_kr",
        quantity=10.0,
        position_weight_pct=12.5,
        journal_status="missing",
        journal_id=None,
        thesis=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        hold_until=None,
        latest_research_session_id=None,
        latest_research_summary_id=None,
        latest_summary_decision=None,
        thesis_conflict_with_summary=False,
    )
    resp = JournalCoverageResponse(generated_at="2026-05-06T00:00:00Z", total=1, rows=[row])
    assert resp.total == 1


def test_read_response_fields_present() -> None:
    JournalReadResponse(
        id=1,
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        thesis="t",
        status="draft",
        account_type="live",
        created_at="2026-05-06T00:00:00Z",
        updated_at="2026-05-06T00:00:00Z",
        research_session_id=None,
        research_summary_id=None,
    )
