# Trade Notifier Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 1,882-line `trade_notifier.py` into a package with separate transport, formatter, and types modules while preserving the public API exactly.

**Architecture:** Convert `app/monitoring/trade_notifier.py` into a package `app/monitoring/trade_notifier/` with 5 focused modules. The public-facing `TradeNotifier` class and `get_trade_notifier()` function remain importable from `app.monitoring.trade_notifier`. Internal formatters and transports are split by responsibility.

**Tech Stack:** Python 3.13+, httpx, pytest, pytest-asyncio

---

## File Structure

After refactoring, the directory layout will be:

```
app/monitoring/trade_notifier/
├── __init__.py              # Re-exports TradeNotifier, get_trade_notifier, DiscordEmbed, DiscordField
├── types.py                 # TypedDict definitions (DiscordField, DiscordEmbed), color/emoji constants
├── transports.py            # Telegram + Discord HTTP transport functions
├── formatters_discord.py    # All Discord embed formatter functions
├── formatters_telegram.py   # All Telegram message formatter functions
└── notifier.py              # TradeNotifier class (singleton, configure, shutdown, public notify_* methods)
```

**Key design decisions:**
- `types.py` holds `DiscordField`, `DiscordEmbed`, color constants, and emoji mapping — shared by both formatter modules
- `transports.py` contains stateless async functions that accept an `httpx.AsyncClient` and destination config — no singleton dependency
- `formatters_discord.py` contains pure functions (no async, no HTTP) that return `DiscordEmbed` dicts
- `formatters_telegram.py` contains pure functions (no async, no HTTP) that return formatted strings
- `notifier.py` holds the `TradeNotifier` class, which delegates to formatters and transports
- `__init__.py` re-exports everything needed by external callers so existing imports work unchanged

**Callers that import from `app.monitoring.trade_notifier`:**
- `app/services/openclaw_client.py` — `get_trade_notifier`
- `app/services/toss_notification_service.py` — `TradeNotifier`, `get_trade_notifier`
- `app/jobs/analyze.py` — `get_trade_notifier`
- `app/jobs/daily_scan.py` — `get_trade_notifier`
- `app/jobs/kis_trading.py` — `get_trade_notifier`
- `app/jobs/screener.py` — `get_trade_notifier`
- `app/main.py` — `get_trade_notifier`
- `tests/test_trade_notifier.py` — `TradeNotifier`, `get_trade_notifier`
- `tests/test_toss_notification.py` — `TradeNotifier`

All these imports will continue to work because `__init__.py` re-exports the same names.

---

## Task 1: Create `types.py` — Shared Types and Constants

**Files:**
- Create: `app/monitoring/trade_notifier/types.py`
- Test: `tests/test_trade_notifier_types.py`

This module extracts `DiscordField`, `DiscordEmbed`, color constants, and emoji mappings from the monolith. Pure data — no logic.

- [ ] **Step 1: Write the test for types**

```python
# tests/test_trade_notifier_types.py
"""Tests for trade notifier shared types and constants."""

import pytest

from app.monitoring.trade_notifier.types import (
    COLORS,
    DECISION_EMOJI,
    DECISION_TEXT,
    DiscordEmbed,
    DiscordField,
)


@pytest.mark.unit
class TestColors:
    def test_buy_color_is_green(self):
        assert COLORS["buy"] == 0x00FF00

    def test_sell_color_is_red(self):
        assert COLORS["sell"] == 0xFF0000

    def test_cancel_color_is_yellow(self):
        assert COLORS["cancel"] == 0xFFFF00

    def test_analysis_color_is_blue(self):
        assert COLORS["analysis"] == 0x0000FF

    def test_summary_color_is_cyan(self):
        assert COLORS["summary"] == 0x00FFFF

    def test_failure_color_is_orange(self):
        assert COLORS["failure"] == 0xFF6600


@pytest.mark.unit
class TestDecisionEmoji:
    def test_buy_emoji(self):
        assert DECISION_EMOJI["buy"] == "\U0001f7e2"  # green circle

    def test_hold_emoji(self):
        assert DECISION_EMOJI["hold"] == "\U0001f7e1"  # yellow circle

    def test_sell_emoji(self):
        assert DECISION_EMOJI["sell"] == "\U0001f534"  # red circle

    def test_unknown_returns_default(self):
        assert DECISION_EMOJI.get("unknown", "\u26aa") == "\u26aa"


@pytest.mark.unit
class TestDecisionText:
    def test_buy_text(self):
        assert DECISION_TEXT["buy"] == "매수"

    def test_hold_text(self):
        assert DECISION_TEXT["hold"] == "보유"

    def test_sell_text(self):
        assert DECISION_TEXT["sell"] == "매도"


@pytest.mark.unit
class TestTypedDicts:
    def test_discord_field_creation(self):
        field: DiscordField = {"name": "종목", "value": "삼성전자", "inline": True}
        assert field["name"] == "종목"

    def test_discord_embed_creation(self):
        embed: DiscordEmbed = {
            "title": "test",
            "description": "desc",
            "color": 0x00FF00,
            "fields": [],
        }
        assert embed["color"] == 0x00FF00
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_notifier_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.monitoring.trade_notifier.types'`

