"""Tests for scripts.sentry_profiling_canary — the operator-only,
scheduleless Sentry profiling canary CLI.

Hard invariants under test:
- default mode never calls init_sentry / sentry_sdk and performs no network.
- --send without --confirm (and vice versa) fails closed (exit 2), never
  silently degrades to dry mode and never sends.
- --send --confirm with no DSN configured fails closed (exit 1) rather than
  faking a success.
- --send --confirm with a DSN configured uses a fixed transaction name/op —
  never user input — and this test never performs real network I/O (Sentry
  init and sentry_sdk itself are mocked; the repo-wide ROB-1880 socket guard
  would also fail the test if anything tried).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts import sentry_profiling_canary as canary


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    monkeypatch.setattr(canary.settings, "SENTRY_DSN", "")
    monkeypatch.setattr(canary.settings, "SENTRY_TRACES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(canary.settings, "SENTRY_PROFILES_SAMPLE_RATE", 1.0)


@pytest.mark.unit
def test_default_mode_prints_diagnostics_and_never_touches_sentry(capsys):
    with patch.object(canary, "init_sentry") as mock_init_sentry:
        exit_code = canary.main([])

    assert exit_code == 0
    mock_init_sentry.assert_not_called()
    printed = json.loads(capsys.readouterr().out)
    assert printed["process_kind"] == canary.PROCESS_KIND
    assert printed["enabled"] is False


@pytest.mark.unit
@pytest.mark.parametrize("argv", [["--send"], ["--confirm"]])
def test_incomplete_double_intent_fails_closed(argv, capsys):
    with patch.object(canary, "init_sentry") as mock_init_sentry:
        exit_code = canary.main(argv)

    assert exit_code == 2
    mock_init_sentry.assert_not_called()


@pytest.mark.unit
def test_send_confirm_without_dsn_fails_closed_never_fakes_success(capsys):
    exit_code = canary.main(["--send", "--confirm"])

    assert exit_code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["enabled"] is False


@pytest.mark.unit
def test_send_confirm_with_dsn_uses_fixed_transaction_and_bounded_flush(
    monkeypatch, capsys
):
    monkeypatch.setattr(canary.settings, "SENTRY_DSN", "fake-dsn-fixture-not-a-secret")

    fake_transaction = MagicMock()
    fake_transaction.event_id = "deadbeefcafefeed0123456789abcde"
    fake_transaction.sampled = True
    fake_transaction.__enter__ = MagicMock(return_value=fake_transaction)
    fake_transaction.__exit__ = MagicMock(return_value=False)

    fake_sentry_sdk = MagicMock()
    fake_sentry_sdk.start_transaction.return_value = fake_transaction

    with (
        patch.object(canary, "init_sentry", return_value=True) as mock_init_sentry,
        patch.dict("sys.modules", {"sentry_sdk": fake_sentry_sdk}),
    ):
        exit_code = canary.main(["--send", "--confirm", "--timeout", "9999"])

    assert exit_code == 0
    mock_init_sentry.assert_called_once_with(service_name=canary.SERVICE_NAME)
    fake_sentry_sdk.start_transaction.assert_called_once_with(
        name=canary.FIXED_TRANSACTION_NAME, op=canary.FIXED_TRANSACTION_OP
    )
    # --timeout 9999 must be clamped, never passed through raw.
    flush_args, flush_kwargs = fake_sentry_sdk.flush.call_args
    assert flush_kwargs["timeout"] <= canary.MAX_FLUSH_TIMEOUT_SECONDS

    printed = json.loads(capsys.readouterr().out)
    assert printed["transaction_name"] == canary.FIXED_TRANSACTION_NAME
    assert printed["event_id"] == "deadbeefcafefeed0123456789abcde"
    assert "SENTRY_DSN" not in printed
    assert "fake-dsn-fixture-not-a-secret" not in json.dumps(printed)


@pytest.mark.unit
def test_send_confirm_when_init_sentry_returns_false_fails_closed(
    monkeypatch, capsys
):
    monkeypatch.setattr(canary.settings, "SENTRY_DSN", "fake-dsn-fixture-not-a-secret")

    with patch.object(canary, "init_sentry", return_value=False) as mock_init_sentry:
        exit_code = canary.main(["--send", "--confirm"])

    assert exit_code == 1
    mock_init_sentry.assert_called_once()
