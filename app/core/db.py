import os
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings

# MCP 서버에서는 stdout 오염 방지를 위해 echo=False 필수
_echo = os.getenv("SQLALCHEMY_ECHO", "false").lower() in ("true", "1", "yes")


def build_engine(database_url: str | None = None) -> AsyncEngine:
    """Build the shared async engine.

    ROB-469 PR2: default to the async queue pool (AsyncAdaptedQueuePool) instead of
    NullPool. NullPool opened a fresh connection per request — wasteful, and under
    load the connect itself piles up and can stall the event loop. The engine is
    shared by API + MCP + workers, but each PROCESS imports this module independently,
    so the pool is per-process. There is no pgbouncer in front (direct Postgres), so
    the app owns pooling.

    Env-gated:
    - DB_POOL_CLASS=null  → instant rollback to NullPool (no pooling)
    - DB_POOL_SIZE (5), DB_MAX_OVERFLOW (10), DB_POOL_RECYCLE_S (1800),
      DB_POOL_TIMEOUT_S (10)

    NOTE: never pass the sync ``QueuePool`` class to an async engine — SQLAlchemy
    raises ArgumentError. Omitting ``poolclass`` selects AsyncAdaptedQueuePool.
    """
    url = database_url if database_url is not None else settings.DATABASE_URL
    pool_class = os.getenv("DB_POOL_CLASS", "queue").strip().lower()
    if pool_class == "null":
        return create_async_engine(
            url, echo=_echo, pool_pre_ping=True, poolclass=NullPool
        )
    return create_async_engine(
        url,
        echo=_echo,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE_S", "1800")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT_S", "10")),
    )


# ROB-964: defer engine + sessionmaker construction to first use.
#
# ``create_async_engine('postgresql+asyncpg://...')`` eagerly imports the
# ``asyncpg`` DBAPI (~0.8s cold, amplified by coverage's sys.monitoring under
# xdist). This module is imported by ~130 call sites, so building the engine at
# import time charged that asyncpg import to every worker's first test — even
# pure-mock tests that never touch the DB. Constructing the engine lazily keeps
# the singleton semantics (one engine per process) while moving the asyncpg
# import to the first real ``AsyncSessionLocal()`` / ``get_db()`` / ``engine``
# access.
_engine: AsyncEngine | None = None
# ``sessionmaker`` is left unparameterized: its stub TypeVar is bound to the sync
# ``Session`` and rejects ``AsyncSession`` as a type argument, mirroring the
# original untyped ``AsyncSessionLocal = sessionmaker(...)`` binding.
_session_local: Any = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def _get_session_local() -> Any:
    global _session_local
    if _session_local is None:
        _session_local = sessionmaker(
            _get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_local


class _LazyAsyncSessionLocal:
    """Stable-singleton proxy for the async ``sessionmaker``.

    Callers do ``AsyncSessionLocal()`` (and pass the object around as a session
    factory / compare it by identity), so a single proxy instance preserves
    every existing usage while deferring engine creation — and the ``asyncpg``
    import — until the first session is opened. ROB-964.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> AsyncSession:
        return _get_session_local()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Guard against dunder probes (copy/pickle/etc.) forcing eager engine
        # creation; only real ``sessionmaker`` attributes are delegated.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(_get_session_local(), name)


AsyncSessionLocal = _LazyAsyncSessionLocal()


def __getattr__(name: str) -> Any:
    """Module-level lazy attribute access for the shared engine.

    ``from app.core.db import engine`` (or ``app.core.db.engine``) resolves here
    only when ``engine`` is not a real module attribute, so it builds the engine
    on first access instead of at import. ROB-964.
    """
    if name == "engine":
        return _get_engine()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