- [ ] **Step 3: Create the `trade_notifier/` package directory**

```bash
mkdir -p app/monitoring/trade_notifier
```

- [ ] **Step 4: Write `types.py`**

```python
# app/monitoring/trade_notifier/types.py
"""Shared types and constants for trade notifications."""

from __future__ import annotations

from typing import TypedDict


class DiscordField(TypedDict):
    name: str
    value: str
    inline: bool


class DiscordEmbed(TypedDict):
    title: str
    description: str
    color: int
    fields: list[DiscordField]


# Color constants used by formatters
COLORS: dict[str, int] = {
    "buy": 0x00FF00,
    "sell": 0xFF0000,
    "cancel": 0xFFFF00,
    "analysis": 0x0000FF,
    "summary": 0x00FFFF,
    "failure": 0xFF6600,
    "hold": 0xFFFF00,
    "default": 0x0000FF,
}

# Decision → emoji mapping
DECISION_EMOJI: dict[str, str] = {
    "buy": "\U0001f7e2",   # 🟢
    "hold": "\U0001f7e1",  # 🟡
    "sell": "\U0001f534",  # 🔴
}

# Decision → Korean text
DECISION_TEXT: dict[str, str] = {
    "buy": "매수",
    "hold": "보유",
    "sell": "매도",
}
```

- [ ] **Step 5: Create an empty `__init__.py` for now**

```python
# app/monitoring/trade_notifier/__init__.py
"""Trade notification system with Telegram and Discord integration."""
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_notifier_types.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add app/monitoring/trade_notifier/__init__.py app/monitoring/trade_notifier/types.py tests/test_trade_notifier_types.py
git commit -m "refactor: extract trade notifier types and constants into types.py"
```

---

## Task 2: Create `transports.py` — HTTP Transport Functions

**Files:**
- Create: `app/monitoring/trade_notifier/transports.py`
- Test: `tests/test_trade_notifier_transports.py`

Extract the 4 transport functions from `TradeNotifier` into stateless async functions. Each function accepts an `httpx.AsyncClient` plus the destination config (URLs, tokens, chat IDs) as explicit arguments.

- [ ] **Step 1: Write the test**

```python
# tests/test_trade_notifier_transports.py
"""Tests for trade notifier transport functions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.monitoring.trade_notifier.transports import (
    send_discord_content_single,
    send_discord_embed_single,
    send_telegram,
)


@pytest.fixture
def mock_http_client():
    client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post.return_value = response
    return client


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendTelegram:
    async def test_sends_to_all_chat_ids(self, mock_http_client):
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="test_token",
            chat_ids=["111", "222"],
            text="hello",
        )
        assert result is True
        assert mock_http_client.post.call_count == 2

    async def test_returns_false_when_all_fail(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("network error")
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="test_token",
            chat_ids=["111"],
            text="hello",
        )
        assert result is False

    async def test_sends_correct_payload(self, mock_http_client):
        await send_telegram(
            http_client=mock_http_client,
            bot_token="tok123",
            chat_ids=["999"],
            text="msg",
            parse_mode="Markdown",
        )
        call_kwargs = mock_http_client.post.call_args
        assert "tok123" in call_kwargs.args[0]
        payload = call_kwargs.kwargs["json"]
        assert payload["chat_id"] == "999"
        assert payload["text"] == "msg"
        assert payload["parse_mode"] == "Markdown"

    async def test_returns_true_if_at_least_one_succeeds(self, mock_http_client):
        """Partial success: first chat fails, second succeeds."""
        mock_http_client.post.side_effect = [
            Exception("fail"),
            MagicMock(raise_for_status=MagicMock()),
        ]
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="tok",
            chat_ids=["a", "b"],
            text="msg",
        )
        assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendDiscordEmbedSingle:
    async def test_sends_embed_payload(self, mock_http_client):
        embed = {
            "title": "test",
            "description": "desc",
            "color": 0x00FF00,
            "fields": [],
        }
        result = await send_discord_embed_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/api/webhooks/123",
            embed=embed,
        )
        assert result is True
        call_kwargs = mock_http_client.post.call_args
        assert call_kwargs.args[0] == "https://discord.com/api/webhooks/123"
        assert call_kwargs.kwargs["json"]["embeds"] == [embed]

    async def test_returns_false_on_failure(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("err")
        result = await send_discord_embed_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/x",
            embed={"title": "t", "description": "d", "color": 0, "fields": []},
        )
        assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendDiscordContentSingle:
    async def test_sends_content_payload(self, mock_http_client):
        result = await send_discord_content_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/api/webhooks/456",
            content="hello",
        )
        assert result is True
        call_kwargs = mock_http_client.post.call_args
        assert call_kwargs.kwargs["json"]["content"] == "hello"

    async def test_returns_false_on_failure(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("err")
        result = await send_discord_content_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/x",
            content="fail",
        )
        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_notifier_transports.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write `transports.py`**

```python
# app/monitoring/trade_notifier/transports.py
"""HTTP transport functions for Telegram and Discord delivery."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def send_telegram(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_ids: list[str],
    text: str,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a message to multiple Telegram chat IDs.

    Returns True if at least one chat received the message.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    any_success = False
    for chat_id in chat_ids:
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            response = await http_client.post(url, json=payload)
            response.raise_for_status()
            any_success = True
            logger.info(f"Telegram message sent to chat {chat_id}")
        except Exception:
            logger.error(f"Failed to send Telegram message to chat {chat_id}")
    return any_success


async def send_discord_embed_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    embed: dict[str, Any],
) -> bool:
    """Send a single Discord embed to one webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"embeds": [embed]},
        )
        response.raise_for_status()
        logger.info(f"Discord embed sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord embed to {webhook_url[:50]}...")
        return False


