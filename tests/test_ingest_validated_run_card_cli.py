"""ROB-332 — operator CLI for validated_run_card ingest."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ingest_validated_run_card as cli

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "validated_run_card"
    / "run_card_insufficient_data.json"
)


def _load() -> dict:
    with _FIXTURE.open() as fh:
        return json.load(fh)  # default json.loads accepts bare Infinity tokens


def _settings_free_env() -> dict[str, str]:
    """Env that cannot satisfy pydantic Settings: only neutral keys kept, no
    KIS/Upbit/DATABASE_URL/SECRET_KEY. Combined with running from a cwd that has
    no ``.env`` file, Settings would have no source — so the CLI must NOT load
    Settings for the --help / dry-run / file-parse paths."""
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SystemRoot"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def test_parse_args_requires_file_and_market():
    ns = cli.parse_args(["--file", "x.json", "--market", "crypto"])
    assert ns.file == Path("x.json")
    assert ns.market == "crypto"
    assert ns.account_scope is None
    assert ns.commit is False
    assert ns.confirm is False


def test_parse_args_rejects_unknown_market():
    with pytest.raises(SystemExit):
        cli.parse_args(["--file", "x.json", "--market", "forex"])


def test_parse_args_rejects_unknown_account_scope():
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--file", "x.json", "--market", "us", "--account-scope", "binance_demo"]
        )


@pytest.mark.asyncio
async def test_run_ingest_dry_run_returns_headline_no_db(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=False,
        confirm=False,
    )
    assert code == 0
    assert summary["dry_run"] is True
    assert summary["verdict"] == "insufficient_data"
    assert summary["is_pass_stamp"] is False
    assert summary["trade_count"] == 2
    assert summary["symbols"] == ["XRPUSDT"]
    assert "snapshot_uuid" not in summary
    # strict-JSON safe (no Infinity/NaN leaks into output)
    json.dumps(summary, allow_nan=False)


@pytest.mark.asyncio
async def test_run_ingest_commit_persists_sanitized_snapshot(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=True,
    )
    assert code == 0
    assert summary["dry_run"] is False
    uuid_str = summary["snapshot_uuid"]
    assert uuid_str

    import uuid as _uuid

    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    repo = InvestmentSnapshotsRepository(db_session)
    snap = await repo.get_snapshot_by_uuid(_uuid.UUID(uuid_str))
    assert snap is not None
    assert snap.snapshot_kind == "validated_run_card"
    assert snap.source_kind == "manual"  # never the local file path
    # Non-finite metric sanitized to null -> strict-JSON safe payload.
    assert snap.payload_json["net_after_cost"]["profit_factor"] is None
    json.dumps(snap.payload_json, allow_nan=False)


@pytest.mark.asyncio
async def test_run_ingest_is_idempotent_reuses_snapshot(db_session):
    payload = _load()
    _c1, s1 = await cli.run_ingest(
        db=db_session,
        raw_payload=payload,
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=True,
    )
    _c2, s2 = await cli.run_ingest(
        db=db_session,
        raw_payload=payload,
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=True,
    )
    # Same canonical payload dedups to the same snapshot row.
    assert s1["snapshot_uuid"] == s2["snapshot_uuid"]


@pytest.mark.asyncio
async def test_run_ingest_commit_without_confirm_is_gated(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=False,
    )
    assert code == 4
    assert "snapshot_uuid" not in summary


# --- Settings-free entry paths (ROB-332 follow-up) -------------------------
# --help / dry-run / file-parse must work with no DB/broker secrets. Run from
# tmp_path (no .env) with a settings-free env so Settings has no source; the
# CLI must reach these paths without ever instantiating Settings.


def test_help_runs_without_settings_secrets(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.ingest_validated_run_card", "--help"],
        cwd=tmp_path,
        env=_settings_free_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()
    assert "ValidationError" not in proc.stderr


def test_dry_run_runs_without_settings_secrets(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ingest_validated_run_card",
            "--file",
            str(_FIXTURE),
            "--market",
            "crypto",
        ],
        cwd=tmp_path,
        env=_settings_free_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ValidationError" not in proc.stderr
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["verdict"] == "insufficient_data"
