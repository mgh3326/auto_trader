"""ROB-1036 §4.3-9 — approval window and cross-session carry-over are fail-closed.

Offline: the clock is caller-supplied, so no wall-clock or network dependency.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.invalid_sample_eligibility.binding import (
    CLEANUP_PURPOSE,
    CleanupBinding,
    CleanupBindingError,
    build_cleanup_binding,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 2, 6, 0, tzinfo=UTC)
FORECAST_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _build(**overrides):
    kwargs = {
        "forecast_id": FORECAST_ID,
        "sample_ref": "uber-d2-cleanup",
        "approval_id": "approval-uber-001",
        "approval_hash": "a" * 64,
        "approval_expires_at": NOW + timedelta(minutes=5),
        "approval_session_id": "session-A",
        "mission_id": "invalid-sample-cleanup-mission-1",
        "account_mode": "alpaca_paper_lab",
        "client_order_id": "cleanup-uber-001",
        "lifecycle_correlation_id": "corr-uber-001",
        "now": NOW,
        "session_id": "session-A",
    }
    kwargs.update(overrides)
    return build_cleanup_binding(**kwargs)


def test_binding_carries_every_identity() -> None:
    binding = _build()
    assert binding.purpose == CLEANUP_PURPOSE
    assert binding.forecast_id == FORECAST_ID
    assert binding.approval_id == "approval-uber-001"
    assert binding.mission_id == "invalid-sample-cleanup-mission-1"
    assert binding.client_order_id == "cleanup-uber-001"
    assert binding.lifecycle_correlation_id == "corr-uber-001"
    assert len(binding.binding_hash) == 64


def test_binding_hash_changes_with_any_bound_identity() -> None:
    baseline = _build().binding_hash
    assert _build(mission_id="other-mission").binding_hash != baseline
    assert _build(approval_id="approval-uber-002").binding_hash != baseline
    assert _build(client_order_id="cleanup-uber-002").binding_hash != baseline
    assert _build(sample_ref="other-sample").binding_hash != baseline


def test_binding_is_frozen() -> None:
    binding = _build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.mission_id = "tampered"  # type: ignore[misc]


def test_expired_approval_window_fails_closed() -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(approval_expires_at=NOW - timedelta(seconds=1))
    assert excinfo.value.code == "approval_window_expired"


def test_window_boundary_is_exclusive() -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(approval_expires_at=NOW)
    assert excinfo.value.code == "approval_window_expired"


def test_missed_window_is_not_carried_into_a_later_session() -> None:
    """A missed 15:55 ET window is not silently reused by the next session."""

    later = NOW + timedelta(hours=12)
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(now=later)
    assert excinfo.value.code == "approval_window_expired"


def test_cross_session_reuse_of_a_live_approval_is_blocked() -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(session_id="session-B")
    assert excinfo.value.code == "cross_session_carry_over_blocked"


def test_naive_now_is_rejected() -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(now=datetime(2026, 8, 2, 6, 0))
    assert excinfo.value.code == "naive_now"


def test_naive_expiry_is_rejected() -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(approval_expires_at=datetime(2026, 8, 2, 7, 0))
    assert excinfo.value.code == "naive_approval_expires_at"


def test_other_purposes_cannot_use_this_binding() -> None:
    binding = _build()
    with pytest.raises(CleanupBindingError) as excinfo:
        CleanupBinding(
            purpose="some_other_cleanup",
            contract_version=binding.contract_version,
            forecast_id=binding.forecast_id,
            sample_ref=binding.sample_ref,
            approval_id=binding.approval_id,
            approval_hash=binding.approval_hash,
            approval_expires_at=binding.approval_expires_at,
            approval_session_id=binding.approval_session_id,
            mission_id=binding.mission_id,
            account_mode=binding.account_mode,
            client_order_id=binding.client_order_id,
            lifecycle_correlation_id=binding.lifecycle_correlation_id,
        )
    assert excinfo.value.code == "unsupported_purpose"


@pytest.mark.parametrize(
    "field",
    ["approval_id", "mission_id", "sample_ref", "client_order_id", "account_mode"],
)
def test_blank_identity_is_rejected(field: str) -> None:
    with pytest.raises(CleanupBindingError) as excinfo:
        _build(**{field: "   "})
    assert excinfo.value.code == "missing_binding_field"