async def send_discord_content_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    content: str,
) -> bool:
    """Send plain text content to one Discord webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"content": content},
        )
        response.raise_for_status()
        logger.info(f"Discord content sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord content to {webhook_url[:50]}...")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_notifier_transports.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/monitoring/trade_notifier/transports.py tests/test_trade_notifier_transports.py
git commit -m "refactor: extract trade notifier transport functions"
```

---

## Task 3: Create `formatters_discord.py` — Discord Embed Formatters

**Files:**
- Create: `app/monitoring/trade_notifier/formatters_discord.py`
- Test: `tests/test_trade_notifier_formatters_discord.py`

Extract all `_format_*` methods that return `DiscordEmbed` into pure functions. Each function takes the same parameters as the original method and returns a `DiscordEmbed` dict.

- [ ] **Step 1: Write the test**

```python
# tests/test_trade_notifier_formatters_discord.py
"""Tests for Discord embed formatters — extracted from test_trade_notifier.py originals."""

import pytest

from app.monitoring.trade_notifier.formatters_discord import (
    format_analysis_notification,
    format_automation_summary,
    format_buy_notification,
    format_cancel_notification,
    format_failure_notification,
    format_sell_notification,
    format_toss_buy_recommendation,
    format_toss_price_recommendation,
    format_toss_sell_recommendation,
)


@pytest.mark.unit
class TestFormatBuyNotification:
    def test_basic(self):
        embed = format_buy_notification(
            symbol="BTC",
            korean_name="비트코인",
            order_count=3,
            total_amount=300000.0,
            prices=[100000.0, 101000.0, 102000.0],
            volumes=[0.001, 0.001, 0.001],
            market_type="암호화폐",
        )
        assert embed["title"] == "💰 매수 주문 접수"
        assert embed["color"] == 0x00FF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "비트코인 (BTC)"
        assert fields["시장"] == "암호화폐"
        assert fields["주문 수"] == "3건"
        assert fields["총 금액"] == "300,000원"
        assert "100,000.00원 × 0.001" in fields["주문 상세"]

    def test_without_details(self):
        embed = format_buy_notification(
            symbol="BTC",
            korean_name="비트코인",
            order_count=2,
            total_amount=200000.0,
            prices=[],
            volumes=[],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "주문 상세" not in fields


@pytest.mark.unit
class TestFormatSellNotification:
    def test_basic(self):
        embed = format_sell_notification(
            symbol="ETH",
            korean_name="이더리움",
            order_count=2,
            total_volume=0.5,
            prices=[2000000.0, 2100000.0],
            volumes=[0.25, 0.25],
            expected_amount=1025000.0,
            market_type="암호화폐",
        )
        assert embed["title"] == "💸 매도 주문 접수"
        assert embed["color"] == 0xFF0000
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "이더리움 (ETH)"
        assert "2,000,000.00원 × 0.25" in fields["주문 상세"]


@pytest.mark.unit
class TestFormatCancelNotification:
    def test_basic(self):
        embed = format_cancel_notification(
            symbol="XRP",
            korean_name="리플",
            cancel_count=5,
            order_type="매수",
            market_type="암호화폐",
        )
        assert embed["title"] == "🚫 주문 취소"
        assert embed["color"] == 0xFFFF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["취소 유형"] == "매수"
        assert fields["취소 건수"] == "5건"


@pytest.mark.unit
class TestFormatAnalysisNotification:
    def test_buy(self):
        embed = format_analysis_notification(
            symbol="BTC",
            korean_name="비트코인",
            decision="buy",
            confidence=85.5,
            reasons=["상승 추세 지속", "거래량 증가", "기술적 지표 긍정적"],
            market_type="암호화폐",
        )
        assert embed["title"] == "📊 AI 분석 완료"
        assert embed["color"] == 0x0000FF
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🟢 매수"
        assert "1. 상승 추세 지속" in fields["주요 근거"]

    def test_hold(self):
        embed = format_analysis_notification(
            symbol="ETH",
            korean_name="이더리움",
            decision="hold",
            confidence=70.0,
            reasons=["시장 관망"],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🟡 보유"

    def test_sell(self):
        embed = format_analysis_notification(
            symbol="XRP",
            korean_name="리플",
            decision="sell",
            confidence=90.0,
            reasons=["하락 전망"],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🔴 매도"


@pytest.mark.unit
class TestFormatAutomationSummary:
    def test_without_errors(self):
        embed = format_automation_summary(
            total_coins=10, analyzed=10, bought=3, sold=2,
            errors=0, duration_seconds=45.5,
        )
        assert embed["title"] == "🤖 자동 거래 실행 완료"
        assert embed["color"] == 0x00FFFF
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["매수 주문"] == "3건"
        assert "오류 발생" not in fields

    def test_with_errors(self):
        embed = format_automation_summary(
            total_coins=5, analyzed=5, bought=1, sold=1,
            errors=2, duration_seconds=30.0,
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["오류 발생"] == "2건"


@pytest.mark.unit
class TestFormatFailureNotification:
    def test_basic(self):
        embed = format_failure_notification(
            symbol="AAPL",
            korean_name="애플",
            reason="APBK0656 해당종목정보가 없습니다.",
            market_type="해외주식",
        )
        assert embed["title"] == "⚠️ 거래 실패"
        assert embed["color"] == 0xFF6600
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["사유"] == "APBK0656 해당종목정보가 없습니다."


@pytest.mark.unit
class TestFormatTossBuyRecommendation:
    def test_toss_only(self):
        embed = format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )
        assert embed["title"] == "📈 [토스 수동매수]"
        assert embed["color"] == 0x00FF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "삼성전자 (005930)"
        assert "한투 보유" not in fields

    def test_with_kis(self):
        embed = format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "한투 보유" in fields


@pytest.mark.unit
class TestFormatTossSellRecommendation:
    def test_toss_only(self):
        embed = format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
            currency="원",
            market_type="국내주식",
        )
        assert embed["title"] == "📉 [토스 수동매도]"
        assert embed["color"] == 0xFF0000
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "+10.8%" in fields["💡 추천 매도가"]

    def test_negative_profit(self):
        embed = format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=60000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=62000,
            recommended_quantity=5,
            expected_profit=-15000,
            profit_percent=-4.62,
            currency="원",
            market_type="국내주식",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "-4.6%" in fields["💡 추천 매도가"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_notifier_formatters_discord.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write `formatters_discord.py`**

Copy each `_format_*` method from `trade_notifier.py` into a standalone function. Replace `self` references with explicit parameters. Use constants from `types.py`. The functions to extract are:

| Original method (line) | New function name |
|---|---|
| `_format_buy_notification` (178-225) | `format_buy_notification` |
| `_format_sell_notification` (227-280) | `format_sell_notification` |
| `_format_cancel_notification` (282-304) | `format_cancel_notification` |
| `_format_analysis_notification` (306-347) | `format_analysis_notification` |
| `_format_automation_summary` (349-382) | `format_automation_summary` |
| `_format_failure_notification` (1122-1142) | `format_failure_notification` |
| `_format_toss_buy_recommendation` (1197-1257) | `format_toss_buy_recommendation` |
| `_format_toss_sell_recommendation` (1259-1328) | `format_toss_sell_recommendation` |
| `_format_toss_price_recommendation_discord_embed` (1551-1658) | `format_toss_price_recommendation` |

Implementation approach:
- Read each original method from `app/monitoring/trade_notifier.py`
- Copy the body, replacing `self` usage and hardcoded color values with `COLORS[...]`
- Import `format_datetime` from `app.core.timezone`
- Import `DiscordEmbed`, `DiscordField`, `COLORS`, `DECISION_EMOJI`, `DECISION_TEXT` from `.types`

```python
# app/monitoring/trade_notifier/formatters_discord.py
"""Discord embed formatters for trade notifications.

