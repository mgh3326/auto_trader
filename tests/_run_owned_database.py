"""Fail-closed lifecycle helpers for pytest-owned PostgreSQL databases."""

from __future__ import annotations

import os
import re
import secrets
from typing import Any

from sqlalchemy.engine import URL, make_url

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"
)
DATABASE_NAME_ENV = "AUTO_TRADER_XDIST_DATABASE_NAME"
BASE_DATABASE_URL_ENV = "AUTO_TRADER_XDIST_BASE_DATABASE_URL"
RUN_UID_ENV = "AUTO_TRADER_PYTEST_RUN_UID"
OWNER_TOKEN_ENV = "AUTO_TRADER_PYTEST_OWNER_TOKEN"
SHARED_DATABASE_ENV = "AUTO_TRADER_PYTEST_USE_SHARED_DB"
TEST_DATABASE_URL_ENV = "AUTO_TRADER_TEST_DATABASE_URL"

_RUN_UID_PATTERN = re.compile(r"[0-9a-f]{12}")
_OWNER_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
_WORKER_PATTERN = re.compile(r"(?:main|gw[0-9]+)")
_OWNED_DATABASE_PATTERN = re.compile(
    r"test_db_pytest_(?P<run_uid>[0-9a-f]{12})_(?P<worker>main|gw[0-9]+)"
)
_OWNER_TABLE = "public._pytest_database_owner"


def _shared_database_requested() -> bool:
    raw = os.environ.get(SHARED_DATABASE_ENV, "0").strip()
    if raw in {"", "0"}:
        return False
    if raw == "1":
        return True
    raise RuntimeError(f"{SHARED_DATABASE_ENV} must be exactly 0 or 1")


def _validated_base_url() -> URL:
    raw = os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL)
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError(f"{TEST_DATABASE_URL_ENV} must use PostgreSQL")
    if url.database != "test_db":
        raise RuntimeError(
            f"{TEST_DATABASE_URL_ENV} must target the dedicated test_db database"
        )
    if not url.username or not url.host:
        raise RuntimeError(f"{TEST_DATABASE_URL_ENV} must include username and host")
    return url


def _validated_run_uid() -> str:
    run_uid = os.environ.get(RUN_UID_ENV, "")
    if not _RUN_UID_PATTERN.fullmatch(run_uid):
        raise RuntimeError("refusing invalid pytest run UID")
    return run_uid


def _validated_owner_token() -> str:
    owner_token = os.environ.get(OWNER_TOKEN_ENV, "")
    if not _OWNER_TOKEN_PATTERN.fullmatch(owner_token):
        raise RuntimeError("refusing invalid pytest database owner token")
    return owner_token


def validate_owned_database_name(database_name: str) -> re.Match[str]:
    match = _OWNED_DATABASE_PATTERN.fullmatch(database_name)
    if match is None:
        raise RuntimeError("refusing unsafe or unowned pytest database name")
    if match.group("run_uid") != _validated_run_uid():
        raise RuntimeError("refusing pytest database from a different run")
    return match


def validate_run_owned_database_url(database_url: str | URL) -> URL:
    """Validate a PostgreSQL URL before a test opens any database connection.

    This guard intentionally lives outside ``tests/conftest.py``.  A test
    invoked with ``--noconftest`` must fail closed before a module-local
    session/engine can connect to an operator-provided URL.
    """

    url = make_url(database_url) if isinstance(database_url, str) else database_url
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("refusing a non-PostgreSQL test database URL")
    if not url.database or not url.host or not url.username:
        raise RuntimeError("refusing an incomplete test database URL")

    if _shared_database_requested():
        # The documented shared mode is a narrowly scoped exception to the
        # run-owned name pattern.  The opt-in alone is not authority: the
        # URL must still target the dedicated shared ``test_db`` database and
        # the same PostgreSQL server identity as the validated test base URL.
        if url.database != "test_db":
            raise RuntimeError("refusing unsafe or unowned pytest database name")
        base_raw = os.environ.get(BASE_DATABASE_URL_ENV)
        base_url = make_url(base_raw) if base_raw else _validated_base_url()
        if (
            base_url.get_backend_name() != "postgresql"
            or base_url.database != "test_db"
            or not base_url.host
            or not base_url.username
        ):
            raise RuntimeError("refusing an invalid shared pytest base URL")
        if (url.host, url.port, url.username) != (
            base_url.host,
            base_url.port,
            base_url.username,
        ):
            raise RuntimeError("refusing a test URL on an unowned PostgreSQL server")
        return url

    validate_owned_database_name(url.database)
    configured_name = os.environ.get(DATABASE_NAME_ENV)
    if configured_name != url.database:
        raise RuntimeError("refusing a URL not owned by this pytest run")

    base_raw = os.environ.get(BASE_DATABASE_URL_ENV)
    if not base_raw:
        raise RuntimeError("refusing a test URL without an owned base URL")
    base_url = make_url(base_raw)
    if base_url.get_backend_name() != "postgresql":
        raise RuntimeError("refusing a non-PostgreSQL pytest base URL")
    if (url.host, url.port, url.username) != (
        base_url.host,
        base_url.port,
        base_url.username,
    ):
        raise RuntimeError("refusing a test URL on an unowned PostgreSQL server")
    return url


