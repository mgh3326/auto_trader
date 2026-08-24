"""Offline contract tests for the three-mode NHPLUG mock smoke CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.nhplug.errors import NHPlugMockBrokerRejected
from scripts import nhplug_mock_smoke as smoke

pytestmark = pytest.mark.unit

SENTINEL_KEY = "NHPLUG_KEY_MUST_NOT_LEAK_123"
SENTINEL_SECRET = "NHPLUG_SECRET_MUST_NOT_LEAK_456"
SENTINEL_ACCOUNT = "NHPLUG_ACCOUNT_MUST_NOT_LEAK_789"


def _write_minimal_env(path: Path, *, extra: str = "") -> Path:
    path.write_text(
        "\n".join(
            (
                f"NHPLUG_APP_KEY={SENTINEL_KEY}",
                f"NHPLUG_APP_SECRET={SENTINEL_SECRET}",
                f"NHPLUG_MOCK_ACCOUNT_NO={SENTINEL_ACCOUNT}",
                extra,
            )
        ),
        encoding="utf-8",
    )
    return path


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip())


def _assert_values_redacted(rendered: str) -> None:
    for value in (SENTINEL_KEY, SENTINEL_SECRET, SENTINEL_ACCOUNT):
        assert value not in rendered


def test_preflight_is_offline_and_redacts_all_minimal_env_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(tmp_path / ".env.nhplug-mock.native")
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")

    class _NetworkMustNotConstruct:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("preflight constructed a network client")

    monkeypatch.setattr(smoke, "NHPlugAuthClient", _NetworkMustNotConstruct)
    monkeypatch.setattr(smoke, "NHPlugMockClient", _NetworkMustNotConstruct)

    assert smoke.main(["--env-file", str(env_file), "--mode", "preflight"]) == 0
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    payload = json.loads(rendered)
    assert payload["network_calls"] == 0
    assert payload["required_env_keys"] == list(smoke.REQUIRED_ENV_KEYS)


def test_gate_off_fails_closed_before_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(tmp_path / ".env.nhplug-mock.native")
    monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)

    assert smoke.main(["--env-file", str(env_file)]) == 2
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    assert "NHPLUG_MOCK_ENABLED" in rendered


def test_prod_named_env_file_is_refused_without_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(tmp_path / ".env.prod.nhplug")
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")

    assert smoke.main(["--env-file", str(env_file)]) == 2
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    assert "filename contains 'prod'" in rendered


def test_expanded_env_file_is_refused_and_only_key_names_are_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(
        tmp_path / ".env.nhplug-mock.native",
        extra="DATABASE_URL=postgresql://must-not-be-loaded",
    )
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")

    assert smoke.main(["--env-file", str(env_file)]) == 2
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    assert "DATABASE_URL" in rendered
    assert "postgresql://must-not-be-loaded" not in rendered


def test_account_mode_verifies_type_then_reads_balance_without_leaking_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(tmp_path / ".env.nhplug-mock.native")
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    calls: list[str] = []

    class _FakeAuth:
        def __init__(self, **_: Any) -> None:
            calls.append("auth")

        async def get_access_token(self) -> str:
            return "unused-fake-token"

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            calls.append("client")

        async def list_accounts(self) -> dict[str, Any]:
            calls.append("accounts")
            return {
                "rsp_cd": "00000",
                "Output_0": [
                    {"acct_no": SENTINEL_ACCOUNT, "acct_type": "03"},
                    {"acct_no": "live-account-not-rendered", "acct_type": "01"},
                ],
            }

        async def fetch_balance(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("balance")
            assert kwargs["act_no"] == SENTINEL_ACCOUNT
            return {"rsp_cd": "00166", "Output_0": {"not_rendered": True}}

    monkeypatch.setattr(smoke, "NHPlugAuthClient", _FakeAuth)
    monkeypatch.setattr(smoke, "NHPlugMockClient", _FakeClient)

    assert (
        smoke.main(["--env-file", str(env_file), "--mode", "account", "--confirm-read"])
        == 0
    )
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    payload = json.loads(rendered)
    assert calls == ["auth", "client", "accounts", "balance"]
    assert payload["account_verification"] == {
        "account_type_counts": {"01": 1, "03": 1},
        "verified_mock_account_count": 1,
    }
    assert payload["balance"]["rsp_cd"] == "00166"


def test_quote_rejection_is_a_clear_nonzero_result_not_fake_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_minimal_env(tmp_path / ".env.nhplug-mock.native")
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")

    class _FakeAuth:
        def __init__(self, **_: Any) -> None:
            pass

        async def get_access_token(self) -> str:
            return "unused-fake-token"

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def list_accounts(self) -> dict[str, Any]:
            return {
                "rsp_cd": "00000",
                "Output_0": [{"acct_no": SENTINEL_ACCOUNT, "acct_type": "03"}],
            }

        async def fetch_quote(self, **_: Any) -> dict[str, Any]:
            raise NHPlugMockBrokerRejected(response_code="mock_quote_unavailable")

    monkeypatch.setattr(smoke, "NHPlugAuthClient", _FakeAuth)
    monkeypatch.setattr(smoke, "NHPlugMockClient", _FakeClient)

    assert (
        smoke.main(["--env-file", str(env_file), "--mode", "quote", "--confirm-read"])
        == 2
    )
    rendered = capsys.readouterr().out
    _assert_values_redacted(rendered)
    payload = json.loads(rendered)
    assert payload == {
        "broker_response_code": "mock_quote_unavailable",
        "error_type": "NHPlugMockBrokerRejected",
        "mode": "quote",
        "status": "failed",
    }


def test_only_the_three_readonly_modes_exist() -> None:
    parser = smoke.build_parser()
    mode_action = next(action for action in parser._actions if action.dest == "mode")
    assert set(mode_action.choices) == {"preflight", "account", "quote"}