Each function is pure (no I/O, no side effects) and returns a DiscordEmbed dict.
"""

from __future__ import annotations

from app.core.timezone import format_datetime

from .types import COLORS, DECISION_EMOJI, DECISION_TEXT, DiscordEmbed, DiscordField


def format_buy_notification(
    *,
    symbol: str,
    korean_name: str,
    order_count: int,
    total_amount: float,
    prices: list[float],
    volumes: list[float],
    market_type: str,
) -> DiscordEmbed:
    # Copy body from trade_notifier.py lines 178-225 exactly,
    # replacing hardcoded 0x00FF00 with COLORS["buy"]
    ...  # IMPLEMENTATION: copy from original _format_buy_notification


def format_sell_notification(
    *,
    symbol: str,
    korean_name: str,
    order_count: int,
    total_volume: float,
    prices: list[float],
    volumes: list[float],
    expected_amount: float,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 227-280, use COLORS["sell"]
    ...


def format_cancel_notification(
    *,
    symbol: str,
    korean_name: str,
    cancel_count: int,
    order_type: str,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 282-304, use COLORS["cancel"]
    ...


def format_analysis_notification(
    *,
    symbol: str,
    korean_name: str,
    decision: str,
    confidence: float,
    reasons: list[str],
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 306-347, use COLORS["analysis"], DECISION_EMOJI, DECISION_TEXT
    ...


def format_automation_summary(
    *,
    total_coins: int,
    analyzed: int,
    bought: int,
    sold: int,
    errors: int,
    duration_seconds: float,
) -> DiscordEmbed:
    # Copy from lines 349-382, use COLORS["summary"]
    ...


def format_failure_notification(
    *,
    symbol: str,
    korean_name: str,
    reason: str,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 1122-1142, use COLORS["failure"]
    ...


def format_toss_buy_recommendation(
    *,
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    recommended_price: float,
    recommended_quantity: int,
    currency: str,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 1197-1257, use COLORS["buy"]
    ...


def format_toss_sell_recommendation(
    *,
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    recommended_price: float,
    recommended_quantity: int,
    expected_profit: float,
    profit_percent: float,
    currency: str,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 1259-1328, use COLORS["sell"]
    ...


def format_toss_price_recommendation(
    *,
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    decision: str,
    confidence: float,
    reasons: list[str],
    appropriate_buy_min: float | None,
    appropriate_buy_max: float | None,
    appropriate_sell_min: float | None,
    appropriate_sell_max: float | None,
    buy_hope_min: float | None,
    buy_hope_max: float | None,
    sell_target_min: float | None,
    sell_target_max: float | None,
    currency: str,
    market_type: str,
) -> DiscordEmbed:
    # Copy from lines 1551-1658, use COLORS dict and DECISION_EMOJI/DECISION_TEXT
    ...
```

**IMPORTANT for the implementing agent:** Each `...` placeholder above MUST be replaced with the **exact body** from the corresponding method in `trade_notifier.py`. Read those line ranges and transplant the logic, only changing:
1. Remove `self` parameter
2. Replace hardcoded hex colors with `COLORS["..."]`
3. Use `DECISION_EMOJI` and `DECISION_TEXT` from imports instead of inline dicts

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_notifier_formatters_discord.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/monitoring/trade_notifier/formatters_discord.py tests/test_trade_notifier_formatters_discord.py
git commit -m "refactor: extract Discord embed formatters into formatters_discord.py"
```

---

## Task 4: Create `formatters_telegram.py` — Telegram Message Formatters

**Files:**
- Create: `app/monitoring/trade_notifier/formatters_telegram.py`
- Test: `tests/test_trade_notifier_formatters_telegram.py`

Extract Telegram text formatting functions. These are pure string-returning functions.

- [ ] **Step 1: Write the test**

```python
# tests/test_trade_notifier_formatters_telegram.py
"""Tests for Telegram message formatters."""

import pytest

from app.monitoring.trade_notifier.formatters_telegram import (
    format_analysis_notification_telegram,
    format_automation_summary_telegram,
    format_buy_notification_telegram,
    format_cancel_notification_telegram,
    format_failure_notification_telegram,
    format_sell_notification_telegram,
    format_toss_price_recommendation_html,
)


@pytest.mark.unit
class TestFormatBuyNotificationTelegram:
    def test_basic(self):
        msg = format_buy_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[100000.0],
            volumes=[0.001],
            market_type="암호화폐",
        )
        assert "*💰 매수 주문 접수*" in msg
        assert "비트코인" in msg
        assert "BTC" in msg


@pytest.mark.unit
class TestFormatSellNotificationTelegram:
    def test_basic(self):
        msg = format_sell_notification_telegram(
            symbol="ETH",
            korean_name="이더리움",
            order_count=2,
            total_volume=0.5,
            prices=[2000000.0, 2100000.0],
            volumes=[0.25, 0.25],
            expected_amount=1025000.0,
            market_type="암호화폐",
        )
        assert "*💸 매도 주문 접수*" in msg
        assert "이더리움" in msg


@pytest.mark.unit
class TestFormatCancelNotificationTelegram:
    def test_basic(self):
        msg = format_cancel_notification_telegram(
            symbol="XRP",
            korean_name="리플",
            cancel_count=5,
            order_type="매수",
            market_type="암호화폐",
        )
        assert "*🚫 주문 취소*" in msg
        assert "리플" in msg


@pytest.mark.unit
class TestFormatAnalysisNotificationTelegram:
    def test_buy(self):
        msg = format_analysis_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            decision="buy",
            confidence=85.5,
            reasons=["상승 추세"],
            market_type="암호화폐",
        )
        assert "*📊 AI 분석 완료*" in msg
        assert "🟢" in msg


@pytest.mark.unit
class TestFormatAutomationSummaryTelegram:
    def test_basic(self):
        msg = format_automation_summary_telegram(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=0,
            duration_seconds=45.5,
        )
        assert "*🤖 자동 거래 실행 완료*" in msg
        assert "45.5" in msg


@pytest.mark.unit
class TestFormatFailureNotificationTelegram:
    def test_basic(self):
        msg = format_failure_notification_telegram(
            symbol="AAPL",
            korean_name="애플",
            reason="주문 실패",
            market_type="해외주식",
        )
        assert "*⚠️ 거래 실패*" in msg
        assert "주문 실패" in msg


@pytest.mark.unit
class TestFormatTossPriceRecommendationHtml:
    def test_basic(self):
        html = format_toss_price_recommendation_html(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            decision="buy",
            confidence=85.0,
            reasons=["상승 추세"],
            appropriate_buy_min=68000,
            appropriate_buy_max=70000,
            appropriate_sell_min=None,
            appropriate_sell_max=None,
            buy_hope_min=65000,
            buy_hope_max=67000,
            sell_target_min=None,
            sell_target_max=None,
            currency="원",
            market_type="국내주식",
        )
        assert "<b>" in html
        assert "삼성전자" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_notifier_formatters_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write `formatters_telegram.py`**

```python
# app/monitoring/trade_notifier/formatters_telegram.py
"""Telegram message formatters for trade notifications.

