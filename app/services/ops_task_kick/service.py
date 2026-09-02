"""Dispatch and status mechanics for the narrow ops task-kick surface."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from redis.asyncio import Redis
from taskiq.result_backends.dummy import DummyResultBackend

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.services.ops_task_kick.registry import TaskKickSpec

_IDEMPOTENCY_TTL_SECONDS = 3_600
_IDEMPOTENCY_KEY_PREFIX = "ops-task-kick:idempotency:"


class RedisUnavailableError(RuntimeError):
    """Redis could not establish the idempotency authority."""


class TaskDispatchUnavailableError(RuntimeError):
    """The selected registered task could not be sent to TaskIQ."""


class _RedisClient(Protocol):
    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool | None: ...

    async def get(self, name: str) -> str | bytes | None: ...

    async def delete(self, *names: str) -> int: ...

    async def aclose(self) -> None: ...


def _open_redis() -> _RedisClient:
    return Redis.from_url(settings.get_redis_url(), decode_responses=True)


def idempotency_redis_key(task_name: str, idempotency_key: str) -> str:
    """Keep client keys out of Redis key names and task status responses."""

    material = f"{task_name}\x00{idempotency_key}".encode()
    return _IDEMPOTENCY_KEY_PREFIX + hashlib.sha256(material).hexdigest()


def _decode_task_id(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if not isinstance(value, str) or not value:
        return None
    return value


async def kick_task(
    *,
    task_name: str,
    spec: TaskKickSpec,
    parameters: Mapping[str, Any],
    idempotency_key: str,
) -> tuple[str, bool]:
    """Reserve one TaskIQ ID before dispatching exactly one static task."""

    redis = _open_redis()
    redis_key = idempotency_redis_key(task_name, idempotency_key)
    task_id = uuid.uuid4().hex
    try:
        try:
            reserved = await redis.set(
                redis_key,
                task_id,
                nx=True,
                ex=_IDEMPOTENCY_TTL_SECONDS,
            )
        except Exception as exc:  # Redis must be available to preserve dedupe.
            raise RedisUnavailableError from exc
        if not reserved:
            try:
                prior_task_id = _decode_task_id(await redis.get(redis_key))
            except Exception as exc:
                raise RedisUnavailableError from exc
            if prior_task_id is None:
                raise RedisUnavailableError
            return prior_task_id, True

        task = broker.find_task(task_name)
        if task is None:
            # Registry import should make this impossible; do not turn a
            # configuration drift into an untracked successful-looking kick.
            await redis.delete(redis_key)
            raise TaskDispatchUnavailableError

        task_kwargs = {**parameters, **spec.fixed_kwargs}
        try:
            await task.kicker().with_task_id(task_id).kiq(**task_kwargs)
        except Exception as exc:
            # Delivery outcome is ambiguous once TaskIQ dispatch has started.
            # Keep the reserved ID so a retry cannot enqueue a duplicate.
            raise TaskDispatchUnavailableError from exc
        return task_id, False
    finally:
        await redis.aclose()


def _result_summary(value: Any) -> dict[str, Any]:
    """Expose shape only; Task output can contain broker-facing evidence."""

    if value is None:
        return {"kind": "none"}
    if isinstance(value, Mapping):
        return {"kind": "object", "field_count": len(value)}
    if isinstance(value, list):
        return {"kind": "list", "item_count": len(value)}
    return {"kind": type(value).__name__}


async def get_task_run(task_id: str) -> dict[str, Any]:
    """Return explicit unknown when no usable TaskIQ result backend exists."""

    backend = getattr(broker, "result_backend", None)
    if backend is None or isinstance(backend, DummyResultBackend):
        return {"state": "unknown"}
    try:
        if not await backend.is_result_ready(task_id):
            return {"state": "pending"}
        result = await backend.get_result(task_id, with_logs=False)
    except Exception:
        return {"state": "unknown"}
    return {
        "state": "error" if result.is_err else "done",
        "result": _result_summary(result.return_value),
    }
