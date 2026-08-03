"""Adversarial tests for the dry-run scratch-database guard.

These are deliberately written as attempts to *reach* an operational database.
A guard that has only ever been exercised on the happy path is an assertion,
not evidence.

The guard interrogates the live connection, so the stub here returns whatever a
hostile/misconfigured server would report — that is the input that matters, not
the DSN string.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "kr_backfill_dryrun", REPO / "research/kr_backfill/dryrun_upsert.py"
)
dryrun = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dryrun)

RUN_ID = "a" * 32
GOOD_DB = f"{dryrun.SCRATCH_PREFIX}deadbeefcafe"


class StubConn:
    """Reports whatever the 'server' claims, so the guard can be attacked."""

    def __init__(self, database: str, marker_run_id: str | None = RUN_ID):
        self._database = database
        self._marker_run_id = marker_run_id

    async def fetchval(self, sql: str, *args):
        if "current_database()" in sql:
            return self._database
        if "to_regclass" in sql:
            return self._marker_run_id is not None
        if "run_id" in sql:
            return self._marker_run_id
        raise AssertionError(f"unexpected query: {sql}")


async def _guard(conn, expected=GOOD_DB, run_id=RUN_ID):
    return await dryrun.assert_scratch_target(conn, expected, run_id)


@pytest.mark.asyncio
async def test_happy_path_passes():
    result = await _guard(StubConn(GOOD_DB))
    assert result["current_database"] == GOOD_DB


# --- attempts to reach the operational database ------------------------


@pytest.mark.asyncio
async def test_operational_database_is_refused():
    """The exact database this incident touched."""
    with pytest.raises(dryrun.ScratchGuardViolation, match="deny-listed"):
        await _guard(StubConn("auto_trader"), expected="auto_trader")


@pytest.mark.asyncio
async def test_operational_database_refused_even_with_a_forged_marker():
    """Planting the marker row must not buy access to a deny-listed database."""
    with pytest.raises(dryrun.ScratchGuardViolation, match="deny-listed"):
        await _guard(
            StubConn("auto_trader", marker_run_id=RUN_ID), expected="auto_trader"
        )


@pytest.mark.asyncio
async def test_operational_database_refused_even_if_renamed_to_look_like_scratch():
    """A deny-listed name wins over the prefix rule; order of checks matters."""
    with pytest.raises(dryrun.ScratchGuardViolation):
        await _guard(StubConn("postgres"), expected="postgres")


@pytest.mark.asyncio
async def test_arbitrary_database_without_prefix_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="prefix"):
        await _guard(StubConn("test_db"), expected="test_db")


@pytest.mark.asyncio
async def test_a_different_scratch_database_is_refused():
    """Right prefix, wrong database — e.g. a leftover from another run."""
    other = f"{dryrun.SCRATCH_PREFIX}000000000000"
    with pytest.raises(dryrun.ScratchGuardViolation, match="this run created"):
        await _guard(StubConn(other))


@pytest.mark.asyncio
async def test_missing_marker_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="marker"):
        await _guard(StubConn(GOOD_DB, marker_run_id=None))


@pytest.mark.asyncio
async def test_marker_from_another_run_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="does not match this run"):
        await _guard(StubConn(GOOD_DB, marker_run_id="b" * 32))


# --- structural: the caller cannot choose the target -------------------


def test_admin_connection_is_forced_to_the_maintenance_database():
    """Even an admin URL aimed at the operational database is redirected."""
    hostile = "postgresql://u:p@localhost:5432/auto_trader"
    assert dryrun._admin_dsn(hostile).endswith(f"/{dryrun.ADMIN_DATABASE}")
    assert "auto_trader" not in dryrun._admin_dsn(hostile)


def test_admin_dsn_normalises_async_driver_prefix():
    hostile = "postgresql+asyncpg://u:p@localhost:5432/auto_trader"
    assert dryrun._admin_dsn(hostile).startswith("postgresql://")
    assert "auto_trader" not in dryrun._admin_dsn(hostile)


def test_operational_database_is_deny_listed_by_name():
    assert "auto_trader" in dryrun.DENY_DATABASES


def test_upsert_statement_targets_research_schema():
    assert "research.kr_candles_1m" in dryrun.BACKFILL_UPSERT_SQL
    assert "public." not in dryrun.BACKFILL_UPSERT_SQL
