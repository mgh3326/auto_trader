# ROB-754 Watch + Triage Telegram Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver DB-backed watch trigger notifications and `claude -p` watch-triage replies to Telegram even when Discord delivery succeeds.

**Architecture:** Keep the existing Discord-first fallback behavior for generic trade notifications. Add opt-in mirror delivery paths for watch trigger embeds and agent/triage text replies so Discord and Telegram are attempted independently, with success returned when at least one configured channel receives the message. Move the runbook's raw Discord `curl` reply step to a small tested Python CLI that reuses `TradeNotifier`.

**Tech Stack:** Python 3.13+, FastAPI runtime settings, existing `TradeNotifier`, pytest, Ruff, ty, Claude Code command markdown, operator runbook shell.

## Global Constraints

- Do not hardcode credentials, webhook URLs, Telegram tokens, chat IDs, or operator paths.
- Use `get_trade_notifier()` and `configure_trade_notifier_from_settings()` instead of constructing separate notifier clients.
- Do not change broker, order, watch registration, order-intent, KIS, Upbit, or live execution paths.
- Keep `app/jobs/` schedule-agnostic; no new `@broker.task(...)` in `app/jobs/`.
- Watch trigger delivery may mark the event delivered when either Discord or Telegram succeeds; Telegram failure must not cause repeated watch alerts when Discord already succeeded.
- Triage remains read-only: no remote reply to order automation, no auto approval, no live order execution.
- Triage reply must include these visible sections in the Claude output: `알림 요약`, `제안 verdict`, and `결정 필요` when operator input is needed.
- When a decision is needed, include this exact one-liner: `operator 세션에서: \`session_context 최근 제안 승인 검토\``.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `app/core/config.py` | Accept `TELEGRAM_CHAT_IDS_STR` while preserving existing `TELEGRAM_CHAT_ID`. |
| `env.example` | Document both Telegram chat ID env forms. |
| `tests/test_config.py` | Lock Telegram chat ID parsing. |
| `app/monitoring/trade_notifier/notifier.py` | Add mirror dispatch for watch embeds and opt-in Telegram mirror for agent text replies. |
| `tests/test_trade_notifier.py` | Lock watch mirror and triage mirror transport behavior. |
| `scripts/post_watch_triage_reply.py` | Tested operator CLI: read triage result text, format a watch-triage reply, configure `TradeNotifier`, and mirror-send it. |
| `tests/scripts/test_post_watch_triage_reply.py` | Unit tests for CLI formatting, notifier wiring, and exit behavior. |
| `.claude/commands/crypto-alert-triage.md` | Require final Claude output sections and the operator one-liner. |
| `tests/test_crypto_alert_triage_command.py` | Assert command contract includes the required reply sections and one-liner. |
| `docs/runbooks/watch-alert-claude-triage.md` | Replace raw Discord curl in the poller with `scripts.post_watch_triage_reply`; document Telegram mirror expectations. |

---

### Task 0: Telegram Chat ID Env Compatibility

**Files:**
- Modify: `app/core/config.py:375`
- Modify: `env.example:80`
- Modify: `tests/test_config.py:139`

**Interfaces:**
- Consumes: existing `Settings.telegram_chat_id` and `Settings.telegram_chat_ids`.
- Produces: `Settings.telegram_chat_ids_str: str | None = None`; `Settings.telegram_chat_ids` returns comma-split `telegram_chat_ids_str` when set, else falls back to singular `telegram_chat_id`.

- [ ] **Step 1: Add failing config tests**

Append these tests to `TestConfigLoading` in `tests/test_config.py`:

```python
    def test_telegram_chat_ids_str_splits_multiple_ids(self):
        cfg = Settings(
            telegram_token="token",
            telegram_chat_id="legacy",
            telegram_chat_ids_str="111, 222,,333 ",
        )

        assert cfg.telegram_chat_ids == ["111", "222", "333"]

    def test_telegram_chat_ids_falls_back_to_single_chat_id(self):
        cfg = Settings(telegram_token="token", telegram_chat_id="legacy")

        assert cfg.telegram_chat_ids == ["legacy"]
```

