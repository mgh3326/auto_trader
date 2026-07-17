from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.fixture
def probe_module():
    return importlib.import_module("scripts.kis_mock_us_cash_probe")


class FakeTransport:
    def __init__(self, replies: dict[str, tuple[int | None, dict[str, Any]]]) -> None:
        self.replies = replies
        self.secret_values: tuple[str, ...] = ()
        self.calls: list[str] = []

    async def request(self, target):  # noqa: ANN001
        self.calls.append(target.key)
        status, payload = self.replies[target.key]
        return status, payload


@pytest.mark.asyncio
async def test_disabled_gate_exits_without_constructing_transport(
    monkeypatch, capsys, probe_module
):
    monkeypatch.delenv("KIS_MOCK_US_CASH_PROBE_ENABLED", raising=False)

    def unexpected_transport():
        raise AssertionError("disabled probe must not construct transport")

    exit_code = await probe_module.run_probe(
        selected="vtts3007",
        transport_factory=unexpected_transport,
    )

    assert exit_code == 0
    assert "disabled" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_missing_mock_credentials_reports_names_not_values(
    monkeypatch, capsys, probe_module
):
    monkeypatch.setenv("KIS_MOCK_US_CASH_PROBE_ENABLED", "true")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "secret-app-key")
    monkeypatch.delenv("KIS_MOCK_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_MOCK_ACCOUNT_NO", raising=False)

    exit_code = await probe_module.run_probe(selected="vtts3007")

    output = capsys.readouterr().out
    assert exit_code == 3
    assert "KIS_MOCK_APP_SECRET" in output
    assert "KIS_MOCK_ACCOUNT_NO" in output
    assert "secret-app-key" not in output


@pytest.mark.asyncio
async def test_success_reports_codes_and_parsed_usd_and_krw_fields(
    monkeypatch, capsys, probe_module
):
    monkeypatch.setenv("KIS_MOCK_US_CASH_PROBE_ENABLED", "true")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "key")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_NO", "12345678-01")
    transport = FakeTransport(
        {
            "vttc0869": (
                200,
                {
                    "rt_cd": "0",
                    "msg_cd": "0",
                    "msg1": "OK",
                    "output": {
                        "stck_cash_ord_psbl_amt": "2950965",
                        "usd_ord_psbl_amt": "101.25",
                        "usd_balance": "99.50",
                    },
                },
            )
        }
    )

    exit_code = await probe_module.run_probe(
        selected="vttc0869", transport_factory=lambda: transport
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "http_status" in output
    assert "rt_cd" in output
    assert "2950965" in output
    assert "101.25" in output
    assert transport.calls == ["vttc0869"]


@pytest.mark.asyncio
async def test_explicit_broker_rejection_is_normal_nonzero_verdict(
    monkeypatch, probe_module
):
    monkeypatch.setenv("KIS_MOCK_US_CASH_PROBE_ENABLED", "true")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "key")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_NO", "12345678-01")
    transport = FakeTransport(
        {
            "vtts3007": (
                200,
                {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "not supported"},
            )
        }
    )

    exit_code = await probe_module.run_probe(
        selected="vtts3007", transport_factory=lambda: transport
    )

    assert exit_code == 2


def test_redaction_masks_account_and_secret_values(probe_module):
    payload = {
        "CANO": "12345678",
        "authorization": "Bearer secret-token",
        "nested": {"appsecret": "secret-app-secret", "echo": "secret-token"},
    }

    redacted = probe_module.redact_payload(
        payload,
        secret_values=("12345678-01", "secret-token", "secret-app-secret"),
    )

    rendered = str(redacted)
    assert "12345678" not in rendered
    assert "secret-token" not in rendered
    assert "secret-app-secret" not in rendered
    assert "[REDACTED]" in rendered
