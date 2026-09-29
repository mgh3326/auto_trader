# alembic/env.py
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 환경 변수/URL 로딩 (당신의 Settings로 대체)
from app.core.config import settings

# ---- 앱 메타데이터 임포트 (autogenerate 위해 꼭 필요)
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """--offline 모드: 커넥션 없이 URL로 실행"""
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """실제 마이그레이션 실행 (sync 함수로 정의하고 run_sync로 감쌈)"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """--online 모드: async 엔진/커넥션 사용"""
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.DATABASE_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    migration_role = os.environ.get("AT_MIGRATION_SET_ROLE")
    if migration_role is not None and migration_role != "at_migration_owner":
        raise RuntimeError("AT_MIGRATION_SET_ROLE must be at_migration_owner")

    async with connectable.connect() as connection:
        if migration_role:
            # The runner is NOINHERIT. SET ROLE must precede all Alembic DDL so
            # new objects receive the owner's default ACLs and ownership.
            await connection.execute(text("SET ROLE at_migration_owner"))
            identity = await connection.execute(
                text("SELECT session_user, current_user")
            )
            session_user, current_user = identity.one()
            if session_user != "at_migration_runner" or current_user != migration_role:
                raise RuntimeError("migration role identity mismatch")
            await connection.commit()
        else:
            identity = await connection.execute(text("SELECT session_user"))
            if identity.scalar_one() == "at_migration_runner":
                raise RuntimeError("migration runner requires AT_MIGRATION_SET_ROLE")
            await connection.commit()
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
