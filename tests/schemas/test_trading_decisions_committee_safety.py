"""ROB-107 schema-level safety tests for committee sessions.

These tests guarantee that the contract layer rejects unsafe configurations
(``account_mode=kis_live``, ``automation.auto_execute=True``) before any
service or router code can act on them.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.trading_decisions import (
    CommitteeAutomation,
    SessionCreateRequest,
)


@pytest.mark.unit
def test_committee_automation_rejects_auto_execute_true():
    with pytest.raises(ValidationError) as exc_info:
        CommitteeAutomation(enabled=True, auto_approve_risk=True, auto_execute=True)
    assert "auto_execute must be False" in str(exc_info.value)


@pytest.mark.unit
def test_committee_automation_accepts_auto_execute_false():
    auto = CommitteeAutomation(enabled=True, auto_approve_risk=True, auto_execute=False)
    assert auto.auto_execute is False


@pytest.mark.unit
def test_committee_session_rejects_kis_live_account_mode():
    with pytest.raises(ValidationError) as exc_info:
        SessionCreateRequest(
            source_profile="committee_mock_paper",
            generated_at=datetime.now(UTC),
            account_mode="kis_live",
        )
    msg = str(exc_info.value)
    assert "committee sessions reject account_mode" in msg
    assert "kis_live" in msg


@pytest.mark.unit
def test_committee_session_rejects_db_simulated_account_mode():
    with pytest.raises(ValidationError) as exc_info:
        SessionCreateRequest(
            source_profile="committee_mock_paper",
            generated_at=datetime.now(UTC),
            account_mode="db_simulated",
        )
    assert "db_simulated" in str(exc_info.value)


@pytest.mark.unit
def test_committee_session_rejects_missing_account_mode():
    with pytest.raises(ValidationError) as exc_info:
        SessionCreateRequest(
            source_profile="committee_mock_paper",
            generated_at=datetime.now(UTC),
        )
    assert "committee sessions require account_mode" in str(exc_info.value)


@pytest.mark.unit
def test_committee_session_accepts_kis_mock():
    req = SessionCreateRequest(
        source_profile="committee_mock_paper",
        generated_at=datetime.now(UTC),
        account_mode="kis_mock",
    )
    assert req.account_mode == "kis_mock"


@pytest.mark.unit
def test_committee_session_accepts_alpaca_paper():
    req = SessionCreateRequest(
        source_profile="committee_mock_paper",
        generated_at=datetime.now(UTC),
        account_mode="alpaca_paper",
    )
    assert req.account_mode == "alpaca_paper"


@pytest.mark.unit
def test_non_committee_session_unaffected_by_account_mode_guard():
    # Non-committee profiles can still use any account_mode literal — the
    # guard only fires for source_profile=="committee_mock_paper".
    req = SessionCreateRequest(
        source_profile="operator",
        generated_at=datetime.now(UTC),
        account_mode="kis_live",
    )
    assert req.account_mode == "kis_live"


@pytest.mark.unit
def test_non_committee_session_with_no_account_mode_is_valid():
    req = SessionCreateRequest(
        source_profile="operator",
        generated_at=datetime.now(UTC),
    )
    assert req.account_mode is None
