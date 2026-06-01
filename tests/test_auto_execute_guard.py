"""ROB-402 — live auto-execute is permanently blocked."""

import pytest

from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)


def test_live_account_blocked():
    with pytest.raises(AutoExecuteLiveBlocked):
        assert_auto_execute_account_allowed("auto_execute_mock", "kis_live")
    with pytest.raises(AutoExecuteLiveBlocked):
        assert_auto_execute_account_allowed("auto_execute_mock", "upbit_live")


def test_kiwoom_mock_unsupported():
    with pytest.raises(AutoExecuteUnsupported):
        assert_auto_execute_account_allowed("auto_execute_mock", "kiwoom_mock")


def test_kis_mock_allowed():
    assert_auto_execute_account_allowed("auto_execute_mock", "kis_mock")  # no raise


def test_non_auto_mode_is_noop():
    # any account is fine when not auto-executing
    assert_auto_execute_account_allowed("notify_only", "kis_live")  # no raise
