"""Assertion-RED mutants for every branch of the H5 alert decision functions.

Branches are counted from ``alerting.py`` ON DISK: every ``if``/``elif`` inside
the decision functions below. Each mutant compiles a copy of the module with
ONE branch condition forced false and runs the scenario only that branch should
satisfy; the real module passes it, the mutant must fail it. A new branch
without a declared invariant fails ``test_every_branch_has_a_mutant``; an
invariant sentence in the module docstring below without a branch (or the other
way round) fails ``test_invariant_sentences_match_the_declared_mutants``.

Invariant sentences (one per mutant):
- DISABLED: a disabled alerter, or one without a channel, sends nothing.
- SAME_EPISODE: the same failure is one episode, however often it recurs.
- DELIVERED_BRANCH: a delivered failure stays quiet until its reminder window.
- REPEAT_WINDOW: a delivered failure is never re-sent inside the reminder window.
- RETRY_BACKOFF: a failed delivery is not retried inside the retry back-off.
- OPERATOR_STOP: the operator's own stop (Ctrl-C) is never an alert.
- FAILURE_EVENT: a blocked or uncertain tick is an error alert.
- NO_STAMP: a lane that has never ticked is absent, not a failure.
- AWARE_ONLY: a naive timestamp is refused, never compared.
- STALE_THRESHOLD: a stamp older than the miss window is a missed heartbeat.
- MISSED_ALERTS: a missed heartbeat alerts, a healthy one only re-arms.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from app.services.brokers.binance.h5 import alerting

pytestmark = pytest.mark.unit

SOURCE = Path(alerting.__file__)
T0 = dt.datetime(2026, 10, 5, 3, 0, tzinfo=dt.UTC)
MISS = dt.timedelta(minutes=10)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += dt.timedelta(**kwargs)


class Channel:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[object] = []
        self.result = result

    async def send(self, alert: object) -> bool:
        self.sent.append(alert)
        return self.result


def alerter(m, channel, *, enabled=True):
    clock = Clock()
    return m.H5Alerter(channel=channel, enabled=enabled, clock=clock), clock


def fire(m, a, signature="sig"):
    return asyncio.run(a.fire(m.AlertKind.ERROR, signature))


def sc_disabled(m):
    channel = Channel()
    a, _ = alerter(m, channel, enabled=False)
    fire(m, a)
    assert channel.sent == []
    a, _ = alerter(m, None)
    assert fire(m, a) is False


def sc_same_episode(m):
    channel = Channel()
    a, clock = alerter(m, channel)
    fire(m, a)
    clock.advance(minutes=1)
    fire(m, a)
    assert len(channel.sent) == 1


def sc_delivered_branch(m):
    channel = Channel()
    a, clock = alerter(m, channel)
    fire(m, a)
    clock.advance(minutes=10)
    fire(m, a)
    assert len(channel.sent) == 1


def sc_repeat_window(m):
    channel = Channel()
    a, _ = alerter(m, channel)
    fire(m, a)
    fire(m, a)
    assert len(channel.sent) == 1


def sc_retry_backoff(m):
    channel = Channel(result=False)
    a, _ = alerter(m, channel)
    fire(m, a)
    fire(m, a)
    assert len(channel.sent) == 1


def sc_operator_stop(m):
    channel = Channel()
    a, _ = alerter(m, channel)
    asyncio.run(m.H5RunMonitor(a).stopped(operator=True, reason="sigint"))
    assert channel.sent == []


def sc_failure_event(m):
    channel = Channel()
    a, _ = alerter(m, channel)
    asyncio.run(m.H5RunMonitor(a).tick_done({"event": "blocked"}))
    assert len(channel.sent) == 1


def sc_no_stamp(m):
    assert m.judge_heartbeat(None, now=T0, miss_after=MISS) is m.HeartbeatVerdict.ABSENT


def sc_aware_only(m):
    with pytest.raises(ValueError):
        m.judge_heartbeat(T0.replace(tzinfo=None), now=T0, miss_after=MISS)


def sc_stale_threshold(m):
    stale = T0 - MISS - dt.timedelta(seconds=1)
    assert (
        m.judge_heartbeat(stale, now=T0, miss_after=MISS) is m.HeartbeatVerdict.MISSED
    )
    assert (
        m.judge_heartbeat(T0 - MISS, now=T0, miss_after=MISS) is m.HeartbeatVerdict.OK
    )


def sc_missed_alerts(m):
    class State:
        async def last_tick_at(self):
            return T0 - dt.timedelta(hours=1)

    channel = Channel()
    a, _ = alerter(m, channel)
    asyncio.run(m.poll_heartbeat(State(), a, miss_after=MISS, now=T0))
    assert len(channel.sent) == 1


# (function, branch condition source) -> (invariant key, scenario)
DECLARED: dict[tuple[str, str], tuple[str, Callable[[types.ModuleType], None]]] = {
    ("H5Alerter.fire", "not self._enabled or channel is None"): (
        "DISABLED",
        sc_disabled,
    ),
    (
        "H5Alerter.fire",
        "episode is not None and episode.signature == cleaned",
    ): ("SAME_EPISODE", sc_same_episode),
    ("H5Alerter.fire", "episode.delivered_at is not None"): (
        "DELIVERED_BRANCH",
        sc_delivered_branch,
    ),
    ("H5Alerter.fire", "now - episode.delivered_at < self._repeat_after"): (
        "REPEAT_WINDOW",
        sc_repeat_window,
    ),
    ("H5Alerter.fire", "now - episode.attempted_at < self._retry_after"): (
        "RETRY_BACKOFF",
        sc_retry_backoff,
    ),
    ("H5RunMonitor.stopped", "operator"): ("OPERATOR_STOP", sc_operator_stop),
    ("H5RunMonitor.tick_done", "payload.get('event') in FAILURE_EVENTS"): (
        "FAILURE_EVENT",
        sc_failure_event,
    ),
    ("judge_heartbeat", "last_tick_at is None"): ("NO_STAMP", sc_no_stamp),
    (
        "judge_heartbeat",
        "last_tick_at.tzinfo is None or now.tzinfo is None",
    ): ("AWARE_ONLY", sc_aware_only),
    ("judge_heartbeat", "now - last_tick_at > miss_after"): (
        "STALE_THRESHOLD",
        sc_stale_threshold,
    ),
    ("poll_heartbeat", "verdict is HeartbeatVerdict.MISSED"): (
        "MISSED_ALERTS",
        sc_missed_alerts,
    ),
}
DECIDING = {"H5Alerter", "H5RunMonitor"}
DECIDING_FUNCTIONS = {"judge_heartbeat", "poll_heartbeat"}


def _branches(tree: ast.Module) -> list[tuple[str, str, ast.If]]:
    found: list[tuple[str, str, ast.If]] = []

    def visit_function(qualified: str, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.If):
                found.append((qualified, ast.unparse(child.test), child))

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in DECIDING:
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit_function(f"{node.name}.{item.name}", item)
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in DECIDING_FUNCTIONS
        ):
            visit_function(node.name, node)
    return found


def _mutant_module(target: tuple[str, str]) -> types.ModuleType:
    tree = ast.parse(SOURCE.read_text("utf-8"))
    hits = [n for q, t, n in _branches(tree) if (q, t) == target]
    assert len(hits) == 1, target
    hits[0].test = ast.Constant(False)
    ast.fix_missing_locations(tree)
    name = (
        "h5_alerting_mutant_"
        + "".join(c if c.isalnum() else "_" for c in target[0] + target[1])[:60]
    )
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(SOURCE), "exec"), module.__dict__)  # noqa: S102
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def test_every_branch_has_a_mutant():
    on_disk = {(q, t) for q, t, _ in _branches(ast.parse(SOURCE.read_text("utf-8")))}
    assert on_disk == set(DECLARED), {
        "undeclared": sorted(on_disk - set(DECLARED)),
        "stale": sorted(set(DECLARED) - on_disk),
    }


def test_invariant_sentences_match_the_declared_mutants():
    doc = __doc__ or ""
    keys = [
        line[2:].split(":", 1)[0]
        for line in doc.splitlines()
        if line.startswith("- ") and ": " in line
    ]
    declared = [key for key, _ in DECLARED.values()]
    assert sorted(keys) == sorted(declared)
    assert len(set(declared)) == len(declared) == 11


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_scenario_passes_on_the_real_module(target):
    DECLARED[target][1](alerting)


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_mutant_is_killed_by_its_invariant(target):
    key, scenario = DECLARED[target]
    mutant = _mutant_module(target)
    try:
        with pytest.raises(BaseException) as killed:  # noqa: PT011
            scenario(mutant)
        assert not isinstance(killed.value, KeyboardInterrupt), key
    finally:
        sys.modules.pop(mutant.__name__, None)
