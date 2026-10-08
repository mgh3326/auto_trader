"""H5 failure alerts: dedupe, kinds, heartbeat judgement, channel. Fakes only."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from app.services.brokers.binance.h5 import alerting
from app.services.brokers.binance.h5.alerting import (
    FAILURE_EVENTS,
    HEALTHY_EVENTS,
    AlertKind,
    DiscordAlertsChannel,
    H5Alert,
    H5Alerter,
    H5RunMonitor,
    HeartbeatVerdict,
    alert_embed,
    alert_enabled,
    judge_heartbeat,
    poll_heartbeat,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 5, 3, 0, tzinfo=dt.UTC)
REPO_ROOT = Path(__file__).resolve().parents[5]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += dt.timedelta(**kwargs)


class FakeChannel:
    def __init__(self, *results: bool | BaseException) -> None:
        self.sent: list[H5Alert] = []
        self._results = list(results)

    async def send(self, alert: H5Alert) -> bool:
        self.sent.append(alert)
        if not self._results:
            return True
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make(channel: FakeChannel | None, *, enabled: bool = True, **kwargs):
    clock = Clock()
    return H5Alerter(channel=channel, enabled=enabled, clock=clock, **kwargs), clock


def fire(alerter: H5Alerter, kind: AlertKind, signature: str = "sig") -> bool:
    return asyncio.run(alerter.fire(kind, signature))


# --- gate -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [None, "", "false", "TRUE", "True", "1", "yes", " true"]
)
def test_alert_gate_is_exactly_lowercase_true(value):
    env = {} if value is None else {alerting.ALERT_ENABLED_ENV: value}
    assert alert_enabled(env) is False
    assert alert_enabled({alerting.ALERT_ENABLED_ENV: "true"}) is True


def test_disabled_alerter_never_touches_the_channel():
    channel = FakeChannel()
    alerter, _ = make(channel, enabled=False)
    for kind in AlertKind:
        assert fire(alerter, kind) is False
    assert channel.sent == []
    assert alerter.enabled is False


def test_enabled_without_a_channel_sends_nothing_and_does_not_raise():
    alerter, _ = make(None, enabled=True)
    assert fire(alerter, AlertKind.ERROR) is False
    assert alerter.enabled is False


# --- dedupe ----------------------------------------------------------------


def test_same_failure_alerts_once_then_stays_quiet():
    channel = FakeChannel()
    alerter, clock = make(channel)
    assert fire(alerter, AlertKind.ERROR, "blocked:X") is True
    for _ in range(30):
        clock.advance(minutes=1)
        assert fire(alerter, AlertKind.ERROR, "blocked:X") is False
    assert len(channel.sent) == 1


def test_reminder_is_bounded_to_one_per_repeat_window():
    channel = FakeChannel()
    alerter, clock = make(channel, repeat_after=dt.timedelta(hours=6))
    fire(alerter, AlertKind.HEARTBEAT_MISSED, "stale_tick")
    clock.advance(hours=5, minutes=59)
    assert fire(alerter, AlertKind.HEARTBEAT_MISSED, "stale_tick") is False
    clock.advance(minutes=1)
    assert fire(alerter, AlertKind.HEARTBEAT_MISSED, "stale_tick") is True
    clock.advance(hours=1)
    assert fire(alerter, AlertKind.HEARTBEAT_MISSED, "stale_tick") is False
    assert len(channel.sent) == 2


def test_diagnostic_text_inside_one_bucket_never_opens_a_new_episode():
    """Round-1 B2: ConnectError then ReadTimeout during one outage is one episode."""
    channel = FakeChannel()
    alerter, clock = make(channel)
    for i, text in enumerate(["blocked:A", "blocked:B", "blocked:A", "blocked:B"]):
        clock.advance(minutes=1)
        delivered = asyncio.run(alerter.fire(AlertKind.ERROR, text, "blocked"))
        assert delivered is (i == 0)
    assert [a.signature for a in channel.sent] == ["blocked:A"]


def test_failed_delivery_backoff_holds_across_changing_diagnostic_text():
    channel = FakeChannel(False, False, True)
    alerter, clock = make(channel, retry_after=dt.timedelta(minutes=5))
    attempts = []
    for text in ["blocked:A", "blocked:B", "blocked:A", "blocked:B"]:
        attempts.append(asyncio.run(alerter.fire(AlertKind.ERROR, text, "blocked")))
        clock.advance(minutes=1)
    assert len(channel.sent) == 1 and attempts == [False] * 4
    clock.advance(minutes=5)
    assert asyncio.run(alerter.fire(AlertKind.ERROR, "blocked:C", "blocked")) is False
    assert len(channel.sent) == 2


def test_a_different_bucket_is_a_new_failure_but_a_seen_bucket_is_not_resent():
    channel = FakeChannel()
    alerter, _ = make(channel)
    sends = [
        asyncio.run(alerter.fire(AlertKind.ERROR, f"{b}:x", b))
        for b in ("blocked", "entry_uncertain", "blocked", "entry_uncertain")
    ]
    assert sends == [True, True, False, False]
    assert [a.signature for a in channel.sent] == ["blocked:x", "entry_uncertain:x"]


def test_a_send_still_in_flight_counts_as_the_episode():
    """Two failing ticks while the first webhook call is outstanding send once."""
    release_started = []

    class Slow:
        sent: list[H5Alert] = []

        async def send(self, alert: H5Alert) -> bool:
            self.sent.append(alert)
            release_started.append(1)
            await asyncio.sleep(0.05)
            return True

    channel = Slow()
    alerter = H5Alerter(channel=channel, enabled=True)

    async def go() -> tuple[bool, bool]:
        first = asyncio.create_task(alerter.fire(AlertKind.ERROR, "e", "blocked"))
        await asyncio.sleep(0.01)
        second = await alerter.fire(AlertKind.ERROR, "e", "blocked")
        return await first, second

    assert asyncio.run(go()) == (True, False)
    assert len(channel.sent) == 1


def test_clear_rearms_the_kind_and_only_that_kind():
    channel = FakeChannel()
    alerter, _ = make(channel)
    fire(alerter, AlertKind.ERROR, "e")
    fire(alerter, AlertKind.STOPPED, "s")
    alerter.clear(AlertKind.ERROR)
    assert fire(alerter, AlertKind.ERROR, "e") is True
    assert fire(alerter, AlertKind.STOPPED, "s") is False
    assert len(channel.sent) == 3


def test_kinds_do_not_suppress_each_other():
    channel = FakeChannel()
    alerter, _ = make(channel)
    for kind in (AlertKind.STOPPED, AlertKind.ERROR, AlertKind.HEARTBEAT_MISSED):
        assert fire(alerter, kind, "same") is True
    assert {a.kind for a in channel.sent} == {
        AlertKind.STOPPED,
        AlertKind.ERROR,
        AlertKind.HEARTBEAT_MISSED,
    }


def test_failed_delivery_is_not_a_delivery_and_retries_are_spaced():
    channel = FakeChannel(False, False, True)
    alerter, clock = make(channel, retry_after=dt.timedelta(minutes=5))
    assert fire(alerter, AlertKind.ERROR) is False
    clock.advance(minutes=4)
    assert fire(alerter, AlertKind.ERROR) is False  # backed off: no second attempt
    assert len(channel.sent) == 1
    clock.advance(minutes=1)
    assert fire(alerter, AlertKind.ERROR) is False  # second attempt also fails
    clock.advance(minutes=5)
    assert fire(alerter, AlertKind.ERROR) is True
    clock.advance(minutes=10)
    assert fire(alerter, AlertKind.ERROR) is False  # delivered: deduped now
    assert len(channel.sent) == 3


def test_a_raising_channel_never_propagates():
    alerter, _ = make(FakeChannel(RuntimeError("boom")))
    assert fire(alerter, AlertKind.ERROR) is False


def test_cancellation_is_not_swallowed_by_delivery():
    alerter, _ = make(FakeChannel(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        fire(alerter, AlertKind.ERROR)


def test_a_hanging_channel_is_bounded_by_the_send_timeout():
    class Hang:
        async def send(self, alert):
            await asyncio.sleep(30)
            return True

    alerter = H5Alerter(channel=Hang(), enabled=True, send_timeout=0.01)
    assert asyncio.run(alerter.fire(AlertKind.ERROR, "x")) is False


def test_signature_is_single_line_and_bounded():
    channel = FakeChannel()
    alerter, _ = make(channel)
    fire(alerter, AlertKind.ERROR, "a\nb\t c" + "z" * 500)
    (sent,) = channel.sent
    assert "\n" not in sent.signature and "\t" not in sent.signature
    assert len(sent.signature) == 120


# --- monitor ---------------------------------------------------------------


def run_ticks(monitor: H5RunMonitor, *payloads: dict) -> None:
    """Report each tick, letting its background alert finish before the next."""

    async def go():
        for payload in payloads:
            await monitor.tick_done(payload)
            await monitor.drain()

    asyncio.run(go())


@pytest.mark.parametrize("event", sorted(FAILURE_EVENTS))
def test_each_failure_event_alerts_once_per_episode(event):
    channel = FakeChannel()
    alerter, _ = make(channel)
    run_ticks(H5RunMonitor(alerter), *[{"event": event, "detail": "d"}] * 5)
    assert [a.kind for a in channel.sent] == [AlertKind.ERROR]
    assert channel.sent[0].signature == f"{event}:d"


@pytest.mark.parametrize("event", sorted(HEALTHY_EVENTS))
def test_healthy_events_never_alert_and_rearm(event):
    channel = FakeChannel()
    alerter, _ = make(channel)
    monitor = H5RunMonitor(alerter)
    run_ticks(monitor, {"event": event})
    assert channel.sent == []
    run_ticks(
        monitor,
        {"event": "blocked", "error_class": "E"},
        {"event": event},
        {"event": "blocked", "error_class": "E"},
    )
    assert len(channel.sent) == 2  # healthy tick between them ended the episode


def test_cli_exception_payload_signature_uses_error_class():
    channel = FakeChannel()
    alerter, _ = make(channel)
    run_ticks(H5RunMonitor(alerter), {"event": "blocked", "error_class": "HTTPError"})
    assert channel.sent[0].signature == "blocked:HTTPError"


def test_operator_stop_is_never_an_alert_but_any_other_stop_is():
    channel = FakeChannel()
    alerter, _ = make(channel)
    monitor = H5RunMonitor(alerter)
    asyncio.run(monitor.stopped(operator=True, reason="sigint"))
    assert channel.sent == []
    asyncio.run(monitor.stopped(operator=False, reason="sigterm"))
    assert [(a.kind, a.signature) for a in channel.sent] == [
        (AlertKind.STOPPED, "sigterm")
    ]


def test_disabled_monitor_is_inert():
    channel = FakeChannel()
    alerter, _ = make(channel, enabled=False)
    monitor = H5RunMonitor(alerter)
    run_ticks(monitor, {"event": "blocked"})
    asyncio.run(monitor.stopped(operator=False, reason="exception:X"))
    assert channel.sent == [] and monitor.enabled is False


def test_monitor_swallows_a_broken_alerter():
    class Broken:
        enabled = True

        async def fire(self, *a):
            raise RuntimeError("alerter bug")

        def clear(self, *a):
            raise RuntimeError("alerter bug")

    monitor = H5RunMonitor(Broken())  # type: ignore[arg-type]
    run_ticks(monitor, {"event": "blocked"}, {"event": "no_entry"})
    asyncio.run(monitor.stopped(operator=False, reason="x"))


def test_a_slow_webhook_never_delays_the_tick_that_reported_the_failure():
    """Round-1 B1: tick_done returns before the webhook answers."""

    class Gate:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.sent: list[H5Alert] = []

        async def send(self, alert: H5Alert) -> bool:
            await self.release.wait()
            self.sent.append(alert)
            return True

    async def go() -> list[str]:
        gate = Gate()
        monitor = H5RunMonitor(H5Alerter(channel=gate, enabled=True))
        order = []
        await monitor.tick_done({"event": "blocked"})
        order.append("tick_done returned")
        assert gate.sent == []
        gate.release.set()
        await monitor.drain()
        order.append(f"sent={len(gate.sent)}")
        return order

    assert asyncio.run(go()) == ["tick_done returned", "sent=1"]


def test_drain_is_bounded_and_cancels_an_overrunning_send(monkeypatch):
    monkeypatch.setattr(alerting, "DRAIN_TIMEOUT_SECONDS", 0.05)

    class Hang:
        async def send(self, alert: H5Alert) -> bool:
            await asyncio.sleep(30)
            return True

    async def go() -> int:
        monitor = H5RunMonitor(H5Alerter(channel=Hang(), enabled=True, send_timeout=30))
        await monitor.tick_done({"event": "blocked"})
        await monitor.drain()
        await asyncio.sleep(0)
        return len(monitor._pending)

    assert asyncio.run(go()) == 0


def test_drain_with_nothing_pending_is_a_no_op():
    monitor = H5RunMonitor(H5Alerter(channel=FakeChannel(), enabled=True))
    asyncio.run(monitor.drain())


# --- heartbeat -------------------------------------------------------------

MISS = dt.timedelta(minutes=10)


def test_judge_heartbeat_boundaries():
    assert judge_heartbeat(None, now=T0, miss_after=MISS) is HeartbeatVerdict.ABSENT
    assert judge_heartbeat(T0, now=T0, miss_after=MISS) is HeartbeatVerdict.OK
    assert judge_heartbeat(T0 - MISS, now=T0, miss_after=MISS) is HeartbeatVerdict.OK
    just_over = T0 - MISS - dt.timedelta(seconds=1)
    assert (
        judge_heartbeat(just_over, now=T0, miss_after=MISS) is HeartbeatVerdict.MISSED
    )
    future = T0 + dt.timedelta(minutes=3)
    assert judge_heartbeat(future, now=T0, miss_after=MISS) is HeartbeatVerdict.OK


def test_judge_heartbeat_requires_aware_times():
    with pytest.raises(ValueError):
        judge_heartbeat(T0.replace(tzinfo=None), now=T0, miss_after=MISS)
    with pytest.raises(ValueError):
        judge_heartbeat(T0, now=T0.replace(tzinfo=None), miss_after=MISS)


class FakeTicks:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value, self.error = value, error

    async def last_tick_at(self):
        if self.error is not None:
            raise self.error
        return self.value


def poll(state, alerter, now):
    return asyncio.run(poll_heartbeat(state, alerter, miss_after=MISS, now=now))


def test_stale_heartbeat_alerts_once_across_many_polls_then_rearms_on_recovery():
    channel = FakeChannel()
    alerter, clock = make(channel)
    stale = FakeTicks(T0 - dt.timedelta(minutes=30))
    for i in range(10):
        record = poll(stale, alerter, T0 + dt.timedelta(minutes=i))
        clock.advance(minutes=1)
        assert record["verdict"] == "missed"
    assert [a.kind for a in channel.sent] == [AlertKind.HEARTBEAT_MISSED]
    fresh = FakeTicks(T0 + dt.timedelta(minutes=9))
    assert poll(fresh, alerter, T0 + dt.timedelta(minutes=10))["verdict"] == "ok"
    poll(FakeTicks(T0 - dt.timedelta(minutes=30)), alerter, T0 + dt.timedelta(hours=1))
    assert len(channel.sent) == 2


def test_absent_and_fresh_heartbeats_never_alert():
    channel = FakeChannel()
    alerter, _ = make(channel)
    assert poll(FakeTicks(None), alerter, T0)["verdict"] == "absent"
    assert poll(FakeTicks(T0), alerter, T0)["verdict"] == "ok"
    assert channel.sent == []


def test_unreadable_heartbeat_is_an_alert_not_silence():
    channel = FakeChannel()
    alerter, _ = make(channel)
    state = FakeTicks(error=ConnectionRefusedError("db"))
    record = poll(state, alerter, T0)
    poll(state, alerter, T0)
    assert record == {
        "event": "watch",
        "verdict": "unreadable",
        "error_class": "ConnectionRefusedError",
    }
    assert [a.signature for a in channel.sent] == ["unreadable:ConnectionRefusedError"]


def test_poll_record_reports_age_without_secrets():
    record = poll(FakeTicks(T0 - dt.timedelta(minutes=3)), make(FakeChannel())[0], T0)
    assert record["age_seconds"] == 180
    assert record["last_tick_at"] == (T0 - dt.timedelta(minutes=3)).isoformat()


def test_a_stalled_heartbeat_read_is_an_unreadable_alert_not_a_hang():
    """Round-1 B5: the read has a deadline, so the detector cannot wait forever."""

    class Stalled:
        async def last_tick_at(self):
            await asyncio.sleep(30)

    channel = FakeChannel()
    alerter, _ = make(channel)

    async def go():
        return await asyncio.wait_for(
            poll_heartbeat(
                Stalled(), alerter, miss_after=MISS, now=T0, read_timeout=0.05
            ),
            timeout=5,
        )

    record = asyncio.run(go())
    assert record["verdict"] == "unreadable"
    assert record["error_class"] == "TimeoutError"
    assert [a.signature for a in channel.sent] == ["unreadable:TimeoutError"]


def test_changing_unreadable_causes_stay_one_episode():
    channel = FakeChannel()
    alerter, _ = make(channel)
    for error in (ConnectionRefusedError("a"), TimeoutError("b"), OSError("c")):
        poll(FakeTicks(error=error), alerter, T0)
    assert len(channel.sent) == 1


def test_the_read_deadline_default_is_shorter_than_the_poll_pace():
    assert 0 < alerting.READ_TIMEOUT_SECONDS < 60


# --- embed and channel -----------------------------------------------------


@pytest.mark.parametrize("kind", list(AlertKind))
def test_embed_carries_identifiers_only(kind):
    embed = alert_embed(H5Alert(kind, "blocked:ValueError", T0))
    text = json.dumps(embed, ensure_ascii=False)
    assert kind.value in text and "blocked:ValueError" in text
    assert "2026-10-05 03:00:00" in text
    for forbidden in ("http", "token", "secret", "key=", "webhook"):
        assert forbidden not in text.lower(), forbidden
    if kind is AlertKind.TEST:
        assert embed["color"] != 0xE74C3C and not embed["title"].startswith("🚨")
    else:
        assert embed["color"] == 0xE74C3C and embed["title"].startswith("🚨")
        assert alerting.PLAYBOOK_DOC in text


def test_failure_actions_say_h5_has_no_exchange_side_stop():
    for kind in (AlertKind.STOPPED, AlertKind.HEARTBEAT_MISSED):
        action = alert_embed(H5Alert(kind, "x", T0))["fields"][-1]["value"]
        assert "거래소 측 손절이 없다" in action


def test_playbook_named_in_alerts_exists():
    assert (REPO_ROOT / alerting.PLAYBOOK_DOC).is_file()


def test_discord_channel_posts_to_the_ops_webhook_only(monkeypatch):
    calls = []

    async def fake_send(*, http_client, webhook_url, embed):
        calls.append((webhook_url, embed))
        return True

    monkeypatch.setattr(
        "app.monitoring.trade_notifier.transports.send_discord_embed_single",
        fake_send,
    )
    alert = H5Alert(AlertKind.ERROR, "blocked", T0)
    assert asyncio.run(DiscordAlertsChannel("https://example.invalid/hook").send(alert))
    assert calls == [("https://example.invalid/hook", alert_embed(alert))]


@pytest.mark.parametrize("webhook", [None, ""])
def test_unset_webhook_sends_nothing(monkeypatch, webhook):
    async def must_not_run(**kwargs):
        raise AssertionError("no webhook configured")

    monkeypatch.setattr(
        "app.monitoring.trade_notifier.transports.send_discord_embed_single",
        must_not_run,
    )
    alert = H5Alert(AlertKind.ERROR, "blocked", T0)
    assert asyncio.run(DiscordAlertsChannel(webhook).send(alert)) is False


def test_default_channel_reads_the_existing_ops_webhook_setting(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "discord_webhook_alerts", "https://example.invalid/x")
    channel = alerting.build_default_channel()
    assert channel._webhook_url == "https://example.invalid/x"
    source = (REPO_ROOT / "app/services/brokers/binance/h5/alerting.py").read_text()
    assert "discord_webhook_alerts" in source
    for provider in ("hermes", "telegram", "slack", "sentry"):
        assert provider not in source.lower().replace("hermes is not", "")


# --- vocabulary drift ------------------------------------------------------


def _leaf_strings(expr: ast.expr) -> set[str]:
    """String values an expression can evaluate to (IfExp branches, not tests)."""
    if isinstance(expr, ast.IfExp):
        return _leaf_strings(expr.body) | _leaf_strings(expr.orelse)
    assert isinstance(expr, ast.Constant) and isinstance(expr.value, str), ast.dump(
        expr
    )
    return {expr.value}


def _executor_events() -> set[str]:
    tree = ast.parse(
        (REPO_ROOT / "app/services/brokers/binance/h5/executor.py").read_text()
    )
    events: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "H5TickResult"
        ):
            expr = node.args[1] if len(node.args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == "event":
                    expr = keyword.value
            assert expr is not None
            events |= _leaf_strings(expr)
    return events


def test_every_executor_event_is_classified_failure_or_healthy():
    assert FAILURE_EVENTS.isdisjoint(HEALTHY_EVENTS)
    assert _executor_events() == FAILURE_EVENTS | HEALTHY_EVENTS


def test_runner_exit_code_set_is_the_alert_failure_set():
    source = (REPO_ROOT / "scripts/binance_h5_demo.py").read_text()
    assert "FAILURE_EVENTS" in source
    assert '"entry_uncertain"' not in source and '"close_uncertain"' not in source
