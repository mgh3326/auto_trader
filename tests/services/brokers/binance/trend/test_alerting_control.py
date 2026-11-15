"""TREND alerting, kill-switch classification and heartbeat liveness on fakes."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.services.brokers.binance.trend import control
from app.services.brokers.binance.trend.alerting import (
    AlertKind,
    HeartbeatVerdict,
    TrendAlert,
    TrendAlerter,
    TrendRunMonitor,
    judge_heartbeat,
    poll_heartbeat,
)
from app.services.brokers.binance.trend.control import (
    RunnerHeartbeat,
    RunnerLiveness,
    TickFailure,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 15, 4, tzinfo=dt.UTC)


class Channel:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[TrendAlert] = []
        self.fail = fail

    async def send(self, alert: TrendAlert) -> bool:
        if self.fail:
            raise RuntimeError("webhook down")
        self.sent.append(alert)
        return True


def alerter(
    channel: Channel | None, *, enabled: bool = True, clock=None
) -> TrendAlerter:
    return TrendAlerter(channel=channel, enabled=enabled, clock=clock or (lambda: NOW))


def event(name: str, detail: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(symbol="BTCUSDT", event=name, detail=detail)


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    "name",
    [
        "no_complete_4h_bar",
        "no_signal",
        "entry_sent",
        "entry_filled",
        "entry_refused",
        "reconciled_filled",
        "reconciled_no_fill",
        "unsent_released",
        "bars_unavailable",
        "entries_skipped_manage_only",
    ],
)
def test_healthy_events_do_not_alert_and_do_not_fail_the_tick(name):
    assert control.classify_event(name) is None
    assert control.tick_failures((event(name),)) == ()


@pytest.mark.parametrize(
    ("name", "bucket"),
    [
        ("blocked", "blocked"),
        ("entry_conflict_blocked", "entry_conflict_blocked"),
        ("entry_conflict_rejected", "entry_conflict_rejected"),
        ("entry_ledger_incomplete", "entry_ledger_incomplete"),
        ("reconciled_ledger_incomplete", "reconciled_ledger_incomplete"),
        ("exposure_mismatch", "exposure_mismatch"),
        ("partial_entry_exposure_open", "partial_entry_exposure_open"),
        ("partial_entry_stop_breached", "partial_entry_stop_breached"),
        (
            "partial_entry_cancel_unconfirmed",
            "partial_entry_cancel_unconfirmed",
        ),
        ("entry_uncertain", "entry_uncertain"),
        ("exit_uncertain", "exit_uncertain"),
        ("reconciled_uncertain", "reconciled_uncertain"),
        ("entry_aborted:TimeoutError", "entry_aborted"),
        ("exit_aborted:TrendStateBlocked", "exit_aborted"),
    ],
)
def test_failure_events_alert_with_their_own_bucket(name, bucket):
    assert control.classify_event(name) == bucket
    failure = control.tick_failures((event(name, "detail"),))
    assert failure == (TickFailure(f"{name}:detail", bucket),)


def test_the_conflict_week_kill_switch_alerts_but_other_refusals_do_not():
    tripped = event("entry_refused:conflict_week_limit_reached")
    failure = control.tick_failures((tripped,))
    assert failure == (
        TickFailure(
            "entry_refused:conflict_week_limit_reached",
            "kill_switch_conflict_week",
        ),
    )
    # Same event name, other reasons: normal capacity/sizing behaviour.
    for reason in (
        "leg_notional_cap_exceeded",
        "max_concurrent_positions_reached",
        "gross_notional_cap_exceeded",
        "consecutive_hard_stop_limit_reached",
    ):
        assert control.classify_event(f"entry_refused:{reason}") is None
    # A multi-reason refusal still trips the switch if the latch is one of them.
    both = event(
        "entry_refused:gross_notional_cap_exceeded,conflict_week_limit_reached"
    )
    assert control.tick_failures((both,))[0].bucket == "kill_switch_conflict_week"


def test_one_failure_per_bucket_first_occurrence_wins():
    failures = control.tick_failures(
        (
            event("blocked", "first"),
            event("blocked", "second"),
            event("entry_uncertain"),
            event("no_signal"),
        )
    )
    assert failures == (
        TickFailure("blocked:first", "blocked"),
        TickFailure("entry_uncertain", "entry_uncertain"),
    )


def test_a_non_event_object_is_ignored():
    assert control.tick_failures((SimpleNamespace(event=object()),)) == ()


def test_stop_signature_uses_the_class_only():
    assert (
        control.stop_signature(TimeoutError("secret url")) == "exception:TimeoutError"
    )


# ---------------------------------------------------------------- alerter


@pytest.mark.asyncio
async def test_one_alert_per_episode_with_a_bounded_reminder():
    channel = Channel()
    clock = {"now": NOW}
    a = alerter(channel, clock=lambda: clock["now"])
    assert await a.fire(AlertKind.ERROR, "blocked:x", "blocked") is True
    assert len(channel.sent) == 1
    clock["now"] = NOW + dt.timedelta(hours=5)
    assert await a.fire(AlertKind.ERROR, "blocked:x", "blocked") is False
    clock["now"] = NOW + dt.timedelta(hours=6, minutes=1)
    assert await a.fire(AlertKind.ERROR, "blocked:x", "blocked") is True
    assert len(channel.sent) == 2
    # A different bucket is a different episode.
    assert await a.fire(AlertKind.ERROR, "entry_uncertain", "entry_uncertain") is True
    assert len(channel.sent) == 3


@pytest.mark.asyncio
async def test_clear_ends_the_episode_so_the_next_occurrence_alerts():
    channel = Channel()
    a = alerter(channel)
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is True
    a.clear(AlertKind.ERROR)
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is True
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_a_failed_delivery_is_retried_after_the_backoff_not_immediately():
    channel = Channel(fail=True)
    clock = {"now": NOW}
    a = alerter(channel, clock=lambda: clock["now"])
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is False
    channel.fail = False
    clock["now"] = NOW + dt.timedelta(minutes=4)
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is False
    clock["now"] = NOW + dt.timedelta(minutes=5, seconds=1)
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is True


@pytest.mark.asyncio
async def test_the_flag_off_sends_nothing_even_with_a_channel():
    channel = Channel()
    a = alerter(channel, enabled=False)
    assert a.enabled is False
    assert await a.fire(AlertKind.ERROR, "blocked", "blocked") is False
    assert channel.sent == []


@pytest.mark.asyncio
async def test_no_channel_configured_is_off():
    a = alerter(None)
    assert a.enabled is False
    assert await a.fire(AlertKind.STOPPED, "sigterm") is False


# --------------------------------------------------------------- monitor


@pytest.mark.asyncio
async def test_a_broken_webhook_never_raises_out_of_the_monitor():
    monitor = TrendRunMonitor(alerter(Channel(fail=True)))
    await monitor.tick_done((("blocked", "blocked"),))
    await monitor.drain()  # waits for the background send; the failure is swallowed


@pytest.mark.asyncio
async def test_the_disabled_monitor_starts_no_task_and_clears_nothing():
    channel = Channel()
    monitor = TrendRunMonitor(alerter(channel, enabled=False))
    assert monitor.enabled is False
    await monitor.tick_done((("blocked", "blocked"),))
    assert channel.sent == []


@pytest.mark.asyncio
async def test_a_healthy_tick_clears_the_error_episode_then_a_new_one_alerts():
    channel = Channel()
    monitor = TrendRunMonitor(alerter(channel))
    await monitor.tick_done((("blocked", "blocked"),))
    await monitor.drain()
    assert len(channel.sent) == 1
    await monitor.tick_done(())
    await monitor.tick_done((("blocked", "blocked"),))
    await monitor.drain()
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_a_resolved_bucket_rearms_even_while_another_failure_persists():
    channel = Channel()
    monitor = TrendRunMonitor(alerter(channel))
    partial = ("partial_entry_exposure_open", "partial_entry_exposure_open")
    await monitor.tick_done((("blocked:first", "blocked"), partial))
    await monitor.drain()
    assert [a.signature for a in channel.sent] == [
        "blocked:first",
        "partial_entry_exposure_open",
    ]
    # ``blocked`` resolves; the partial-exposure episode stays open inside 6h.
    await monitor.tick_done((partial,))
    await monitor.drain()
    assert len(channel.sent) == 2
    # A renewed ``blocked`` within six hours must alert: its bucket was cleared.
    await monitor.tick_done((("blocked:second", "blocked"), partial))
    await monitor.drain()
    assert [a.signature for a in channel.sent] == [
        "blocked:first",
        "partial_entry_exposure_open",
        "blocked:second",
    ]


@pytest.mark.asyncio
async def test_the_operator_stop_is_never_alerted_but_any_other_is():
    channel = Channel()
    monitor = TrendRunMonitor(alerter(channel))
    await monitor.stopped(operator=True, reason="sigint")
    assert channel.sent == []
    await monitor.stopped(operator=False, reason="sigterm")
    assert [a.kind for a in channel.sent] == [AlertKind.STOPPED]


# -------------------------------------------------------------- heartbeat


def test_judge_heartbeat_distinguishes_absent_stale_and_fresh():
    miss = dt.timedelta(minutes=10)
    assert judge_heartbeat(None, now=NOW, miss_after=miss) is HeartbeatVerdict.ABSENT
    assert (
        judge_heartbeat(NOW - dt.timedelta(minutes=9), now=NOW, miss_after=miss)
        is HeartbeatVerdict.OK
    )
    assert (
        judge_heartbeat(NOW - dt.timedelta(minutes=11), now=NOW, miss_after=miss)
        is HeartbeatVerdict.MISSED
    )
    with pytest.raises(ValueError):
        judge_heartbeat(dt.datetime(2026, 1, 1), now=NOW, miss_after=miss)


class Lane:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value, self.error = value, error

    async def last_tick_at(self):
        if self.error:
            raise self.error
        return self.value


@pytest.mark.asyncio
async def test_absence_alerts_only_after_this_watcher_has_seen_the_runner():
    channel = Channel()
    a = alerter(channel)
    # Never started: absence alone cannot distinguish that from a death.
    first = await poll_heartbeat(
        Lane(None), a, miss_after=dt.timedelta(minutes=10), now=NOW
    )
    assert first["verdict"] == "absent" and first["runner_seen"] is False
    assert channel.sent == []
    # The runner was seen at least once; now the lock disappearing is a death.
    seen = await poll_heartbeat(
        Lane(NOW - dt.timedelta(minutes=1)),
        a,
        miss_after=dt.timedelta(minutes=10),
        now=NOW,
        seen_before=first["runner_seen"],
    )
    assert seen["verdict"] == "ok" and seen["runner_seen"] is True
    gone = await poll_heartbeat(
        Lane(None),
        a,
        miss_after=dt.timedelta(minutes=10),
        now=NOW,
        seen_before=seen["runner_seen"],
    )
    assert gone["verdict"] == "absent" and gone["runner_seen"] is True
    assert [x.signature for x in channel.sent] == ["runner_absent"]


@pytest.mark.asyncio
async def test_a_stale_tick_alerts_and_a_fresh_one_rearms():
    channel = Channel()
    a = alerter(channel)
    stale = await poll_heartbeat(
        Lane(NOW - dt.timedelta(minutes=11)),
        a,
        miss_after=dt.timedelta(minutes=10),
        now=NOW,
    )
    assert stale["verdict"] == "missed"
    assert stale["age_seconds"] == 660
    assert [x.signature for x in channel.sent] == ["stale_tick"]
    fresh = await poll_heartbeat(
        Lane(NOW - dt.timedelta(minutes=1)),
        a,
        miss_after=dt.timedelta(minutes=10),
        now=NOW,
    )
    assert fresh["verdict"] == "ok"
    assert a._episodes == {}


@pytest.mark.asyncio
async def test_an_unreadable_liveness_is_its_own_alert():
    channel = Channel()
    record = await poll_heartbeat(
        Lane(error=TimeoutError("db down")),
        alerter(channel),
        miss_after=dt.timedelta(minutes=10),
        now=NOW,
    )
    assert record == {
        "event": "watch",
        "verdict": "unreadable",
        "error_class": "TimeoutError",
        "runner_seen": False,
    }
    assert [x.signature for x in channel.sent] == ["unreadable:TimeoutError"]


# ----------------------------------------------------------- runner lock


class FakeEngine:
    """Engine double: each ``connect`` returns a fresh pinned connection."""

    def __init__(
        self,
        *,
        granted: bool = True,
        connect_error: Exception | None = None,
        lock_error: Exception | None = None,
        stamp_error: Exception | None = None,
        unlock_ok: bool = True,
    ) -> None:
        self.granted = granted
        self.connect_error = connect_error
        self.lock_error = lock_error
        self.stamp_error = stamp_error
        self.unlock_ok = unlock_ok
        self.connections: list[FakeConnection] = []

    async def connect(self) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn


class FakeConnection:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.commits = 0
        self.closed = False
        self.invalidated = False
        self.stamps = 0
        self.ok_stamps = 0
        self.rollbacks = 0

    async def scalar(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            if self.engine.lock_error is not None:
                raise self.engine.lock_error
            return self.engine.granted
        if "pg_advisory_unlock" in sql:
            return self.engine.unlock_ok
        if sql.strip().upper() == "SELECT 1":
            self.stamps += 1
            if self.engine.stamp_error is not None:
                raise self.engine.stamp_error
            self.ok_stamps += 1
            return 1
        raise AssertionError(sql)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        # A rollback ends the aborted transaction, so a modelled transient
        # statement error recovers -- exactly what the real connection needs.
        self.rollbacks += 1
        self.engine.stamp_error = None

    async def invalidate(self) -> None:
        self.invalidated = True
        self.closed = True

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_open_reports_held_by_another_runner_distinctly():
    engine = FakeEngine(granted=False)
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.HELD_BY_OTHER
    assert engine.connections[0].closed is True
    assert await heartbeat.stamp() is False
    assert heartbeat._conn is None


@pytest.mark.asyncio
async def test_open_reports_a_database_outage_as_unavailable_not_a_runner():
    engine = FakeEngine(connect_error=ConnectionError("db down"))
    assert await RunnerHeartbeat(engine).open() is control.LivenessOpen.UNAVAILABLE
    engine = FakeEngine(lock_error=RuntimeError("statement failed"))
    assert await RunnerHeartbeat(engine).open() is control.LivenessOpen.UNAVAILABLE
    assert engine.connections[0].closed is True


@pytest.mark.asyncio
async def test_the_loop_pins_one_connection_stamps_and_unlocks_it():
    engine = FakeEngine()
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.ACQUIRED
    conn = engine.connections[0]
    assert heartbeat._conn is conn
    assert await heartbeat.stamp() is True
    assert await heartbeat.stamp() is True
    # Every stamp ran on the same checked-out connection: no pool round trip.
    assert len(engine.connections) == 1
    assert conn.stamps == 2
    assert conn.commits >= 3  # the lock, then each stamp transaction
    await heartbeat.close()
    assert conn.closed is True and conn.invalidated is False
    assert heartbeat._conn is None


@pytest.mark.asyncio
async def test_a_transient_stamp_error_rolls_back_and_the_next_stamp_works():
    engine = FakeEngine(stamp_error=RuntimeError("statement cancelled"))
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.ACQUIRED
    conn = engine.connections[0]
    assert await heartbeat.stamp() is True  # logged, transaction rolled back
    assert conn.rollbacks == 1
    assert heartbeat._conn is conn and conn.invalidated is False
    # No manual reset: the rollback ended the aborted transaction, so the
    # pinned connection is usable again instead of failing every later stamp.
    assert await heartbeat.stamp() is True
    # The second stamp really ran a healthy query (not just returned True).
    assert conn.stamps == 2 and conn.ok_stamps == 1


@pytest.mark.asyncio
async def test_a_statement_timeout_on_a_live_backend_does_not_stop_the_loop():
    engine = FakeEngine(stamp_error=TimeoutError("command_timeout"))
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.ACQUIRED
    conn = engine.connections[0]
    assert await heartbeat.stamp() is True  # rollback, not invalidation
    assert conn.invalidated is False and conn.rollbacks == 1
    assert heartbeat._conn is conn
    assert await heartbeat.stamp() is True
    assert conn.ok_stamps == 1


@pytest.mark.asyncio
async def test_only_a_proven_dead_backend_stops_the_loop_and_is_invalidated():
    engine = FakeEngine(stamp_error=ConnectionError("backend gone"))
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.ACQUIRED
    conn = engine.connections[0]
    assert await heartbeat.stamp() is False
    assert heartbeat._conn is None
    assert conn.invalidated is True  # never returned to the pool while locked


@pytest.mark.asyncio
async def test_an_unlock_failure_invalidates_instead_of_pooling_a_locked_backend():
    engine = FakeEngine(unlock_ok=False, lock_error=None)
    heartbeat = RunnerHeartbeat(engine)
    assert await heartbeat.open() is control.LivenessOpen.ACQUIRED
    conn = engine.connections[0]

    async def broken_unlock(statement, params=None):
        if "pg_advisory_unlock" in str(statement):
            raise RuntimeError("unlock failed")
        return 1

    conn.scalar = broken_unlock  # type: ignore[method-assign]
    await heartbeat.close()
    assert conn.invalidated is True
    assert heartbeat._conn is None


# --------------------------------------------------------------- liveness


class ClockSession:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def scalar(self, statement, params=None):
        return self.value


@pytest.mark.asyncio
async def test_the_watcher_reads_the_lock_stamp_as_an_aware_utc_time():
    reader = RunnerLiveness(lambda: ClockSession(NOW.replace(tzinfo=None)))
    assert await reader.last_tick_at() == NOW
    assert await RunnerLiveness(lambda: ClockSession(NOW)).last_tick_at() == NOW
    assert await RunnerLiveness(lambda: ClockSession(None)).last_tick_at() is None


@pytest.mark.asyncio
async def test_liveness_sql_is_a_select_of_locks_and_activity_only():
    source = control._LIVENESS_SQL.text.lower()
    assert "pg_locks" in source and "pg_stat_activity" in source
    assert "advisory" in source
    for forbidden in ("insert", "update", "delete", " for update", " pg_sleep"):
        assert forbidden not in source
