import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings

# MCP 서버에서는 stdout 오염 방지를 위해 echo=False 필수
_echo = os.getenv("SQLALCHEMY_ECHO", "false").lower() in ("true", "1", "yes")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=_echo,
    pool_pre_ping=True,
    poolclass=NullPool,
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
