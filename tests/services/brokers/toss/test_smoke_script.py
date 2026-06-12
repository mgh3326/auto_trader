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

    monkeypatch.setattr(toss_live_smoke.TossReadClient, "from_settings", lambda: FakeClient())

    code = await toss_live_smoke.run_preflight(["005930"])

    assert code == 0
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()
