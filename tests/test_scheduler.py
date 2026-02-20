import pytest
from taskiq.abc.schedule_source import ScheduleSource

from app.core.scheduler import RedisLeaderScheduleSource


class _FakeSource(ScheduleSource):
    def __init__(self, schedules: list[str]) -> None:
        self.schedules = schedules
        self.get_calls = 0
        self.started = False
        self.stopped = False

    async def startup(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True

    async def get_schedules(self) -> list[str]:
        self.get_calls += 1
        return self.schedules


class _FakeRedis:
    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner
        self.expire_calls = 0
        self.closed = False

    async def get(self, _key: str):
        return self.owner

    async def set(self, _key: str, value: str, *, nx: bool, ex: int):
        if nx and self.owner is None:
            self.owner = value
            return True
        return False

    async def expire(self, _key: str, _ttl: int):
        self.expire_calls += 1
        return True

    async def eval(self, _script: str, _numkeys: int, _key: str, token: str):
        if self.owner == token:
            self.owner = None
            return 1
        return 0

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_leader_source_skips_schedules_if_lock_owned_by_other(monkeypatch):
    base_source = _FakeSource(["schedule"])
    fake_redis = _FakeRedis(owner="other-instance")

    monkeypatch.setattr(
        "app.core.scheduler.redis.from_url",
        lambda *_args, **_kwargs: fake_redis,
    )

    source = RedisLeaderScheduleSource(
        source=base_source,
        redis_url="redis://localhost:6379/0",
    )
    await source.startup()

    schedules = await source.get_schedules()
    assert schedules == []
    assert base_source.get_calls == 0


@pytest.mark.asyncio
async def test_leader_source_runs_delegate_when_lock_owned(monkeypatch):
    base_source = _FakeSource(["schedule"])
    fake_redis = _FakeRedis(owner=None)

    monkeypatch.setattr(
        "app.core.scheduler.redis.from_url",
        lambda *_args, **_kwargs: fake_redis,
    )

    source = RedisLeaderScheduleSource(
        source=base_source,
        redis_url="redis://localhost:6379/0",
    )
    await source.startup()

    first = await source.get_schedules()
    second = await source.get_schedules()

    assert first == ["schedule"]
    assert second == ["schedule"]
    assert base_source.get_calls == 2
    assert fake_redis.expire_calls == 1

    await source.shutdown()
    assert fake_redis.owner is None
    assert fake_redis.closed is True
