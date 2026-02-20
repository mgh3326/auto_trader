from __future__ import annotations

from typing import Any

import pytest

from app.core import task_lock


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, num_keys: int, key: str, token: str) -> int:
        del script, num_keys
        if self.values.get(key) == token:
            self.values.pop(key, None)
            return 1
        return 0

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_acquire_task_lock_returns_lock_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    lock = await task_lock.acquire_task_lock(
        lock_key="auto-trader:task-lock:test",
        ttl_seconds=30,
        redis_url="redis://test",
    )

    assert lock is not None
    assert fake_redis.values[lock.lock_key] == lock.token
    assert fake_redis.closed is False


@pytest.mark.asyncio
async def test_acquire_task_lock_returns_none_when_already_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.values["auto-trader:task-lock:test"] = "existing-token"
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    lock = await task_lock.acquire_task_lock(
        lock_key="auto-trader:task-lock:test",
        ttl_seconds=30,
        redis_url="redis://test",
    )

    assert lock is None
    assert fake_redis.values["auto-trader:task-lock:test"] == "existing-token"
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_task_lock_release_uses_lua_compare_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    lock = await task_lock.acquire_task_lock(
        lock_key="auto-trader:task-lock:test",
        ttl_seconds=30,
        redis_url="redis://test",
    )
    assert lock is not None

    released = await lock.release()

    assert released is True
    assert "auto-trader:task-lock:test" not in fake_redis.values
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_run_with_task_lock_returns_skipped_when_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.values["auto-trader:task-lock:test"] = "existing-token"
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    coro_called = False

    async def _coro_factory() -> dict[str, Any]:
        nonlocal coro_called
        coro_called = True
        return {"status": "completed"}

    result = await task_lock.run_with_task_lock(
        lock_key="auto-trader:task-lock:test",
        ttl_seconds=30,
        coro_factory=_coro_factory,
        redis_url="redis://test",
    )

    assert result == {
        "status": "skipped",
        "reason": "lock_held",
        "lock_key": "auto-trader:task-lock:test",
    }
    assert coro_called is False


@pytest.mark.asyncio
async def test_run_with_task_lock_executes_coro_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    async def _coro_factory() -> dict[str, Any]:
        return {"status": "completed", "count": 1}

    result = await task_lock.run_with_task_lock(
        lock_key="auto-trader:task-lock:test",
        ttl_seconds=30,
        coro_factory=_coro_factory,
        redis_url="redis://test",
    )

    assert result == {"status": "completed", "count": 1}
    assert "auto-trader:task-lock:test" not in fake_redis.values
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_run_with_task_lock_releases_lock_when_coro_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "app.core.task_lock.redis.from_url", lambda *_args, **_kwargs: fake_redis
    )

    async def _coro_factory() -> dict[str, Any]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await task_lock.run_with_task_lock(
            lock_key="auto-trader:task-lock:test",
            ttl_seconds=30,
            coro_factory=_coro_factory,
            redis_url="redis://test",
        )

    assert "auto-trader:task-lock:test" not in fake_redis.values
    assert fake_redis.closed is True
