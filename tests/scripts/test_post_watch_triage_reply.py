from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import post_watch_triage_reply as cli


def test_build_message_wraps_triage_sections() -> None:
    body = "\n".join(
        [
            "## 알림 요약",
            "- BTC watch threshold fired.",
            "## 제안 verdict",
            "- approve dry_run preview only.",
            "## 결정 필요",
            "operator 세션에서: `session_context 최근 제안 승인 검토`",
        ]
    )

    msg = cli.build_message(
        symbol="KRW-BTC",
        market="crypto",
        event_uuid="event-1",
        triage_text=body,
    )

    assert "[watch triage] KRW-BTC" in msg
    assert "market: crypto" in msg
    assert "event: event-1" in msg
    assert "알림 요약" in msg
    assert "제안 verdict" in msg
    assert "결정 필요" in msg
    assert "session_context 최근 제안 승인 검토" in msg


@pytest.mark.asyncio
async def test_send_reply_uses_trade_notifier_mirror(monkeypatch) -> None:
    fake = SimpleNamespace(
        notify_agent_message=AsyncMock(return_value=True),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "configure_trade_notifier_from_settings", lambda **_: True)
    monkeypatch.setattr(cli, "get_trade_notifier", lambda: fake)
    monkeypatch.setattr(cli, "shutdown_trade_notifier", AsyncMock())

    ok = await cli.send_reply(
        symbol="KRW-BTC",
        market="crypto",
        event_uuid="event-1",
        triage_text="## 알림 요약\n## 제안 verdict",
    )

    assert ok is True
    fake.notify_agent_message.assert_awaited_once()
    kwargs = fake.notify_agent_message.await_args.kwargs
    assert kwargs["correlation_id"] == "event-1"
    assert kwargs["market_type"] == "crypto"
    assert kwargs["mirror_telegram"] is True


def test_main_reads_stdin_and_returns_zero(monkeypatch, capsys) -> None:
    def fake_asyncio_run(coro):
        coro.close()
        return True

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(read=lambda: "## 알림 요약"))
    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)

    rc = cli.main(
        [
            "--symbol",
            "KRW-BTC",
            "--market",
            "crypto",
            "--event-uuid",
            "event-1",
            "--text-file",
            "-",
        ]
    )

    assert rc == 0
    assert "sent" in capsys.readouterr().out
