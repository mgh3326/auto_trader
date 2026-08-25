"""Unit contracts for fail-closed pytest database ownership."""

from __future__ import annotations

import re

import pytest

from tests import _run_owned_database as owned_db

pytestmark = pytest.mark.unit


def _clear_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        owned_db.DATABASE_NAME_ENV,
        owned_db.BASE_DATABASE_URL_ENV,
        owned_db.RUN_UID_ENV,
        owned_db.OWNER_TOKEN_ENV,
        owned_db.SHARED_DATABASE_ENV,
        owned_db.TEST_DATABASE_URL_ENV,
        "DATABASE_URL",
        "PYTEST_XDIST_WORKER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_serial_configuration_uses_fresh_owned_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)

    owned_db.configure_test_database_environment()

    database_name = owned_db.os.environ[owned_db.DATABASE_NAME_ENV]
    assert re.fullmatch(r"test_db_pytest_[0-9a-f]{12}_main", database_name)
    assert owned_db.os.environ["DATABASE_URL"].endswith(f"/{database_name}")
    assert owned_db.uses_run_owned_database() is True


def test_xdist_worker_reuses_run_identity_but_gets_worker_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.RUN_UID_ENV, "a1b2c3d4e5f6")
    monkeypatch.setenv(owned_db.OWNER_TOKEN_ENV, "7" * 32)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    owned_db.configure_test_database_environment()

    assert (
        owned_db.os.environ[owned_db.DATABASE_NAME_ENV]
        == "test_db_pytest_a1b2c3d4e5f6_gw3"
    )


def test_shared_database_requires_exact_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.SHARED_DATABASE_ENV, "1")

    owned_db.configure_test_database_environment()

    assert owned_db.DATABASE_NAME_ENV not in owned_db.os.environ
    assert owned_db.os.environ["DATABASE_URL"].endswith("/test_db")
    assert owned_db.uses_shared_test_database() is True
    assert (
        owned_db.validate_run_owned_database_url(
            owned_db.os.environ["DATABASE_URL"]
        ).database
        == "test_db"
    )
    with pytest.raises(RuntimeError, match="unsafe or unowned"):
        owned_db.validate_run_owned_database_url(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/production"
        )

    _clear_database_env(monkeypatch)
    monkeypatch.setenv(
        owned_db.TEST_DATABASE_URL_ENV,
        "postgresql+asyncpg://postgres:postgres@localhost:5432/auto_trader",
    )
    with pytest.raises(RuntimeError, match="dedicated test_db"):
        owned_db.configure_test_database_environment()


def test_shared_database_rejects_same_name_on_a_different_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.SHARED_DATABASE_ENV, "1")
    monkeypatch.setenv(
        owned_db.TEST_DATABASE_URL_ENV,
        "postgresql+asyncpg://test_user:test_pass@test-db.example:5432/test_db",
    )

    owned_db.configure_test_database_environment()

    with pytest.raises(RuntimeError, match="unowned PostgreSQL server"):
        owned_db.validate_run_owned_database_url(
            "postgresql+asyncpg://test_user:test_pass@other-db.example:5432/test_db"
        )


@pytest.mark.parametrize("value", ["true", "yes", "2", "-1"])
def test_shared_database_rejects_ambiguous_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.SHARED_DATABASE_ENV, value)

    with pytest.raises(RuntimeError, match="must be exactly 0 or 1"):
        owned_db.configure_test_database_environment()


def test_drop_name_must_belong_to_current_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.RUN_UID_ENV, "a1b2c3d4e5f6")

    with pytest.raises(RuntimeError, match="different run"):
        owned_db.validate_owned_database_name("test_db_pytest_000000000000_main")
    with pytest.raises(RuntimeError, match="unsafe or unowned"):
        owned_db.validate_owned_database_name("production")


def test_database_url_guard_accepts_the_current_run_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.RUN_UID_ENV, "a1b2c3d4e5f6")
    monkeypatch.setenv(owned_db.OWNER_TOKEN_ENV, "7" * 32)
    owned_db.configure_test_database_environment()

    url = owned_db.validate_run_owned_database_url(owned_db.os.environ["DATABASE_URL"])
    assert url.database == owned_db.os.environ[owned_db.DATABASE_NAME_ENV]


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://prod_user:secret@prod-db.example/production",
        "postgresql+asyncpg://prod_user:secret@prod-db.example/test_db_pytest_a1b2c3d4e5f6_main",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/production",
    ],
)
def test_database_url_guard_rejects_production_like_urls(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(owned_db.RUN_UID_ENV, "a1b2c3d4e5f6")
    monkeypatch.setenv(owned_db.OWNER_TOKEN_ENV, "7" * 32)
    owned_db.configure_test_database_environment()

    with pytest.raises(RuntimeError):
        owned_db.validate_run_owned_database_url(database_url)
