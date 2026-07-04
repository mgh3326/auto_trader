from __future__ import annotations

import logging

import httpx
import pytest

from app.core.async_rate_limiter import RateLimitExceededError
from app.core.config import Settings
from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.circuit_breaker import (
    KISCircuitBreaker,
    KISCircuitOpen,
    is_kis_connect_failure,
)

pytestmark = pytest.mark.unit


class TestCircuitBreakerSettings:
    def test_defaults(self):
        s = Settings()
        assert s.kis_circuit_breaker_enabled is True
        assert s.kis_circuit_breaker_failure_threshold == 5
        assert s.kis_circuit_breaker_cooldown_seconds == 45

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_ENABLED", "false")
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "10")
        s = Settings()
        assert s.kis_circuit_breaker_enabled is False
        assert s.kis_circuit_breaker_failure_threshold == 3
        assert s.kis_circuit_breaker_cooldown_seconds == 10


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeSettings:
    def __init__(self, *, enabled=True, threshold=3, cooldown=45) -> None:
        self.kis_circuit_breaker_enabled = enabled
        self.kis_circuit_breaker_failure_threshold = threshold
        self.kis_circuit_breaker_cooldown_seconds = cooldown


@pytest.fixture(autouse=True)
def _reset_singleton():
    cb.reset_kis_circuit_breaker()
    yield
    cb.reset_kis_circuit_breaker()


def _make(**kw):
    clock = _Clock()
    breaker = KISCircuitBreaker(now=clock.now, settings_obj=_FakeSettings(**kw))
    return breaker, clock


class TestConnectClassifier:
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectTimeout(""),
            httpx.ConnectError(""),
            httpx.PoolTimeout(""),
            httpx.ReadTimeout(""),
            ConnectionRefusedError(),
        ],
    )
    def test_connect_failures_classified(self, exc):
        assert is_kis_connect_failure(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.WriteTimeout(""),
            httpx.HTTPStatusError("x", request=None, response=None),
            RateLimitExceededError("throttle"),
            RuntimeError("KIS business error"),
            ValueError("boom"),
        ],
    )
    def test_non_connect_not_classified(self, exc):
        # WriteTimeout stays OUT of the trip set; ReadTimeout is IN (read-hang
        # outage) and is asserted in test_connect_failures_classified above.
        assert is_kis_connect_failure(exc) is False


class TestStateMachine:
    def test_closed_passthrough_until_threshold(self):
        breaker, _ = _make(threshold=3)
        breaker.before_request()  # closed -> no raise
        breaker.record_failure()  # 1
        breaker.record_failure()  # 2
        assert breaker.state == "closed"
        breaker.before_request()  # still closed
        breaker.record_failure()  # 3 -> open
        assert breaker.state == "open"

    def test_success_resets_failure_count(self):
        breaker, _ = _make(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.failure_count == 0
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed"  # not opened: counter was reset

    def test_reachable_error_does_not_trip(self):
        breaker, _ = _make(threshold=2)
        breaker.record_reachable_error()  # 429/business — must not count
        breaker.record_reachable_error()
        breaker.record_reachable_error()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_open_raises_with_zero_side_effects_until_cooldown(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()  # -> open
        assert breaker.state == "open"
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()
        clock.advance(44.9)
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()

    def test_cooldown_transitions_to_half_open_single_probe(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()  # hands out THE probe -> half_open
        assert breaker.state == "half_open"
        # concurrent burst: every other half-open caller fails fast (no stampede)
        for _ in range(5):
            with pytest.raises(KISCircuitOpen):
                breaker.before_request()

    def test_probe_success_closes(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()
        breaker.record_success()
        assert breaker.state == "closed"
        breaker.before_request()  # closed again -> no raise

    def test_probe_reachable_error_closes(self):
        # A 429 during the probe proves KIS is reachable -> close.
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()
        breaker.record_reachable_error()
        assert breaker.state == "closed"

    def test_probe_failure_reopens_and_extends_cooldown(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()  # half_open probe
        breaker.record_failure()  # probe failed -> reopen
        assert breaker.state == "open"
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()  # cooldown restarted from now
        clock.advance(45)
        breaker.before_request()  # probe again
        assert breaker.state == "half_open"

    def test_disabled_is_complete_no_op(self):
        breaker, _ = _make(enabled=False, threshold=1)
        for _ in range(10):
            breaker.record_failure()
        breaker.before_request()  # never raises
        assert breaker.state == "closed"

    def test_open_logs_warning_close_logs_info(self, caplog):
        breaker, clock = _make(threshold=1, cooldown=1)
        with caplog.at_level(logging.INFO):
            breaker.record_failure()  # open -> WARNING
            clock.advance(1)
            breaker.before_request()  # half_open -> INFO
            breaker.record_success()  # close -> INFO
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "open" in text.lower()


class TestSingleton:
    def test_shared_instance(self):
        assert cb.get_kis_circuit_breaker() is cb.get_kis_circuit_breaker()

    def test_reset_drops_instance(self):
        first = cb.get_kis_circuit_breaker()
        cb.reset_kis_circuit_breaker()
        assert cb.get_kis_circuit_breaker() is not first
