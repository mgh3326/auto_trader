import os
from collections.abc import AsyncGenerator

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


engine = build_engine()
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