- [ ] **Step 2: Run config tests to verify failure**

Run:

```bash
uv run pytest tests/test_config.py -k "telegram_chat_ids" -q
```

Expected: fails because `Settings` does not define `telegram_chat_ids_str`.

- [ ] **Step 3: Implement `TELEGRAM_CHAT_IDS_STR` support**

In `app/core/config.py`, add the plural env field below `telegram_chat_id`:

```python
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_chat_ids_str: str | None = None
```

Replace the `telegram_chat_ids` property with:

```python
    @property
    def telegram_chat_ids(self) -> list[str]:
        """Return configured Telegram chat IDs.

        `TELEGRAM_CHAT_IDS_STR` supports comma-separated multi-chat delivery.
        `TELEGRAM_CHAT_ID` remains supported as the legacy single-chat form.
        """
        if self.telegram_chat_ids_str:
            return [
                chat_id.strip()
                for chat_id in self.telegram_chat_ids_str.split(",")
                if chat_id.strip()
            ]
        if not self.telegram_chat_id:
            return []
        return [self.telegram_chat_id.strip()]
```

- [ ] **Step 4: Document the env compatibility**

In `env.example`, add this line below `TELEGRAM_CHAT_ID=`:

```bash
TELEGRAM_CHAT_IDS_STR= # optional comma-separated override, e.g. 123456789,987654321
```

- [ ] **Step 5: Verify config tests pass**

Run:

```bash
uv run pytest tests/test_config.py -k "telegram_chat_ids" -q
```

Expected: all selected tests pass.

---

### Task 1: Watch Trigger Mirror Delivery

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:194`
- Modify: `app/monitoring/trade_notifier/notifier.py:288`
- Modify: `tests/test_trade_notifier.py:2066`

**Interfaces:**
- Consumes: existing `TradeNotifier._send_to_discord_embed_single()`, `TradeNotifier._send_to_telegram()`, `TradeNotifier._has_telegram_delivery_config()`.
- Produces: `TradeNotifier._dispatch_mirror(discord_embed: DiscordEmbed | None, telegram_message: str, market_type: str | None = None, *, context: str) -> bool`.

- [ ] **Step 1: Update the existing watch test to expect Telegram mirror**

In `tests/test_trade_notifier.py`, update `test_notify_investment_watch_routes_by_market` so the Telegram mock is awaited when Discord succeeds:

```python
    assert ok is True
    md.assert_awaited_once()
    mt.assert_awaited_once()
    assert md.await_args.args[0]["title"].startswith("🔔 워치 트리거")
    assert "워치 트리거" in mt.await_args.args[0]
```

- [ ] **Step 2: Add a failing test for Telegram-only watch delivery**

Append this test after `test_notify_investment_watch_routes_by_market`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_investment_watch_delivers_to_telegram_without_discord_webhook(
    trade_notifier,
):
    from decimal import Decimal
    from uuid import uuid4

    from app.services.hermes_client import ReviewTriggerPayload

    payload = ReviewTriggerPayload(
        event_uuid=uuid4(),
        alert_uuid=uuid4(),
        source_report_uuid=uuid4(),
        source_item_uuid=uuid4(),
        correlation_id="c-telegram-only",
        kst_date="2026-07-07",
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal("68000"),
        threshold_key="k",
        intent="buy_review",
        action_mode="notify_only",
        current_value=Decimal("67500"),
        scanner_snapshot={},
        outcome="notified",
        invest_links=None,
        operator_action_guidance=None,
        price_guidance=None,
        planned_action=None,
        trigger_checklist=None,
    )

    trade_notifier.configure(bot_token="t", chat_ids=["1"], enabled=True)
    with (
        patch.object(
            trade_notifier,
            "_send_to_discord_embed_single",
            new=AsyncMock(return_value=True),
        ) as md,
        patch.object(
            trade_notifier,
            "_send_to_telegram",
            new=AsyncMock(return_value=True),
        ) as mt,
    ):
        ok = await trade_notifier.notify_investment_watch(payload)

    assert ok is True
    md.assert_not_awaited()
    mt.assert_awaited_once()
    assert "005930" in mt.await_args.args[0]
```

