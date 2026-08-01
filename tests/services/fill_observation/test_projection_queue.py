from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.fill_observation import projection as projection_module
from app.services.fill_observation.errors import (
    FillProjectionCursorRegression,
    FillProjectionLeaseMismatch,
)
from app.services.fill_observation.projection import FillProjectionQueue

pytestmark = pytest.mark.unit


class _AsyncContext:
    def __init__(self, value: object, on_enter: object | None = None) -> None:
        self.value = value
        self.on_enter = on_enter

    async def __aenter__(self) -> object:
        if callable(self.on_enter):
            self.on_enter()
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.transactions = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _AsyncContext:
        return _AsyncContext(None, self._begin)

    def _begin(self) -> None:
        self.transactions += 1


class _FakeProjectionRepository:
    def __init__(self) -> None:
        self.outbox: object | None = None
        self.observation = SimpleNamespace(
            id=7,
            observation_identity="a" * 64,
        )
        self.cursor: object | None = None
        self.added_cursor: object | None = None
        self.claimed: list[object] = []
        self.lock_keys: list[int] = []
        self.flushed = 0

    async def claim_ready(self, **_kwargs: object) -> list[object]:
        return self.claimed

    async def get_observation(self, _observation_id: int) -> object:
        return self.observation

    async def get_outbox_for_update(self, _outbox_id: int) -> object | None:
        return self.outbox

    async def lock_projection_partition(self, lock_key: int) -> None:
        self.lock_keys.append(lock_key)

    async def get_cursor_for_update(self, **_kwargs: object) -> object | None:
        return self.cursor

    def add_cursor(self, **kwargs: object) -> None:
        observation = kwargs["observation"]
        self.added_cursor = SimpleNamespace(
            projection_name=kwargs["projection_name"],
            partition_key=kwargs["partition_key"],
            last_fill_observation_id=observation.id,  # type: ignore[attr-defined]
            last_observation_identity=(
                observation.observation_identity  # type: ignore[attr-defined]
            ),
        )

    async def flush(self) -> None:
        self.flushed += 1


def _queue(
    monkeypatch: pytest.MonkeyPatch,
    repository: _FakeProjectionRepository,
    *,
    now: datetime,
    token: uuid.UUID,
) -> tuple[FillProjectionQueue, _FakeSession]:
    session = _FakeSession()
    monkeypatch.setattr(
        projection_module,
        "FillProjectionRepository",
        lambda _session: repository,
    )
    queue = FillProjectionQueue(
        lambda: session,  # type: ignore[arg-type]
        clock=lambda: now,
        token_factory=lambda: token,
    )
    return queue, session


@pytest.mark.asyncio
async def test_claim_returns_durable_delivery_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    token = uuid.UUID("11111111-1111-1111-1111-111111111111")
    repository = _FakeProjectionRepository()
    repository.claimed = [
        SimpleNamespace(
            id=3,
            delivery_key="b" * 64,
            projection_name="legacy_dual_read_validation.v1",
            partition_key="c" * 64,
            fill_observation_id=7,
            attempt_count=2,
        )
    ]
    queue, session = _queue(
        monkeypatch,
        repository,
        now=now,
        token=token,
    )

    deliveries = await queue.claim(projection_name="legacy_dual_read_validation.v1")

    assert len(deliveries) == 1
    assert deliveries[0].observation_identity == "a" * 64
    assert deliveries[0].attempt_count == 2
    assert deliveries[0].lease_token == token
    assert session.transactions == 1


@pytest.mark.asyncio
async def test_completion_advances_cursor_and_outbox_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    token = uuid.UUID("22222222-2222-2222-2222-222222222222")
    repository = _FakeProjectionRepository()
    repository.outbox = SimpleNamespace(
        id=3,
        state="processing",
        lease_token=token,
        lease_expires_at=now,
        projection_name="legacy_dual_read_validation.v1",
        partition_key="c" * 64,
        fill_observation_id=7,
        last_error="old",
        completed_at=None,
        updated_at=None,
    )
    queue, session = _queue(
        monkeypatch,
        repository,
        now=now,
        token=token,
    )

    await queue.complete(outbox_id=3, lease_token=token)

    outbox = repository.outbox
    assert outbox is not None
    assert outbox.state == "succeeded"  # type: ignore[attr-defined]
    assert outbox.completed_at == now  # type: ignore[attr-defined]
    assert outbox.lease_token is None  # type: ignore[attr-defined]
    assert repository.added_cursor is not None
    assert (
        repository.added_cursor.last_observation_identity  # type: ignore[attr-defined]
        == "a" * 64
    )
    assert len(repository.lock_keys) == 1
    assert -(2**63) <= repository.lock_keys[0] < 2**63
    assert repository.flushed == 1
    assert session.transactions == 1


@pytest.mark.asyncio
async def test_retry_persists_error_and_clears_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    token = uuid.UUID("33333333-3333-3333-3333-333333333333")
    repository = _FakeProjectionRepository()
    repository.outbox = SimpleNamespace(
        state="processing",
        lease_token=token,
        lease_expires_at=now,
        last_error=None,
        completed_at=None,
        available_at=now,
        updated_at=None,
    )
    queue, session = _queue(
        monkeypatch,
        repository,
        now=now,
        token=token,
    )

    await queue.retry(
        outbox_id=3,
        lease_token=token,
        error="projection failed",
        retry_after_seconds=30,
    )

    outbox = repository.outbox
    assert outbox is not None
    assert outbox.state == "retry"  # type: ignore[attr-defined]
    assert outbox.last_error == "projection failed"  # type: ignore[attr-defined]
    assert outbox.lease_token is None  # type: ignore[attr-defined]
    assert int((outbox.available_at - now).total_seconds()) == 30  # type: ignore[attr-defined]
    assert repository.flushed == 1
    assert session.transactions == 1


@pytest.mark.asyncio
async def test_completion_with_wrong_lease_fails_before_cursor_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    token = uuid.UUID("44444444-4444-4444-4444-444444444444")
    repository = _FakeProjectionRepository()
    repository.outbox = SimpleNamespace(
        state="processing",
        lease_token=token,
    )
    queue, _session = _queue(
        monkeypatch,
        repository,
        now=now,
        token=token,
    )

    with pytest.raises(FillProjectionLeaseMismatch):
        await queue.complete(outbox_id=3, lease_token=uuid.uuid4())

    assert repository.added_cursor is None
    assert repository.flushed == 0


@pytest.mark.asyncio
async def test_completion_older_than_cursor_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    token = uuid.UUID("55555555-5555-5555-5555-555555555555")
    repository = _FakeProjectionRepository()
    repository.outbox = SimpleNamespace(
        state="processing",
        lease_token=token,
        projection_name="legacy_dual_read_validation.v1",
        partition_key="c" * 64,
        fill_observation_id=7,
        last_error=None,
        completed_at=None,
        updated_at=None,
    )
    repository.cursor = SimpleNamespace(
        last_fill_observation_id=8,
        last_observation_identity="b" * 64,
        advanced_at=now,
        updated_at=now,
    )
    queue, _session = _queue(
        monkeypatch,
        repository,
        now=now,
        token=token,
    )

    with pytest.raises(FillProjectionCursorRegression):
        await queue.complete(outbox_id=3, lease_token=token)

    assert repository.outbox.state == "processing"  # type: ignore[union-attr]
    assert repository.flushed == 0
