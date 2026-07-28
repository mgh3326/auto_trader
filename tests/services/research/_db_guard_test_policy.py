"""Exact research-write policy for the pytest-owned PostgreSQL target.

This helper is test-only. It does not change the production guard or teach it
to accept a prefix: it builds a one-target policy from the DATABASE_URL that
tests/conftest.py has already forced to localhost and, under xdist, to the
exact run-owned worker database.
"""

from __future__ import annotations

import os
import re

from sqlalchemy.engine import make_url

from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget

_BASE_TEST_DATABASE_NAME = "test_db"
_LOCAL_TEST_DATABASE_HOST = "localhost"
_XDIST_DATABASE_NAME_ENV = "AUTO_TRADER_XDIST_DATABASE_NAME"
_XDIST_DATABASE_PATTERN = re.compile(r"test_db_[A-Za-z0-9_]+")


def current_research_test_db_policy() -> ResearchDbPolicy:
    """Authorize exactly the local pytest database selected by conftest."""
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        raise RuntimeError("pytest research DB policy requires DATABASE_URL")

    url = make_url(raw_url)
    host = (url.host or "").strip().lower()
    database_name = (url.database or "").strip()
    worker_database_name = os.environ.get(_XDIST_DATABASE_NAME_ENV)
    expected_database_name = worker_database_name or _BASE_TEST_DATABASE_NAME

    if host != _LOCAL_TEST_DATABASE_HOST:
        raise RuntimeError("pytest research DB policy requires the localhost target")
    if database_name != expected_database_name:
        raise RuntimeError("pytest research DB policy target does not match conftest")
    if worker_database_name and not _XDIST_DATABASE_PATTERN.fullmatch(
        worker_database_name
    ):
        raise RuntimeError(
            "pytest research DB policy rejected the worker database name"
        )

    return ResearchDbPolicy.of(ResearchDbTarget(host=host, database_name=database_name))