- [ ] **Step 3: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_trade_notifier.py -k "notify_investment_watch" -q
```

Expected: fails because `notify_investment_watch()` currently uses `_dispatch()`, which skips Telegram when Discord succeeds.

- [ ] **Step 4: Add `_dispatch_mirror()`**

In `app/monitoring/trade_notifier/notifier.py`, add this method immediately after `_dispatch()`:

```python
    async def _dispatch_mirror(
        self,
        discord_embed: DiscordEmbed | None,
        telegram_message: str,
        market_type: str | None = None,
        *,
        context: str,
    ) -> bool:
        """Attempt Discord and Telegram independently for mirror notifications."""
        if not self._enabled:
            logger.info(
                "Notification mirror result: context=%s market_type=%s discord=%s telegram=%s",
                context,
                market_type,
                "skipped(notifier_disabled)",
                "skipped(notifier_disabled)",
            )
            return False

        delivered = False
        discord_result = "skipped(no_discord_webhook)"
        telegram_result = "skipped(no_telegram_config)"

        if market_type and discord_embed:
            webhook_url = self._get_webhook_for_market_type(market_type)
            if webhook_url:
                try:
                    discord_success = await self._send_to_discord_embed_single(
                        discord_embed, webhook_url
                    )
                    discord_result = "success" if discord_success else "failed"
                    delivered = delivered or discord_success
                except Exception:
                    discord_result = "failed(exception)"
                    logger.exception(
                        "Notification mirror Discord send failed: context=%s market_type=%s",
                        context,
                        market_type,
                    )

        if telegram_message:
            if self._has_telegram_delivery_config():
                try:
                    telegram_success = await self._send_to_telegram(telegram_message)
                    telegram_result = "success" if telegram_success else "failed"
                    delivered = delivered or telegram_success
                except Exception:
                    telegram_result = "failed(exception)"
                    logger.exception(
                        "Notification mirror Telegram send failed: context=%s market_type=%s",
                        context,
                        market_type,
                    )

        logger.info(
            "Notification mirror result: context=%s market_type=%s discord=%s telegram=%s",
            context,
            market_type,
            discord_result,
            telegram_result,
        )
        return delivered
```

- [ ] **Step 5: Switch `notify_investment_watch()` to mirror dispatch**

Replace the final line of `notify_investment_watch()`:

```python
        return await self._dispatch(embed, telegram_msg, payload.market)
```

with:

```python
        return await self._dispatch_mirror(
            embed, telegram_msg, payload.market, context="investment_watch"
        )
```

- [ ] **Step 6: Verify watch mirror tests pass**

Run:

```bash
uv run pytest tests/test_trade_notifier.py -k "notify_investment_watch" -q
```

Expected: all selected tests pass.

---

### Task 2: Agent/Triage Text Reply Mirror Option

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:579`
- Modify: `tests/test_trade_notifier.py:879`

**Interfaces:**
- Consumes: existing `TradeNotifier.notify_agent_message(message, parse_mode="Markdown", correlation_id=None, market_type=None, skip_discord=False)`.
- Produces: additive optional parameter `mirror_telegram: bool = False`; default behavior remains fallback-only.

- [ ] **Step 1: Add a failing mirror test for agent messages**

Append this test near the existing `notify_agent_message` tests:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_agent_message_mirror_telegram_sends_both_on_discord_success(
    trade_notifier,
):
    trade_notifier.configure(
        bot_token="t",
        chat_ids=["1"],
        enabled=True,
        discord_webhook_crypto="https://discord.com/api/webhooks/crypto",
    )

    with (
        patch.object(
            trade_notifier,
            "_send_to_discord_content_single",
            new=AsyncMock(return_value=True),
        ) as md,
        patch.object(
            trade_notifier,
            "_send_to_telegram",
            new=AsyncMock(return_value=True),
        ) as mt,
    ):
        ok = await trade_notifier.notify_agent_message(
            "알림 요약\n제안 verdict\n결정 필요",
            correlation_id="event-1",
            market_type="crypto",
            mirror_telegram=True,
        )

    assert ok is True
    md.assert_awaited_once_with(
        "알림 요약\n제안 verdict\n결정 필요",
        "https://discord.com/api/webhooks/crypto",
    )
    mt.assert_awaited_once_with(
        "알림 요약\n제안 verdict\n결정 필요", parse_mode="Markdown"
    )
