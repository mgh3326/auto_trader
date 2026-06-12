from __future__ import annotations

import pytest

from scripts import toss_live_smoke


def test_main_without_preflight_exits_zero(capsys) -> None:
    code = toss_live_smoke.main([])

    assert code == 0
    assert "disabled" in capsys.readouterr().out


def test_main_disabled_env_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(["--preflight"])

    assert code == 0
    assert "TOSS_API_ENABLED" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_preflight_redacts_secret(monkeypatch, capsys) -> None:
    class FakeClient:
        async def accounts(self):
            return [type("Account", (), {"account_seq": 1})()]

        async def holdings(self):
            return type("Holdings", (), {"items": []})()

        async def prices(self, symbols):
            assert symbols == ["005930"]
            return []

        async def aclose(self):
            return None

    monkeypatch.setattr(
        toss_live_smoke.TossReadClient, "from_settings", lambda: FakeClient()
    )

    code = await toss_live_smoke.run_preflight(["005930"])

    assert code == 0
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()


def test_order_test_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")

    code = toss_live_smoke.main(["--order-test", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --order-test" in output
    assert "--quantity is required for --order-test" in output
    assert "--price is required for --order-test" in output


def test_confirm_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.setenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", "true")

    code = toss_live_smoke.main(["--confirm", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --confirm" in output
    assert "--quantity is required for --confirm" in output
    assert "--price is required for --confirm" in output


def test_order_test_disabled_when_toss_api_disabled(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--order-test",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    assert "TOSS_API_ENABLED is not truthy" in capsys.readouterr().out


def test_confirm_disabled_without_mutation_gate(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.delenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--confirm",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy" in output

