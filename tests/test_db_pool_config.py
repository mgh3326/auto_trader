"""ROB-469 PR2: DB engine pool-class selection (env-gated)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from app.core.db import build_engine

_URL = "postgresql+asyncpg://u:p@localhost:5432/db"


@pytest.mark.unit
def test_default_is_async_queue_pool() -> None:
    engine = build_engine(_URL)
    assert type(engine.pool).__name__ == "AsyncAdaptedQueuePool"
    assert engine.pool.size() == 5  # default DB_POOL_SIZE


@pytest.mark.unit
def test_db_pool_class_null_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_CLASS", "null")
    engine = build_engine(_URL)
    assert isinstance(engine.pool, NullPool)


@pytest.mark.unit
def test_env_overrides_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "9")
    engine = build_engine(_URL)
    assert engine.pool.size() == 9
