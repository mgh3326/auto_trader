from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.services.toss_sellable_cache import (
    TossSellableCache,
    get_shared_sellable_cache,
    reset_shared_sellable_cache,
)

pytestmark = pytest.mark.unit


class TestTossSellableCacheSettings:
    def test_defaults(self):
        s = Settings()
        assert s.toss_sellable_cache_enabled is True
        assert s.toss_sellable_cache_ttl_seconds == 45.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_ENABLED", "false")
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_TTL_SECONDS", "30")
        s = Settings()
        assert s.toss_sellable_cache_enabled is False
        assert s.toss_sellable_cache_ttl_seconds == 30.0


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_shared_sellable_cache()
    yield
    reset_shared_sellable_cache()


class TestTossSellableCache:
    def test_hit_within_ttl(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        assert cache.get("BRK.B") is None  # cold miss
        cache.put("BRK.B", Decimal("1.25"))
        clock.advance(44.9)
        assert cache.get("BRK.B") == Decimal("1.25")  # still fresh

    def test_miss_after_ttl_expiry(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        cache.put("BRK.B", Decimal("1.25"))
        clock.advance(45.0)  # expiry boundary is exclusive
        assert cache.get("BRK.B") is None

    def test_per_symbol_isolation(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        cache.put("AAA", Decimal("3"))
        assert cache.get("BBB") is None
        assert cache.get("AAA") == Decimal("3")

    def test_disabled_is_complete_no_op(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now, enabled=False)
        cache.put("BRK.B", Decimal("1.25"))
        assert cache.get("BRK.B") is None  # never stores => always miss


class TestSingleton:
    def test_shared_instance(self):
        assert get_shared_sellable_cache() is get_shared_sellable_cache()

    def test_reset_drops_instance(self):
        first = get_shared_sellable_cache()
        reset_shared_sellable_cache()
        assert get_shared_sellable_cache() is not first