```

- [ ] **Step 2: Add a failing test for Discord success with missing Telegram config**

Append:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_agent_message_mirror_returns_true_when_telegram_unconfigured(
    trade_notifier,
):
    trade_notifier.configure(
        bot_token="",
        chat_ids=[],
        enabled=True,
        discord_webhook_crypto="https://discord.com/api/webhooks/crypto",
    )

    with patch.object(
        trade_notifier,
        "_send_to_discord_content_single",
        new=AsyncMock(return_value=True),
    ) as md:
        ok = await trade_notifier.notify_agent_message(
            "triage text",
            correlation_id="event-2",
            market_type="crypto",
            mirror_telegram=True,
        )

    assert ok is True
    md.assert_awaited_once()
```

- [ ] **Step 3: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_trade_notifier.py -k "notify_agent_message_mirror" -q
```

Expected: fails because `notify_agent_message()` does not accept `mirror_telegram`.

- [ ] **Step 4: Add the optional parameter**

Change the signature in `app/monitoring/trade_notifier/notifier.py`:

```python
    async def notify_agent_message(
        self,
        message: str,
        parse_mode: str = "Markdown",
        *,
        correlation_id: str | None = None,
        market_type: str | None = None,
        skip_discord: bool = False,
        mirror_telegram: bool = False,
    ) -> bool:
```

- [ ] **Step 5: Preserve fallback behavior when `mirror_telegram=False`**

Inside the `if webhook_url:` block, replace the current early-return block:

```python
                if discord_success:
                    discord_result = "success"
                    telegram_result = "skipped(fallback_not_needed)"
                    logger.info(
                        "Agent gateway mirror result: correlation_id=%s discord=%s telegram=%s",
                        correlation_id,
                        discord_result,
                        telegram_result,
                    )
                    return True
```

with:

```python
                if discord_success:
                    discord_result = "success"
                    if not mirror_telegram:
                        telegram_result = "skipped(fallback_not_needed)"
                        logger.info(
                            "Agent gateway mirror result: correlation_id=%s discord=%s telegram=%s",
                            correlation_id,
                            discord_result,
                            telegram_result,
                        )
                        return True
```

- [ ] **Step 6: Return Discord success when Telegram mirror is unavailable**

Replace the no-Telegram-config return block:

```python
            if not self._has_telegram_delivery_config():
                telegram_result = "skipped(no_telegram_config)"
                logger.info(
                    "Agent gateway mirror result: correlation_id=%s discord=%s telegram=%s",
                    correlation_id,
                    discord_result,
                    telegram_result,
                )
                return False
```

with:

```python
            if not self._has_telegram_delivery_config():
                telegram_result = "skipped(no_telegram_config)"
                logger.info(
                    "Agent gateway mirror result: correlation_id=%s discord=%s telegram=%s",
                    correlation_id,
                    discord_result,
                    telegram_result,
                )
                return discord_result == "success"
```

- [ ] **Step 7: Return success if either channel worked**

Replace the final Telegram return:

```python
            return telegram_success
```

with:

```python
            return telegram_success or discord_result == "success"
