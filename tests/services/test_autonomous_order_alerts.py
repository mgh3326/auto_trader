"""hk 7004 — autonomous order alert emitter (design 6939 I12, N7).

Unit tests for the message shape, duplicate suppression, fail-open delivery,
the exact-string gate, and the static import boundary (no broker, order or
ledger module).
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import autonomous_order_alerts as alerts

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/autonomous_order_alerts.py"


class FakeChannel:
    def __init__(
        self,
        *,
        result: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.sent: list[alerts.OrderAlertEvent] = []
        self.result = result
        self.error = error
        self.delay = delay

    async def send(self, event: alerts.OrderAlertEvent) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.sent.append(event)
        return self.result


def event(kind: alerts.OrderAlertKind = alerts.OrderAlertKind.ORDER_ACCEPTED, **kw):
    fields = {
        "kind": kind,
        "event_key": "journal:abc:1",
        "account": "kis_live",
        "symbol": "005930",
        "side": "buy",
        "quantity": "10",
        "price": "71000",
        "plan_head_id": 42,
        "plan_version": 7,
        "plan_row_key": '["new","kr","005930","kis_live"]',
        "reason_code": "order_accepted",
    }
    fields.update(kw)
    return alerts.OrderAlertEvent(**fields)


def alerter(channel, *, enabled: bool = True, **kw) -> alerts.AutonomousOrderAlerter:
    return alerts.AutonomousOrderAlerter(channel=channel, enabled=enabled, **kw)


def fields_of(embed: dict) -> dict[str, str]:
    return {field["name"]: field["value"] for field in embed["fields"]}


# ------------------------------------------------------------------ gate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("True", False),
        ("TRUE", False),
        ("1", False),
        ("", False),
        ("yes", False),
    ],
)
def test_gate_is_exactly_lowercase_true(value, expected):
    assert alerts.alerts_enabled({alerts.ALERTS_ENABLED_ENV: value}) is expected


def test_gate_defaults_off_when_unset(monkeypatch):
    monkeypatch.delenv(alerts.ALERTS_ENABLED_ENV, raising=False)
    assert alerts.alerts_enabled() is False


@pytest.mark.asyncio
async def test_gate_off_schedules_nothing():
    channel = FakeChannel()
    instance = alerter(channel, enabled=False)

    assert instance.emit(event()) is False
    await instance.drain()

    assert channel.sent == []


# --------------------------------------------------------------- messages


def test_every_kind_has_a_korean_title_and_a_colour():
    for kind in alerts.OrderAlertKind:
        embed = alerts.alert_embed(event(kind))
        assert any("가" <= char <= "힣" for char in embed["title"]), kind
        assert isinstance(embed["color"], int)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(alerts.OrderAlertKind))
async def test_each_event_kind_sends_one_message(kind):
    channel = FakeChannel()
    instance = alerter(channel)

    assert instance.emit(event(kind)) is True
    await instance.drain()

    assert len(channel.sent) == 1
    assert channel.sent[0].kind is kind
    assert fields_of(alerts.alert_embed(channel.sent[0]))["이벤트"] == kind.value


@pytest.mark.asyncio
async def test_message_carries_account_symbol_side_quantity_price_plan_and_reason():
    channel = FakeChannel()
    instance = alerter(channel)

    instance.emit(
        event(
            alerts.OrderAlertKind.STOP_LOSS_SENT,
            side="sell",
            quantity="10",
            price="68000",
            reason_code="invalidation_break",
        )
    )
    await instance.drain()

    fields = fields_of(alerts.alert_embed(channel.sent[0]))
    assert fields["계좌"] == "kis_live"
    assert fields["종목"] == "005930"
    assert fields["구분"] == "매도"
    assert fields["수량"] == "10"
    assert fields["가격"] == "68000"
    assert fields["계획"] == 'head 42 · seq 7 · row ["new","kr","005930","kis_live"]'
    assert fields["사유"] == "invalidation_break"
    assert fields["이벤트 키"] == "`journal:abc:1`"


@pytest.mark.asyncio
async def test_optional_fields_render_as_dash_not_none():
    channel = FakeChannel()
    instance = alerter(channel)

    instance.emit(
        alerts.OrderAlertEvent(
            kind=alerts.OrderAlertKind.KILL_FREEZE,
            event_key="kill:2026-11-27",
            account="toss_live",
            symbol="-",
            reason_code="daily_loss_freeze",
        )
    )
    await instance.drain()

    fields = fields_of(alerts.alert_embed(channel.sent[0]))
    assert fields["수량"] == "-"
    assert fields["가격"] == "-"
    assert fields["계획"] == "-"
    assert fields["구분"] == "-"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("kis_live", "kis_live"),
        ("toss_live", "toss_live"),
        ("upbit_live", "upbit_live"),
        ("kiwoom_mock_us", "kiwoom_mock_us"),
        ("12345678-01", "[MASKED]"),
        ("12345678", "[MASKED]"),
        ("acct 12", "[MASKED]"),
        (None, "-"),
        ("", "-"),
    ],
)
def test_account_is_masked_without_a_suffix_reveal(value, expected):
    assert alerts.mask_account(value) == expected


# ------------------------------------------------------- duplicate keys


@pytest.mark.asyncio
async def test_duplicate_event_key_is_sent_once():
    channel = FakeChannel()
    instance = alerter(channel)

    assert instance.emit(event()) is True
    assert instance.emit(event()) is False  # in flight
    await instance.drain()
    assert instance.emit(event()) is False  # already delivered
    await instance.drain()

    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_distinct_event_keys_both_send():
    channel = FakeChannel()
    instance = alerter(channel)

    assert instance.emit(event(event_key="journal:a")) is True
    assert instance.emit(event(event_key="journal:b")) is True
    await instance.drain()

    assert [sent.event_key for sent in channel.sent] == ["journal:a", "journal:b"]


@pytest.mark.asyncio
async def test_empty_event_key_is_not_sent():
    channel = FakeChannel()
    instance = alerter(channel)

    assert instance.emit(event(event_key="   ")) is False
    await instance.drain()

    assert channel.sent == []


# ------------------------------------------------------------- fail-open


@pytest.mark.asyncio
async def test_failed_delivery_never_raises_and_releases_the_key():
    channel = FakeChannel(error=RuntimeError("webhook down"))
    instance = alerter(channel)

    assert instance.emit(event()) is True
    await instance.drain()
    assert channel.sent == []

    channel.error = None
    assert instance.emit(event()) is True
    await instance.drain()

    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_send_timeout_is_bounded_and_swallowed():
    channel = FakeChannel(delay=5.0)
    instance = alerter(channel, send_timeout=0.01)

    started = time.monotonic()
    assert instance.emit(event()) is True
    await instance.drain(timeout=1.0)

    assert time.monotonic() - started < 1.0
    assert channel.sent == []


def test_emit_outside_an_event_loop_does_not_raise():
    channel = FakeChannel()
    instance = alerter(channel)

    assert instance.emit(event()) is False
    assert channel.sent == []


# --------------------------------------------------------------- channel


@pytest.mark.asyncio
async def test_webhook_unset_posts_nothing(monkeypatch):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "send_discord_embed_single", sender)

    assert await alerts.DiscordAlertChannel(None).send(event()) is False
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_posts_one_embed(monkeypatch):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "send_discord_embed_single", sender)

    sent = event(alerts.OrderAlertKind.FILL_BOOKED)

    assert await alerts.DiscordAlertChannel("https://discord.example/ops").send(sent)
    kwargs = sender.await_args.kwargs
    assert kwargs["webhook_url"] == "https://discord.example/ops"
    assert kwargs["embed"]["title"] == "자동 주문 체결 기록"
    assert fields_of(kwargs["embed"])["이벤트"] == "fill_booked"


@pytest.mark.asyncio
async def test_webhook_transport_failure_is_false(monkeypatch):
    monkeypatch.setattr(
        alerts, "send_discord_embed_single", AsyncMock(return_value=False)
    )
    channel = alerts.DiscordAlertChannel("https://discord.example/ops")

    assert await channel.send(event()) is False


# ------------------------------------------------------ default instance


@pytest.mark.asyncio
async def test_default_alerter_is_a_singleton_and_honours_the_gate(monkeypatch):
    channel = FakeChannel()
    monkeypatch.setenv(alerts.ALERTS_ENABLED_ENV, "true")
    monkeypatch.setattr(alerts, "DiscordAlertChannel", lambda _url: channel)
    monkeypatch.setattr(settings, "discord_webhook_alerts", "https://discord.example/x")
    alerts.reset_default_alerter()

    try:
        first = alerts.get_default_alerter()
        assert alerts.get_default_alerter() is first
        assert first.enabled
        assert alerts.emit_order_alert(event()) is True
        assert alerts.emit_order_alert(event()) is False  # same key, same process
        await first.drain()
        assert len(channel.sent) == 1
    finally:
        alerts.reset_default_alerter()


# -------------------------------------------------------- static boundary

_ALLOWED_IMPORTS = {
    "__future__",
    "asyncio",
    "logging",
    "os",
    "re",
    "collections",
    "collections.abc",
    "dataclasses",
    "enum",
    "typing",
    "httpx",
    "app.core.config",
    "app.monitoring.trade_notifier.transports",
}
_FORBIDDEN_PREFIXES = (
    "app.services.brokers",
    "app.services.execution_intent_gateway",
    "app.services.daily_plan",
    "app.services.execution_ledger",
    "app.models",
    "sqlalchemy",
    "asyncpg",
    "redis",
    "taskiq",
    "prefect",
)
_FORBIDDEN_NAMES = ("place_order", "submit_order", "cancel_order", "execution_ledger")


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.append((module, node.lineno))
            found += [(f"{module}.{alias.name}", node.lineno) for alias in node.names]
    return found


def test_module_imports_no_broker_order_or_ledger_surface():
    offenders: list[str] = []
    for module, line in _imported_modules(MODULE):
        top = module.split(".")[0]
        if top in {
            "asyncio",
            "logging",
            "os",
            "re",
            "collections",
            "dataclasses",
            "enum",
            "typing",
            "httpx",
        }:
            continue
        if module.startswith(_FORBIDDEN_PREFIXES):
            offenders.append(f"{line}: {module}")
            continue
        if not any(
            module == allowed or module.startswith(allowed + ".")
            for allowed in _ALLOWED_IMPORTS
        ):
            offenders.append(f"{line}: {module}")
    assert not offenders


def test_module_names_no_order_submission_or_ledger_write():
    source = MODULE.read_text()
    for name in _FORBIDDEN_NAMES:
        assert name not in source, f"module names {name}"
