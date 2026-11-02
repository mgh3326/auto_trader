"""Unit tests for the read-only approval batch health surface (ROB-1488)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.order_proposals.batch_health import (
    BatchHealthReport,
    BatchHealthWindow,
    batch_member_skip_intentional,
    evaluate_batch_health,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _window(**overrides) -> BatchHealthWindow:
    base = {
        "days": 7,
        "manual_cards": 0,
        "member_added": 0,
        "member_processed": 0,
        "batches": 0,
        "summaries_sent": 0,
        "frozen_unsent": 0,
        "published_cards_without_member": 0,
    }
    base.update(overrides)
    return BatchHealthWindow(**base)


@pytest.mark.unit
def test_healthy_writer_is_ok() -> None:
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=7, member_added=7, batches=7),
        zero_member_batches=0,
    )
    assert status == "ok"
    assert anomalies == ()


@pytest.mark.unit
def test_zero_member_batch_is_a_gap() -> None:
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=1, member_added=1),
        zero_member_batches=1,
    )
    assert status == "gap"
    assert anomalies == ("approval_batch_without_members",)


@pytest.mark.unit
def test_cards_without_members_need_review_not_a_proven_writer_failure() -> None:
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=5, member_added=0),
        zero_member_batches=0,
    )
    assert status == "review"
    assert anomalies == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "counts",
    [
        {"frozen_unsent": 1},
        {"published_cards_without_member": 1},
        {"frozen_unsent": 1, "published_cards_without_member": 1},
    ],
)
@pytest.mark.parametrize("zero_member_batches", [0, 1])
def test_summary_and_partial_membership_gaps_require_review(
    counts: dict[str, int], zero_member_batches: int
) -> None:
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=4, member_added=3, **counts),
        zero_member_batches=zero_member_batches,
    )
    assert status == ("gap" if zero_member_batches else "review")
    assert anomalies == (
        ("approval_batch_without_members",) if zero_member_batches else ()
    )


@pytest.mark.unit
def test_idle_surface_is_not_a_gap() -> None:
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=0, member_added=0),
        zero_member_batches=0,
    )
    assert status == "ok"
    assert anomalies == ()


@pytest.mark.unit
def test_result_recorder_silence_is_not_a_writer_gap() -> None:
    """The 2026-11-02 inventory read `processed_at`; members were being written.

    A window with member rows but no recorded callback results is a healthy
    writer with no batch click yet -- not a stopped member writer.
    """
    status, anomalies = evaluate_batch_health(
        recent=_window(manual_cards=4, member_added=4, member_processed=0),
        zero_member_batches=0,
    )
    assert status == "ok"
    assert anomalies == ()


@pytest.mark.unit
def test_report_names_the_writer_and_result_columns() -> None:
    now = datetime.now(UTC)
    report = BatchHealthReport(
        as_of=now,
        recent=_window(days=7),
        window=_window(days=30),
        members_total=354,
        zero_member_batches=0,
        status="ok",
    )
    payload = report.as_dict()
    assert payload["notes"]["member_writer_column"] == "added_at"
    assert payload["notes"]["result_recorder_column"] == "processed_at"
    assert payload["notes"]["auto_digest_writes_batch_rows"] is False
    assert payload["recent"]["days"] == 7
    assert payload["window"]["days"] == 30


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    ["loss_cut_excluded", "cash_funding_excluded", "auto_approved_excluded"],
)
def test_designed_exclusions_are_intentional(reason: str) -> None:
    assert batch_member_skip_intentional(reason) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    [
        "proposal_terminal:expired",
        "proposal_superseded_by:00000000-0000-0000-0000-000000000000",
        "no_pending_approval_rungs",
        "approval_dispatch_not_current",
        "approval_card_kind_not_batchable",
        "approval_nonce_used",
        "approval_window:SESSION_CLOSED:unspecified",
        "duplicate_nonce_member",
        "already_member_of_batch",
    ],
)
def test_state_change_skips_are_not_intentional(reason: str) -> None:
    assert batch_member_skip_intentional(reason) is False


@pytest.mark.unit
def test_health_readers_are_select_only() -> None:
    """Both surfaces must stay read-only; a health probe cannot mutate."""
    for relative in (
        "app/services/order_proposals/batch_health.py",
        "scripts/diagnose_approval_batch_health.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        forbidden_calls = (
            "insert(",
            "update(",
            "delete(",
            ".commit(",
            ".flush(",
        )
        for forbidden in forbidden_calls:
            assert forbidden not in source, f"{relative} contains {forbidden!r}"
        assert "select(" in source or "load_batch_health" in source


def test_skip_log_uses_closed_category_without_dynamic_detail(caplog) -> None:
    import logging
    import uuid

    from app.services.order_proposals.service import _log_batch_member_skip

    with caplog.at_level(logging.WARNING):
        _log_batch_member_skip(
            proposal_id=uuid.UUID(int=1),
            reason="proposal_superseded_by:private-dynamic-detail",
        )
    record = caplog.records[-1]
    assert record.reason == "proposal_superseded"
    assert "private-dynamic-detail" not in str(record.__dict__)


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("proposal_terminal:private", "proposal_terminal"),
        ("approval_window:code:private", "approval_window_blocked"),
        ("unknown-private-detail", "unclassified"),
    ],
)
def test_skip_categories_drop_dynamic_details(reason, expected):
    from app.services.order_proposals.batch_health import batch_member_skip_category

    assert batch_member_skip_category(reason) == expected