```

- [ ] **Step 8: Verify existing fallback tests plus new mirror tests**

Run:

```bash
uv run pytest tests/test_trade_notifier.py -k "notify_agent_message" -q
```

Expected: all selected tests pass, including old fallback-only behavior.

---

### Task 3: Add Tested Triage Reply Sender CLI

**Files:**
- Create: `scripts/post_watch_triage_reply.py`
- Create: `tests/scripts/test_post_watch_triage_reply.py`

**Interfaces:**
- Consumes: stdin or `--text-file` containing Claude `.result` text.
- Produces: CLI `uv run python -m scripts.post_watch_triage_reply --symbol SYMBOL --market MARKET --event-uuid UUID --text-file -`.
- Calls: `get_trade_notifier().notify_agent_message(..., mirror_telegram=True)`.

- [ ] **Step 1: Write CLI unit tests**

Create `tests/scripts/test_post_watch_triage_reply.py`:

```python
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
            'operator 세션에서: `session_context 최근 제안 승인 검토`',
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
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(read=lambda: "## 알림 요약"))
    monkeypatch.setattr(cli, "asyncio", SimpleNamespace(run=lambda coro: True))

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
```

- [ ] **Step 2: Run CLI tests to verify failure**

Run:

```bash
uv run pytest tests/scripts/test_post_watch_triage_reply.py -q
```

Expected: fails with `ImportError` because `scripts.post_watch_triage_reply` does not exist.

- [ ] **Step 3: Implement the CLI**

Create `scripts/post_watch_triage_reply.py`:

```python
"""Send watch-alert Claude triage replies through TradeNotifier.

This CLI is operator-host safe: it sends notifications only. It does not
mutate broker, order, watch, or session-context state.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
    shutdown_trade_notifier,
)


def build_message(
    *,
    symbol: str,
    market: str,
    event_uuid: str,
    triage_text: str,
) -> str:
    body = triage_text.strip()
    return "\n".join(
        [
            f"[watch triage] {symbol}",
            f"market: {market}",
            f"event: {event_uuid}",
            "",
            body,
        ]
    ).strip()


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


async def send_reply(
    *,
    symbol: str,
    market: str,
    event_uuid: str,
    triage_text: str,
) -> bool:
    configured = configure_trade_notifier_from_settings(
        log_context="Watch triage notifier"
    )
    if not configured:
        return False

    message = build_message(
        symbol=symbol,
        market=market,
        event_uuid=event_uuid,
        triage_text=triage_text,
    )
    try:
        return await get_trade_notifier().notify_agent_message(
            message,
            correlation_id=event_uuid,
            market_type=market,
            mirror_telegram=True,
        )
    finally:
        await shutdown_trade_notifier(log_context="Watch triage notifier")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror a watch-alert Claude triage reply to Discord and Telegram."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--event-uuid", required=True)
    parser.add_argument(
        "--text-file",
        default="-",
        help="File containing Claude result text; '-' reads stdin.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    triage_text = _read_text(args.text_file)
    ok = asyncio.run(
        send_reply(
            symbol=args.symbol,
            market=args.market,
            event_uuid=args.event_uuid,
            triage_text=triage_text,
        )
    )
    if ok:
        print("sent")
        return 0
    print("not sent", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Verify CLI tests pass**

Run:

```bash
uv run pytest tests/scripts/test_post_watch_triage_reply.py -q
```

Expected: all tests pass.

---

### Task 4: Lock Claude Triage Reply Shape

**Files:**
- Modify: `.claude/commands/crypto-alert-triage.md:34`
- Modify: `tests/test_crypto_alert_triage_command.py:11`

**Interfaces:**
- Consumes: existing Claude command final assistant message.
- Produces: final `.result` with `알림 요약`, `제안 verdict`, optional `결정 필요`, and the operator one-liner.

- [ ] **Step 1: Add command contract test**

Append to `tests/test_crypto_alert_triage_command.py`:

```python
def test_command_requires_telegram_safe_triage_sections():
    body = CMD.read_text(encoding="utf-8")
    for token in (
        "알림 요약",
        "제안 verdict",
        "결정 필요",
        "operator 세션에서: `session_context 최근 제안 승인 검토`",
    ):
        assert token in body, f"커맨드에 {token} 누락"
```

- [ ] **Step 2: Run command tests to verify failure**

Run:

```bash
uv run pytest tests/test_crypto_alert_triage_command.py -q
```

Expected: fails because the exact section contract and one-liner are not present.

- [ ] **Step 3: Update the Claude command output section**

In `.claude/commands/crypto-alert-triage.md`, replace the bullets under `## 4. 출력 (둘 다 수행)` item 2 with:

```markdown
2. 마지막 assistant 메시지로 Discord와 Telegram 양쪽에 그대로 전달 가능한 간결 요약을 낸다(이게 `--output-format json`의 `.result`로 회신된다). 아래 섹션 제목을 유지한다:
   - `## 알림 요약`
     - 발화 symbol/market/조건/current_value를 한 줄로 요약.
     - 트리거가 여전히 유효한지 한 줄로 요약.
   - `## 제안 verdict`
     - `approve_dry_run`, `wait`, `reject`, `needs_human_review` 중 하나.
     - 핵심 근거 2~3개.
     - 제안 dry_run 실행안(side/수량/지정가)이 있으면 적고, 없으면 `dry_run 제안 없음`으로 적는다.
   - `## 결정 필요`
     - 운영자 확인이 필요한 경우에만 이 섹션을 포함한다.
     - 섹션을 포함할 때 마지막 줄에 `operator 세션에서: \`session_context 최근 제안 승인 검토\``를 포함한다.
```

- [ ] **Step 4: Verify command tests pass**

Run:

```bash
uv run pytest tests/test_crypto_alert_triage_command.py -q
```

Expected: all tests pass.

---

### Task 5: Update Watch-Triage Runbook Poller

**Files:**
- Modify: `docs/runbooks/watch-alert-claude-triage.md:33`
- Modify: `docs/runbooks/watch-alert-claude-triage.md:119`
- Modify: `docs/runbooks/watch-alert-claude-triage.md:336`

**Interfaces:**
- Consumes: `scripts.post_watch_triage_reply` from Task 3.
- Produces: runbook poller that marks an event seen only after Claude succeeds and the new sender CLI returns exit code 0.

- [ ] **Step 1: Document Telegram requirement**

In the prerequisites table, add this row after the Discord webhook row:

```markdown
| Telegram env | `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` 또는 `TELEGRAM_CHAT_IDS_STR` — `TradeNotifier`가 읽는 기존 운영자 채팅 |
| Discord env | `DISCORD_WEBHOOK_CRYPTO`/`DISCORD_WEBHOOK_KR`/`DISCORD_WEBHOOK_US`/`DISCORD_WEBHOOK_ALERTS` 중 market에 맞는 기존 webhook |
```

- [ ] **Step 2: Remove the poller-only Discord webhook requirement**

In the poller script block, remove this line:

```bash
DISCORD_WEBHOOK="${DISCORD_TRIAGE_WEBHOOK:?DISCORD_TRIAGE_WEBHOOK 미설정}"
```

Add this comment after `MARKET="${TRIAGE_MARKET:-crypto}"`:

```bash
# Discord/Telegram routing is read from the app's TradeNotifier settings.
```

- [ ] **Step 3: Replace the raw Discord curl block**

In the poller script block, replace:

```bash
    # Discord 회신 — 실패 시 seen 기록·워터마크 전진 없이 continue (at-least-once)
    curl -fsS -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg c "**[watch triage] $(jq -r .symbol <<<"$ev")**"$'\n'"$text" '{content:$c}')" \
      "$DISCORD_WEBHOOK" >/dev/null \
      || { echo "discord post 실패(재시도 예정): $uuid" >&2; continue; }
```

with:

```bash
    # Discord + Telegram 회신 — 실패 시 seen 기록·워터마크 전진 없이 continue (at-least-once)
    printf '%s\n' "$text" | uv run python -m scripts.post_watch_triage_reply \
      --symbol "$(jq -r .symbol <<<"$ev")" \
      --market "$(jq -r .market <<<"$ev")" \
      --event-uuid "$uuid" \
      --text-file - >/dev/null \
      || { echo "triage reply post 실패(재시도 예정): $uuid" >&2; continue; }
```

- [ ] **Step 4: Update the environment variable example**

Replace the old triage webhook example:

```bash
export DISCORD_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."   # 실제 값으로 교체
```

with:

```bash
export DISCORD_WEBHOOK_CRYPTO="https://discord.com/api/webhooks/..."  # market=crypto 예시
export TELEGRAM_TOKEN="..."                                         # 기존 운영 env 사용
export TELEGRAM_CHAT_IDS_STR="123456789"                            # multi-chat 운영 env
```

- [ ] **Step 5: Update the live triage expected result**

Replace the Step 5 expected Discord-only bullet:

```markdown
- Discord 채널에 `**[watch triage] KRW-BTC**` (또는 해당 symbol) 메시지 + 트리아지 분석 도착.
```

with:

```markdown
- Discord 채널과 Telegram 운영자 채팅에 `[watch triage] KRW-BTC` (또는 해당 symbol) 메시지 + 트리아지 분석 도착.
```

- [ ] **Step 6: Verify docs mention the new sender**

Run:

```bash
rg -n "post_watch_triage_reply|Telegram|triage reply post" docs/runbooks/watch-alert-claude-triage.md
```

Expected: output includes the new CLI, Telegram prerequisite, and failure message.

---

### Task 6: Final Verification

**Files:**
- Verify: all files changed in Tasks 1-5.

**Interfaces:**
- Consumes: test suite commands.
- Produces: evidence that notification mirror behavior, CLI sender, and command contract are locked.

- [ ] **Step 1: Run focused unit tests**

Run:

```bash
uv run pytest \
  tests/test_config.py \
  tests/test_trade_notifier.py \
  tests/scripts/test_post_watch_triage_reply.py \
  tests/test_crypto_alert_triage_command.py \
  -k "telegram_chat_ids or notify_investment_watch or notify_agent_message or post_watch_triage_reply or crypto_alert_triage_command" \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run Hermes python-direct smoke tests**

Run:

```bash
uv run pytest tests/test_hermes_client.py -k "python_direct" -q
```

Expected: all selected tests pass, confirming watch scanner delivery mapping still treats any successful notifier send as success.

- [ ] **Step 3: Run lint and formatting checks**

Run:

```bash
uv run ruff check \
  app/core/config.py \
  app/monitoring/trade_notifier/notifier.py \
  scripts/post_watch_triage_reply.py \
  tests/test_config.py \
  tests/test_trade_notifier.py \
  tests/scripts/test_post_watch_triage_reply.py \
  tests/test_crypto_alert_triage_command.py
uv run ruff format --check \
  app/core/config.py \
  app/monitoring/trade_notifier/notifier.py \
  scripts/post_watch_triage_reply.py \
  tests/test_config.py \
  tests/test_trade_notifier.py \
  tests/scripts/test_post_watch_triage_reply.py \
  tests/test_crypto_alert_triage_command.py
```

Expected: both commands exit 0.

- [ ] **Step 4: Run type check on changed runtime modules**

Run:

```bash
uv run ty check app/core/config.py app/monitoring/trade_notifier/notifier.py scripts/post_watch_triage_reply.py
```

Expected: exits 0.

- [ ] **Step 5: Manual dry-run command assembly**

Run:

```bash
printf '%s\n' '## 알림 요약
- sample
## 제안 verdict
- wait' | uv run python -m scripts.post_watch_triage_reply \
  --symbol KRW-BTC \
  --market crypto \
  --event-uuid dry-run-event \
  --text-file -
```

Expected in a configured operator environment: prints `sent` and sends both Discord and Telegram. Expected in an unconfigured local test environment: prints `not sent` and exits 2 without printing secrets.

---

## Acceptance Criteria

- `notify_investment_watch()` attempts Telegram even when Discord succeeds.
- `notify_investment_watch()` returns success if Telegram succeeds and the market-specific Discord webhook is absent.
- Default `notify_agent_message()` behavior remains Discord-first fallback-only.
- `notify_agent_message(..., mirror_telegram=True)` attempts both Discord and Telegram and returns success if either channel succeeds.
- The watch-triage runbook no longer posts the Claude result via a raw Discord-only `curl`.
- The `crypto-alert-triage` command requires `알림 요약`, `제안 verdict`, optional `결정 필요`, and the operator one-liner.
- No broker/order/watch/order-intent/live trading mutation path is introduced.
