"""The research budget never exceeds its share of a broker's window, whatever
serving does at the same time (fake clock; nothing sleeps)."""

from __future__ import annotations

import math
import threading
from collections import deque

import pytest

from research.data_plane.budget import (
    BROKER_LIMITS,
    DEFAULT_BUDGETS,
    MAX_RESEARCH_SHARE,
    BudgetError,
    BudgetSpec,
    ResearchBudget,
    check_spec,
    parse_budgets,
)
from research.data_plane.cli import build_parser

from .data_plane_fixtures import FakeClock


def _max_in_window(stamps, width):
    """Largest number of stamps inside any half-open window of ``width``."""
    stamps = sorted(stamps)
    best, lo = 0, 0
    for hi, t in enumerate(stamps):
        while t - stamps[lo] >= width:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


class FakeBroker:
    """Counts every call against the per-key window and refuses beyond it."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.recent: deque[float] = deque()
        self.log: list[tuple[float, str, bool]] = []

    def call(self, now: float, who: str) -> bool:
        while self.recent and now - self.recent[0] >= self.window:
            self.recent.popleft()
        ok = len(self.recent) < self.limit
        if ok:
            self.recent.append(now)
        self.log.append((now, who, ok))
        return ok


@pytest.mark.unit
@pytest.mark.parametrize("broker", sorted(DEFAULT_BUDGETS))
def test_research_never_exceeds_its_share_under_concurrent_serving(broker):
    limit, window = BROKER_LIMITS[broker]
    spec = BudgetSpec(broker, *DEFAULT_BUDGETS[broker])
    clock = FakeClock(start=0.0)
    budget = ResearchBudget(spec, clock=clock.now, sleep=clock.sleep)
    fake = FakeBroker(limit, window)
    serving_share = limit - spec.max_calls
    serving: deque[float] = deque()
    tick = window / 40
    for _ in range(int(30 * window / tick)):  # 30 broker windows
        now = clock.now()
        # serving takes everything the research share leaves, as fast as it can
        while serving and now - serving[0] >= window:
            serving.popleft()
        while len(serving) < serving_share:
            assert fake.call(now, "serving"), f"serving refused at {now}"
            serving.append(now)
        # research is greedy too: it calls whenever its budget allows
        while budget.try_acquire():
            fake.call(now, "research")
        clock.t += tick

    research = [t for t, who, _ in fake.log if who == "research"]
    assert all(ok for _, _, ok in fake.log)  # no call refused, serving or research
    assert _max_in_window(research, window) == spec.max_calls  # used, never exceeded
    assert _max_in_window(research, spec.per_seconds) <= spec.max_calls
    # research still progresses (it is not starved by serving either)
    assert len(research) >= 29 * spec.max_calls


@pytest.mark.unit
def test_concurrent_research_threads_share_one_window():
    clock = FakeClock(start=0.0)
    budget = ResearchBudget(
        BudgetSpec("kis", 2, 1.0), clock=clock.now, sleep=clock.sleep
    )

    def worker():
        for _ in range(10):
            budget.acquire()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert budget.acquired == 80
    assert _max_in_window(list(budget.grants), 1.0) <= 2
    assert clock.now() >= 39.0  # 80 calls at 2/s


@pytest.mark.unit
def test_two_collectors_on_one_state_file_share_one_share(tmp_path):
    """KR and US collectors (separate processes) using the same --root."""
    clock = FakeClock(start=0.0)
    state = tmp_path / ".budget" / "kis.json"
    spec = BudgetSpec("kis", 2, 1.0)
    a = ResearchBudget(spec, state_path=state, clock=clock.now, sleep=clock.sleep)
    b = ResearchBudget(spec, state_path=state, clock=clock.now, sleep=clock.sleep)

    def worker(budget):
        for _ in range(15):
            budget.acquire()

    threads = [threading.Thread(target=worker, args=(x,)) for x in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert a.acquired == b.acquired == 15
    assert _max_in_window(list(a.grants) + list(b.grants), 1.0) <= 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("broker", "calls", "seconds"),
    [
        ("kis", 6, 1.0),  # 30% of 20/s
        ("kis", 20, 1.0),
        ("toss", 2, 1.0),  # 40% of 5/s
        ("kis", 2, 0.5),  # window shorter than the broker's
        ("kis", 0, 1.0),
        ("kis", 2, math.nan),
        ("kis", 2, math.inf),
        ("kiwoom", 1, 1.0),  # no research budget for an unlisted broker
    ],
)
def test_a_budget_above_the_research_share_is_refused(broker, calls, seconds):
    with pytest.raises(BudgetError):
        check_spec(broker, calls, seconds)


@pytest.mark.unit
def test_every_default_and_the_maximum_share_are_accepted():
    for broker, (calls, seconds) in DEFAULT_BUDGETS.items():
        check_spec(broker, calls, seconds)
        limit, _ = BROKER_LIMITS[broker]
        assert calls <= MAX_RESEARCH_SHARE * limit
    assert check_spec("kis", 5, 1.0).max_calls == 5
    assert parse_budgets("kis=3/2")["kis"].text() == "3/2s"
    assert parse_budgets("")["toss"].text() == "1/1s"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["kis=6/1", "kis", "kis=2", "toss=x/1", "nh=1/1"])
def test_cli_refuses_a_malformed_or_oversized_budget(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["collect", "--root", "/x", "--market", "kr", "--broker-budget", value]
        )


def _budget_on(state, clock):
    return ResearchBudget(
        BudgetSpec("kis", 2, 1.0), state_path=state, clock=clock.now, sleep=clock.sleep
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        '{"stamps": "x"}',
        '{"stamps": [1, "a"]}',
        "[]",
        '{"stamps": [true]}',
    ],
)
def test_an_unreadable_state_file_counts_as_a_full_window(tmp_path, content):
    state = tmp_path / "kis.json"
    state.write_text(content)
    clock = FakeClock(start=100.0)
    budget = _budget_on(state, clock)
    assert budget.try_acquire() is False  # fail closed
    clock.t += 1.0
    assert budget.try_acquire() is True  # rewritten: recovers after one window
    assert budget.try_acquire() is True
    assert budget.try_acquire() is False


@pytest.mark.unit
@pytest.mark.parametrize("stamp", ["Infinity", "NaN", "3700.0"])
def test_a_non_finite_or_future_stamp_costs_at_most_one_window(tmp_path, stamp):
    state = tmp_path / "kis.json"
    state.write_text(f'{{"stamps": [{stamp}, {stamp}]}}')
    clock = FakeClock(start=100.0)
    budget = _budget_on(state, clock)
    budget.acquire()
    assert clock.slept <= 1.0  # never a stall of an hour or an OverflowError
    assert _max_in_window(list(budget.grants), 1.0) <= 2


@pytest.mark.unit
def test_a_sleeping_acquire_never_holds_up_try_acquire():
    """#2255 review: the lock covers each attempt, not the wait."""
    clock = FakeClock(start=0.0)
    released = threading.Event()
    sleeping = threading.Event()

    def blocking_sleep(seconds):
        sleeping.set()
        released.wait(timeout=10)
        clock.sleep(seconds)

    budget = ResearchBudget(
        BudgetSpec("kis", 1, 1.0), clock=clock.now, sleep=blocking_sleep
    )
    assert budget.try_acquire() is True  # the window is now full
    waiter = threading.Thread(target=budget.acquire)
    waiter.start()
    assert sleeping.wait(timeout=5)  # the waiter is asleep

    answers = []
    probe = threading.Thread(target=lambda: answers.append(budget.try_acquire()))
    probe.start()
    probe.join(timeout=2)
    alive = probe.is_alive()
    released.set()
    waiter.join(timeout=5)
    probe.join(timeout=5)
    assert not alive, "try_acquire waited for a sleeping acquire"
    assert answers == [False]
    assert budget.acquired == 2