def configure_test_database_environment() -> None:
    """Select a run-owned DB name without connecting to PostgreSQL.

    The controller/serial process creates a fresh run UID and ownership token.
    Xdist workers inherit those values and append their validated worker ID.
    Pure unit and collect-only runs stop here and therefore remain DB-free.
    """

    base_url = _validated_base_url()
    rendered_base_url = base_url.render_as_string(hide_password=False)
    os.environ[BASE_DATABASE_URL_ENV] = rendered_base_url

    if _shared_database_requested():
        os.environ.pop(DATABASE_NAME_ENV, None)
        os.environ["DATABASE_URL"] = rendered_base_url
        return

    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id and worker_id != "master":
        if not re.fullmatch(r"gw[0-9]+", worker_id):
            raise RuntimeError("refusing invalid xdist worker ID")
        run_uid = _validated_run_uid()
        _validated_owner_token()
        worker = worker_id
    else:
        run_uid = secrets.token_hex(6)
        os.environ[RUN_UID_ENV] = run_uid
        os.environ[OWNER_TOKEN_ENV] = secrets.token_hex(16)
        worker = "main"

    database_name = f"test_db_pytest_{run_uid}_{worker}"
    validate_owned_database_name(database_name)
    os.environ[DATABASE_NAME_ENV] = database_name
    os.environ["DATABASE_URL"] = base_url.set(database=database_name).render_as_string(
        hide_password=False
    )


def uses_run_owned_database() -> bool:
    database_name = os.environ.get(DATABASE_NAME_ENV)
    if not database_name:
        return False
    validate_owned_database_name(database_name)
    return True


def uses_shared_test_database() -> bool:
    return not uses_run_owned_database()


def _connection_kwargs(url: URL, *, database: str) -> dict[str, Any]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host,
        "port": url.port,
        "database": database,
        "timeout": 10,
    }


async def ensure_run_owned_database() -> bool:
    """Create and mark this process's exact run-owned database.

    Existing databases are never adopted, even when their names match the
    pytest pattern. This prevents a stale or user-created database from being
    mistaken for the current run's property.
    """

    database_name = os.environ.get(DATABASE_NAME_ENV)
    if not database_name:
        return False
    validate_owned_database_name(database_name)
    owner_token = _validated_owner_token()

    import asyncpg

    base_url = make_url(os.environ[BASE_DATABASE_URL_ENV])
    admin = await asyncpg.connect(**_connection_kwargs(base_url, database="postgres"))
    created = False
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database_name
        )
        if exists:
            raise RuntimeError("refusing to reuse an existing pytest database")
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        created = True
    finally:
        await admin.close()

    try:
        target = await asyncpg.connect(
            **_connection_kwargs(base_url, database=database_name)
        )
        try:
            await target.execute(
                f"CREATE TABLE {_OWNER_TABLE} ("
                "database_name TEXT PRIMARY KEY, "
                "owner_token TEXT NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            await target.execute(
                f"INSERT INTO {_OWNER_TABLE} (database_name, owner_token) VALUES ($1, $2)",
                database_name,
                owner_token,
            )
        finally:
            await target.close()
    except BaseException:
        # The name was absent immediately before this process created it, so
        # exact cleanup is safe even if owner-marker initialization failed.
        if created:
            admin = await asyncpg.connect(
                **_connection_kwargs(base_url, database="postgres")
            )
            try:
                await admin.execute(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
            finally:
                await admin.close()
        raise
    return True


async def drop_run_owned_database() -> bool:
    """Drop only a database whose exact ownership marker matches this run."""

    database_name = os.environ.get(DATABASE_NAME_ENV)
    if not database_name:
        return False
    validate_owned_database_name(database_name)
    owner_token = _validated_owner_token()

    import asyncpg

    base_url = make_url(os.environ[BASE_DATABASE_URL_ENV])
    admin = await asyncpg.connect(**_connection_kwargs(base_url, database="postgres"))
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database_name
        )
    finally:
        await admin.close()
    if not exists:
        return False

    target = await asyncpg.connect(
        **_connection_kwargs(base_url, database=database_name)
    )
    try:
        marker = await target.fetchrow(
            f"SELECT database_name, owner_token FROM {_OWNER_TABLE} "
            "WHERE database_name = $1",
            database_name,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise RuntimeError(
            "refusing to drop pytest database without an ownership marker"
        ) from exc
    finally:
        await target.close()

    if marker is None or marker["owner_token"] != owner_token:
        raise RuntimeError("refusing to drop pytest database owned by another run")

    admin = await asyncpg.connect(**_connection_kwargs(base_url, database="postgres"))
    try:
        await admin.execute(f'DROP DATABASE "{database_name}" WITH (FORCE)')
    finally:
        await admin.close()
    return True