Each function is pure (no I/O) and returns a formatted string.
"""

from __future__ import annotations

from app.core.timezone import format_datetime

from .types import DECISION_EMOJI, DECISION_TEXT


def format_buy_notification_telegram(
    *,
    symbol: str,
    korean_name: str,
    order_count: int,
    total_amount: float,
    prices: list[float],
    volumes: list[float],
    market_type: str,
) -> str:
    # Copy from trade_notifier.py lines 601-651
    ...


def format_sell_notification_telegram(
    *,
    symbol: str,
    korean_name: str,
    order_count: int,
    total_volume: float,
    prices: list[float],
    volumes: list[float],
    expected_amount: float,
    market_type: str,
) -> str:
    # Copy from lines 653-706
    ...


def format_cancel_notification_telegram(
    *,
    symbol: str,
    korean_name: str,
    cancel_count: int,
    order_type: str,
    market_type: str,
) -> str:
    # Copy from lines 708-742
    ...


def format_analysis_notification_telegram(
    *,
    symbol: str,
    korean_name: str,
    decision: str,
    confidence: float,
    reasons: list[str],
    market_type: str,
) -> str:
    # Copy from lines 744-794
    ...


def format_automation_summary_telegram(
    *,
    total_coins: int,
    analyzed: int,
    bought: int,
    sold: int,
    errors: int,
    duration_seconds: float,
) -> str:
    # Copy from lines 384-424
    ...


def format_failure_notification_telegram(
    *,
    symbol: str,
    korean_name: str,
    reason: str,
    market_type: str,
) -> str:
    # Copy from lines 796-827
    ...


def format_toss_price_recommendation_html(
    *,
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    decision: str,
    confidence: float,
    reasons: list[str],
    appropriate_buy_min: float | None,
    appropriate_buy_max: float | None,
    appropriate_sell_min: float | None,
    appropriate_sell_max: float | None,
    buy_hope_min: float | None,
    buy_hope_max: float | None,
    sell_target_min: float | None,
    sell_target_max: float | None,
    currency: str,
    market_type: str,
) -> str:
    # Copy from lines 1440-1549
    ...
```

**IMPORTANT for the implementing agent:** Each `...` placeholder above MUST be replaced with the **exact body** from the corresponding method in `trade_notifier.py`. Read those line ranges and transplant the logic, only changing:
1. Remove `self` parameter
2. Use `DECISION_EMOJI` and `DECISION_TEXT` from imports instead of inline dicts

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_notifier_formatters_telegram.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/monitoring/trade_notifier/formatters_telegram.py tests/test_trade_notifier_formatters_telegram.py
git commit -m "refactor: extract Telegram message formatters into formatters_telegram.py"
```

---

## Task 5: Create `notifier.py` — Refactored TradeNotifier Class

**Files:**
- Create: `app/monitoring/trade_notifier/notifier.py`
- Modify: `app/monitoring/trade_notifier/__init__.py`

Rewrite the `TradeNotifier` class to delegate to the new formatter and transport modules. The class retains:
- Singleton pattern (`__new__`, `__init__`, `_instance`, `_initialized`)
- `configure()`, `shutdown()`, `_get_webhook_for_market_type()`, `_has_telegram_delivery_config()`
- All public `notify_*` methods — but their bodies now call formatter functions + transport functions
- `get_trade_notifier()` module-level function

The class **no longer contains** any `_format_*` methods or raw HTTP posting logic.

- [ ] **Step 1: Write `notifier.py`**

Read the original `trade_notifier.py` carefully. For each public `notify_*` method, the new body should:
1. Check `_enabled` guard (same as before)
2. Call the corresponding formatter function to build the embed/message
3. Call the transport function to send it
4. Handle fallback (Discord → Telegram) same as original

```python
# app/monitoring/trade_notifier/notifier.py
"""TradeNotifier singleton class — orchestrates formatters and transports."""

from __future__ import annotations

import logging

import httpx

from . import formatters_discord as fmt_discord
from . import formatters_telegram as fmt_telegram
from .transports import send_discord_content_single, send_discord_embed_single, send_telegram
from .types import DiscordEmbed, DiscordField

logger = logging.getLogger(__name__)


class TradeNotifier:
    """Singleton trade notifier with Telegram and Discord integration."""

    _instance: TradeNotifier | None = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._bot_token: str | None = None
            self._chat_ids: list[str] = []
            self._discord_webhook_us: str | None = None
            self._discord_webhook_kr: str | None = None
            self._discord_webhook_crypto: str | None = None
            self._discord_webhook_alerts: str | None = None
            self._discord_webhook_urls: list[str] = []
            self._enabled: bool = False
            self._http_client: httpx.AsyncClient | None = None
            TradeNotifier._initialized = True

    # ── configure / shutdown / routing ──────────────────────────────
    # Copy configure(), shutdown(), _get_webhook_for_market_type(),
    # _has_telegram_delivery_config() EXACTLY from the original.

    # ── public notify methods ───────────────────────────────────────
    # For each notify_* method, replace the inline formatting with a
    # call to the corresponding formatter, and replace the inline HTTP
    # posting with a call to the corresponding transport function.
    #
    # Example pattern (notify_buy_order):
    #
    #   async def notify_buy_order(self, ...) -> bool:
    #       if not self._enabled:
    #           return False
    #       embed = fmt_discord.format_buy_notification(
    #           symbol=symbol, korean_name=korean_name, ...
    #       )
    #       webhook_url = self._get_webhook_for_market_type(market_type)
    #       if webhook_url and self._http_client:
    #           ok = await send_discord_embed_single(
    #               http_client=self._http_client,
    #               webhook_url=webhook_url,
    #               embed=embed,
    #           )
    #           if ok:
    #               return True
    #       # Telegram fallback
    #       if self._has_telegram_delivery_config():
    #           msg = fmt_telegram.format_buy_notification_telegram(
    #               symbol=symbol, korean_name=korean_name, ...
    #           )
    #           return await send_telegram(
    #               http_client=self._http_client,
    #               bot_token=self._bot_token,
    #               chat_ids=self._chat_ids,
    #               text=msg,
    #           )
    #       return False

    ...  # IMPLEMENTATION: apply pattern to ALL notify_* methods


def get_trade_notifier() -> TradeNotifier:
    """Get the singleton TradeNotifier instance."""
    return TradeNotifier()
```

**IMPORTANT for the implementing agent:**
- Read each `notify_*` method from the original `trade_notifier.py` (lines 829-1870)
- Replicate the exact guard logic, fallback behavior, and logging
- Replace inline formatting calls with `fmt_discord.format_*()` / `fmt_telegram.format_*()` calls
- Replace inline `self._http_client.post(...)` calls with `send_discord_embed_single()` / `send_discord_content_single()` / `send_telegram()`
- Keep the `_send_to_discord_embed_single` and `_send_to_discord_content_single` methods as **thin wrappers** around the transport functions so that tests that mock these methods still work (see Step 3 note below)

**Backward-compatibility wrappers** — the existing tests mock `_send_to_discord_content_single` and `_send_to_telegram` directly on the notifier instance. To keep these tests passing without modification, add these delegating methods:

```python
    async def _send_to_discord_embed_single(self, embed, webhook_url) -> bool:
        if not self._http_client:
            return False
        return await send_discord_embed_single(
            http_client=self._http_client, webhook_url=webhook_url, embed=embed,
        )

    async def _send_to_discord_content_single(self, content, webhook_url) -> bool:
        if not self._http_client:
            return False
        return await send_discord_content_single(
            http_client=self._http_client, webhook_url=webhook_url, content=content,
        )

    async def _send_to_telegram(self, text, parse_mode="Markdown") -> bool:
        if not self._http_client or not self._bot_token or not self._chat_ids:
            return False
        return await send_telegram(
            http_client=self._http_client, bot_token=self._bot_token,
            chat_ids=self._chat_ids, text=text, parse_mode=parse_mode,
        )
```

Also keep `_format_*` methods as thin wrappers to the new formatter functions. This ensures tests in `test_trade_notifier.py` and `test_toss_notification.py` that call `notifier._format_*()` directly still pass:

```python
    def _format_buy_notification(self, **kwargs) -> DiscordEmbed:
        return fmt_discord.format_buy_notification(**kwargs)

    def _format_sell_notification(self, **kwargs) -> DiscordEmbed:
        return fmt_discord.format_sell_notification(**kwargs)

    # ... same for all other _format_* methods
```

- [ ] **Step 2: Update `__init__.py` to re-export**

```python
# app/monitoring/trade_notifier/__init__.py
"""Trade notification system with Telegram and Discord integration."""

from .notifier import TradeNotifier, get_trade_notifier
from .types import DiscordEmbed, DiscordField

__all__ = [
    "DiscordEmbed",
    "DiscordField",
    "TradeNotifier",
    "get_trade_notifier",
]
```

- [ ] **Step 3: Run ALL existing tests to verify backward compatibility**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py -v`
Expected: All PASS — same results as before the refactor

- [ ] **Step 4: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py app/monitoring/trade_notifier/__init__.py
git commit -m "refactor: rewrite TradeNotifier to delegate to formatters and transports"
```

---

## Task 6: Remove Old Monolith and Verify

**Files:**
- Delete: `app/monitoring/trade_notifier.py` (the old single file)
- No test changes needed — imports resolve to the package

- [ ] **Step 1: Verify old file still exists alongside new package**

```bash
ls -la app/monitoring/trade_notifier.py
ls -la app/monitoring/trade_notifier/
```

Python resolves `app.monitoring.trade_notifier` to the **package** (`trade_notifier/`) when both exist, but having both is confusing. Delete the old file.

- [ ] **Step 2: Delete the old monolith**

```bash
git rm app/monitoring/trade_notifier.py
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py tests/test_fill_notification.py -v`
Expected: All PASS

- [ ] **Step 4: Run a broader smoke test**

Run: `uv run pytest tests/ -v -m "not integration and not slow" -x --timeout=60`
Expected: No import errors or regressions related to `trade_notifier`

- [ ] **Step 5: Commit**

```bash
git rm app/monitoring/trade_notifier.py
git commit -m "refactor: remove old trade_notifier.py monolith, package is now sole source"
```

---

## Task 7: Final Verification and Cleanup

**Files:**
- Review: all new files in `app/monitoring/trade_notifier/`
- Review: all test files

- [ ] **Step 1: Verify all external imports still resolve**

```bash
uv run python -c "from app.monitoring.trade_notifier import TradeNotifier, get_trade_notifier; print('OK')"
uv run python -c "from app.monitoring.trade_notifier import DiscordEmbed, DiscordField; print('OK')"
```

Expected: Both print `OK`

- [ ] **Step 2: Run lint and type checks**

```bash
uv run ruff check app/monitoring/trade_notifier/
uv run ruff format --check app/monitoring/trade_notifier/
```

Fix any issues.

- [ ] **Step 3: Run full test suite one final time**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py tests/test_fill_notification.py tests/test_trade_notifier_types.py tests/test_trade_notifier_transports.py tests/test_trade_notifier_formatters_discord.py tests/test_trade_notifier_formatters_telegram.py -v`
Expected: All PASS

- [ ] **Step 4: Fix any lint or format issues and commit**

```bash
uv run ruff format app/monitoring/trade_notifier/
git add -A
git commit -m "refactor: lint and format trade notifier package"
```

---

## Summary of Changes

| Before | After | Responsibility |
|---|---|---|
| `trade_notifier.py` (1,882 lines) | `trade_notifier/types.py` (~50 lines) | TypedDicts, colors, emoji/text maps |
| | `trade_notifier/transports.py` (~80 lines) | HTTP sending to Telegram & Discord |
| | `trade_notifier/formatters_discord.py` (~400 lines) | 9 Discord embed builder functions |
| | `trade_notifier/formatters_telegram.py` (~300 lines) | 7 Telegram message builder functions |
| | `trade_notifier/notifier.py` (~500 lines) | TradeNotifier class (singleton, routing, orchestration) |
| | `trade_notifier/__init__.py` (~10 lines) | Re-exports for backward compatibility |

**Public API preserved:** `TradeNotifier`, `get_trade_notifier`, `DiscordEmbed`, `DiscordField` — all importable from `app.monitoring.trade_notifier`.

**No message format changes:** All formatted strings and embed structures are identical byte-for-byte to the original.

**Backward-compatible private methods:** `_format_*` and `_send_to_*` methods remain as thin wrappers so existing test mocks continue to work.
