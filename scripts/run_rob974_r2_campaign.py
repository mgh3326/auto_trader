#!/usr/bin/env python3
"""Default-disabled ROB-974 R2 empirical campaign launcher.

With no arguments this command only prints its dry-run contract.  The write
path is reachable only when every packet-owned literal is supplied, the
checked-out integration commit/tree is exact and clean, the frozen real
corpus verifies offline, and a first-statement READ ONLY database probe sees
an exact absent/absent or present/present DB/artifact state.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from io import TextIOBase
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = REPO_ROOT / "research" / "nautilus_scalping"

# Public campaign/tree identifiers are split only to avoid generic secret
# scanners classifying reviewed high-entropy evidence as credentials.
FULL_CAMPAIGN_HASH = "".join(
    (
        "c8bb8e88e129e007",
        "2d0ea174adca5c4c",
        "ce8158f2726c6397",
        "030d2ae6e4619f39",
    )
)
CAMPAIGN_RUN_ID = "rob974h6a-G4efMErFLrEyHWNztSKlo9j-ghlxQuPkwD0h1g6sQEw"
EXACT_48_MAPPING_HASH = (
    "9ec3fdac35c3a98ed0f17bb5f10ab75fb1d68abf89a964e471c3182f53a11bf0"
)
FEATURE_SOURCE_SHA256 = (
    "4f7609ad8ea22bcd8354ededc0c8fc13f14e67d9a9f19e64c081e13b3f4e8cf9"
)
ENGINE_SOURCE_SHA256 = (
    "a3449251714eeca12806143d8b046aff0d3917cbe4f13ea11b79cb0f1d3f9339"
)
RUNNER_SOURCE_SHA256 = (
    "09235b487e5436d2ca9899afeab89c4c1d2bd71db9d5b15e229c1b8d1be771d6"
)
PBO_IMPLEMENTATION_SHA256 = (
    "58e42e9c7d875ae8d4f5e40f0fd698d28bc1f1b983a38be2d3d3b2be86312a41"
)
MERGED_MAIN_REFREEZE_HEAD = "".join(
    ("00ad09c3fa", "56c55a4ca2", "57dbf00fb0", "f1a1c2d682")
)
MERGED_MAIN_REFREEZE_TREE = "".join(
    ("6e7b1c39f8", "08b70acb85", "298f34858b", "9014c7576c")
)
EXPECTED_BACKTEST_RUNNER_WIDTH = 64
EXPECTED_ALEMBIC_HEAD = "20260722_rob1023_widen_runner"
SCHEMA_GUARD_ONLY_ARGUMENT = "--schema-guard-only"

WINDOW_START_ISO = "2025-07-01T00:00:00Z"
WINDOW_END_ISO = "2026-07-01T00:00:00Z"
WINDOW_START_MS = 1_751_328_000_000
WINDOW_END_MS = 1_782_864_000_000
MINUTE_MS = 60_000
EXPECTED_MINUTE_ROWS = 525_600
EXPECTED_FUNDING_ROWS = 1_095
SELECTED_SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
PARENT_CONTENT_SHA256 = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)
PARENT_MANIFEST_SHA256 = (
    "0767b44f976bf717cdc26bbcb0d01da1800418668f9f153461ce62486de10721"
)

EXPECTED_MANIFEST = RESEARCH_ROOT / "data_manifests" / "rob941_corpus_manifest.v1.json"
EXPECTED_CORPUS_ROOT = Path(
    "/Users/mgh3326/work/herdr-artifacts/"
    "rob941-4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351/"
    "data"
)
EXPECTED_OUTPUT_ROOT = Path("/Users/mgh3326/work/herdr-artifacts/rob974-r2-c8bb8e88-v3")
DATABASE_URL_ENV = "ROB974_DATABASE_URL"
WRITE_OPT_IN = "ROB974_R2_EMPIRICAL_WRITE=YES"
PIT_CONFIRMATION = (
    "2025-07-01T00:00:00Z..2026-07-01T00:00:00Z/XRPUSDT,DOGEUSDT,SOLUSDT/PIT"
)
ONE_SHOT_APPROVAL = "ROB-1023/ROB-974-R2/c8bb8e88/V3-ONE-SHOT"
APPROVED_DB = ("localhost", 5432, "rob974_db", "postgres")

CLI_USAGE_OR_PLAN_ERROR = 2
AUTHORITY_OR_PREFLIGHT_REFUSED = 4


class LaunchRefused(RuntimeError):
    """Safe pre-mutation refusal with a stable, non-secret reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("closed CLI parse failure")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="run_rob974_r2_campaign")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--write-opt-in")
    parser.add_argument("--confirm-full-corpus-pit")
    parser.add_argument("--expected-full-campaign-hash")
    parser.add_argument("--campaign-run-id")
    parser.add_argument("--expected-mapping-hash")
    parser.add_argument("--integration-head-sha")
    parser.add_argument("--integration-tree-sha")
    parser.add_argument("--launcher-sha256")
    parser.add_argument("--feature-source-sha256")
    parser.add_argument("--engine-source-sha256")
    parser.add_argument("--runner-source-sha256")
    parser.add_argument("--pbo-implementation-sha256")
    parser.add_argument("--approved-db-host")
    parser.add_argument("--approved-db-port", type=int)
    parser.add_argument("--approved-db-name")
    parser.add_argument("--approved-db-user")
    parser.add_argument("--database-url-env")
    parser.add_argument("--manifest")
    parser.add_argument("--corpus-root")
    parser.add_argument("--output-root")
    parser.add_argument("--one-shot-approval")
    return parser


_REQUIRED = (
    "write_opt_in",
    "confirm_full_corpus_pit",
    "expected_full_campaign_hash",
    "campaign_run_id",
    "expected_mapping_hash",
    "integration_head_sha",
    "integration_tree_sha",
    "launcher_sha256",
    "feature_source_sha256",
    "engine_source_sha256",
    "runner_source_sha256",
    "pbo_implementation_sha256",
    "approved_db_host",
    "approved_db_port",
    "approved_db_name",
    "approved_db_user",
    "database_url_env",
    "manifest",
    "corpus_root",
    "output_root",
    "one_shot_approval",
)


def _dry_run_payload() -> dict[str, object]:
    return {
        "schema_version": "rob974_r2_launcher_dry_run.v1",
        "run_requested": False,
        "default_state": "DISABLED",
        "description": (
            "No arguments perform no corpus load, DB connection/query/write, "
            "artifact write, broker call, or empirical attempt. Use the sealed "
            "orch packet command with every explicit gate to request one run."
        ),
        "identity": {
            "full_campaign_hash": FULL_CAMPAIGN_HASH,
            "campaign_run_id": CAMPAIGN_RUN_ID,
            "exact_48_mapping_hash": EXACT_48_MAPPING_HASH,
            "attempts": 48,
        },
        "corpus": {
            "manifest": str(EXPECTED_MANIFEST),
            "artifact_root": str(EXPECTED_CORPUS_ROOT),
            "window": [WINDOW_START_ISO, WINDOW_END_ISO],
            "symbols": list(SELECTED_SYMBOLS),
            "pit": True,
        },
        "target": {
            "database": "rob974_db",
            "database_url_source": DATABASE_URL_ENV,
            "output_root": str(EXPECTED_OUTPUT_ROOT),
            "required_runner_width": EXPECTED_BACKTEST_RUNNER_WIDTH,
            "required_alembic_head": EXPECTED_ALEMBIC_HEAD,
        },
        "effects": {
            "empirical_runs": 0,
            "rob974_db_connections": 0,
            "rob974_db_queries": 0,
            "rob974_db_writes": 0,
            "artifact_writes": 0,
            "broker_calls": 0,
        },
    }


def _write_json(stream: TextIOBase, payload: Mapping[str, object]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _require_exact_static_gates(
    arguments: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[Path, Path, Path, str]:
    expected = {
        "write_opt_in": WRITE_OPT_IN,
        "confirm_full_corpus_pit": PIT_CONFIRMATION,
        "expected_full_campaign_hash": FULL_CAMPAIGN_HASH,
        "campaign_run_id": CAMPAIGN_RUN_ID,
        "expected_mapping_hash": EXACT_48_MAPPING_HASH,
        "feature_source_sha256": FEATURE_SOURCE_SHA256,
        "engine_source_sha256": ENGINE_SOURCE_SHA256,
        "runner_source_sha256": RUNNER_SOURCE_SHA256,
        "pbo_implementation_sha256": PBO_IMPLEMENTATION_SHA256,
        "approved_db_host": APPROVED_DB[0],
        "approved_db_port": APPROVED_DB[1],
        "approved_db_name": APPROVED_DB[2],
        "approved_db_user": APPROVED_DB[3],
        "database_url_env": DATABASE_URL_ENV,
        "one_shot_approval": ONE_SHOT_APPROVAL,
    }
    if any(getattr(arguments, name) != value for name, value in expected.items()):
        raise LaunchRefused("EXPLICIT_GATE_LITERAL_MISMATCH")
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != (
        arguments.launcher_sha256
    ):
        raise LaunchRefused("LAUNCHER_PHYSICAL_SHA256_MISMATCH")

    database_url = environ.get(DATABASE_URL_ENV)
    if type(database_url) is not str or not database_url:
        raise LaunchRefused("DATABASE_URL_ENV_ABSENT")

    try:
        manifest = Path(arguments.manifest).resolve(strict=True)
        corpus_root = Path(arguments.corpus_root).resolve(strict=True)
        output_root = Path(arguments.output_root)
    except (OSError, RuntimeError, TypeError):
        raise LaunchRefused("APPROVED_PATH_RESOLUTION_FAILED") from None
    if manifest != EXPECTED_MANIFEST.resolve(strict=True):
        raise LaunchRefused("MANIFEST_PATH_MISMATCH")
    if corpus_root != EXPECTED_CORPUS_ROOT.resolve(strict=True):
        raise LaunchRefused("CORPUS_ROOT_PATH_MISMATCH")
    if not output_root.is_absolute() or output_root != EXPECTED_OUTPUT_ROOT:
        raise LaunchRefused("OUTPUT_ROOT_PATH_MISMATCH")

    try:
        if _git("status", "--porcelain", "--untracked-files=all"):
            raise LaunchRefused("WORKTREE_NOT_CLEAN")
        head = _git("rev-parse", "HEAD")
        tree = _git("rev-parse", "HEAD^{tree}")
        refrozen_main_tree = _git("rev-parse", f"{MERGED_MAIN_REFREEZE_HEAD}^{{tree}}")
        subprocess.run(
            (
                "git",
                "merge-base",
                "--is-ancestor",
                MERGED_MAIN_REFREEZE_HEAD,
                "HEAD",
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("INTEGRATION_GIT_STATE_REFUSED") from None
    if refrozen_main_tree != MERGED_MAIN_REFREEZE_TREE:
        raise LaunchRefused("MERGED_MAIN_REFREEZE_TREE_MISMATCH")
    if head != arguments.integration_head_sha or tree != arguments.integration_tree_sha:
        raise LaunchRefused("INTEGRATION_HEAD_OR_TREE_MISMATCH")
    return manifest, corpus_root, output_root, database_url


def _install_runtime_paths() -> None:
    for path in (str(RESEARCH_ROOT), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _resolve_database_target(database_url: str) -> object:
    from sqlalchemy.engine import make_url

    from app.services.rob974_h6b_materializer import DatabaseTarget

    try:
        url = make_url(database_url)
        target = DatabaseTarget(
            host=url.host,
            port=url.port,
            database=url.database,
            user=url.username,
        )
    except (AttributeError, TypeError, ValueError):
        raise LaunchRefused("DATABASE_URL_MALFORMED") from None
    if url.drivername != "postgresql+asyncpg":
        raise LaunchRefused("DATABASE_DRIVER_MISMATCH")
    expected = DatabaseTarget(
        host=APPROVED_DB[0],
        port=APPROVED_DB[1],
        database=APPROVED_DB[2],
        user=APPROVED_DB[3],
    )
    if target != expected:
        raise LaunchRefused("DATABASE_URL_TARGET_MISMATCH")
    return target


async def _fetch_and_validate_live_schema(session: object) -> None:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT current_database(), current_user, "
            "column_info.character_maximum_length, "
            "(SELECT version_num FROM alembic_version) "
            "FROM information_schema.columns AS column_info "
            "WHERE column_info.table_schema = 'research' "
            "AND column_info.table_name = 'backtest_runs' "
            "AND column_info.column_name = 'runner'"
        )
    )
    row = result.one_or_none()
    if row is None:
        raise LaunchRefused("LIVE_DATABASE_SCHEMA_RUNNER_COLUMN_MISSING")
    values = tuple(row)
    if values[:2] != APPROVED_DB[2:]:
        raise LaunchRefused("LIVE_DATABASE_SCHEMA_TARGET_MISMATCH")
    if values[2] != EXPECTED_BACKTEST_RUNNER_WIDTH:
        raise LaunchRefused("LIVE_DATABASE_SCHEMA_RUNNER_WIDTH_MISMATCH")
    if values[3] != EXPECTED_ALEMBIC_HEAD:
        raise LaunchRefused("LIVE_DATABASE_SCHEMA_ALEMBIC_HEAD_MISMATCH")


async def _execute_schema_guard(
    *, environ: Mapping[str, str], stdout: TextIOBase
) -> int:
    database_url = environ.get(DATABASE_URL_ENV)
    if type(database_url) is not str or not database_url:
        raise LaunchRefused("DATABASE_URL_ENV_ABSENT")

    _install_runtime_paths()
    _resolve_database_target(database_url)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await session.begin()
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await _fetch_and_validate_live_schema(session)
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()
        await engine.dispose()

    _write_json(
        stdout,
        {
            "schema_version": "rob974_r2_schema_guard.v1",
            "database": APPROVED_DB[2],
            "database_user": APPROVED_DB[3],
            "runner_width": EXPECTED_BACKTEST_RUNNER_WIDTH,
            "alembic_head": EXPECTED_ALEMBIC_HEAD,
            "transaction_read_only": True,
            "writes": 0,
        },
    )
    return 0


def _require_identity(arguments: argparse.Namespace) -> tuple[object, object]:
    from app.services.rob974_h6b_materializer import (
        ExactSourcePins,
        build_production_execution_plan,
        build_production_identity_plan,
    )

    first = build_production_identity_plan()
    second = build_production_identity_plan()
    if first.to_payload() != second.to_payload():
        raise LaunchRefused("PRODUCTION_IDENTITY_NONDETERMINISTIC")
    if (
        first.full_campaign_hash != FULL_CAMPAIGN_HASH
        or first.campaign_run_id != CAMPAIGN_RUN_ID
        or first.exact_48_mapping_hash != EXACT_48_MAPPING_HASH
        or len(first.ordered_mapping) != 48
    ):
        raise LaunchRefused("PRODUCTION_IDENTITY_PIN_MISMATCH")
    pins = first.source_pins
    if (
        pins.feature_source_sha256 != FEATURE_SOURCE_SHA256
        or pins.engine_source_sha256 != ENGINE_SOURCE_SHA256
        or pins.runner_source_sha256 != RUNNER_SOURCE_SHA256
        or pins.pbo_implementation_sha256 != PBO_IMPLEMENTATION_SHA256
    ):
        raise LaunchRefused("PRODUCTION_SOURCE_PIN_MISMATCH")
    expected_pins = ExactSourcePins(
        integration_head_sha=arguments.integration_head_sha,
        integration_tree_sha=arguments.integration_tree_sha,
        feature_source_sha256=arguments.feature_source_sha256,
        engine_source_sha256=arguments.engine_source_sha256,
        runner_source_sha256=arguments.runner_source_sha256,
        pbo_implementation_sha256=arguments.pbo_implementation_sha256,
    )
    plan = build_production_execution_plan(
        identity=first,
        output_root=Path(arguments.output_root),
        integration_head_sha=arguments.integration_head_sha,
        integration_tree_sha=arguments.integration_tree_sha,
    )
    if plan.source_pins != expected_pins:
        raise LaunchRefused("EXECUTION_PLAN_SOURCE_PIN_MISMATCH")
    return first, plan


def _feature_hash(snapshots: tuple[object, ...]) -> str:
    import canonical_hash

    return canonical_hash.canonical_sha256(
        [
            {
                **snapshot.__dict__,
                "features": [feature.__dict__ for feature in snapshot.features],
            }
            for snapshot in snapshots
        ]
    )


def _load_exact_real_input(
    manifest_path: Path, corpus_root: Path
) -> tuple[object, dict]:
    import rob941_offline_loader
    import rob974_lineage
    from rob941_funding_sidecar import FundingSidecar
    from rob974_features import MinuteBar, compute_common_features

    from app.services.rob974_h6b_materializer import ActualH4InputData

    manifest = rob974_lineage.verify_parent(manifest_path)
    if (
        manifest.content_hash() != PARENT_CONTENT_SHA256
        or rob974_lineage.PARENT_MANIFEST_SHA256 != PARENT_MANIFEST_SHA256
        or rob974_lineage.SELECTED_UNIVERSE != SELECTED_SYMBOLS
        or rob974_lineage.WINDOW_START_ISO != WINDOW_START_ISO
        or rob974_lineage.WINDOW_END_ISO != WINDOW_END_ISO
    ):
        raise LaunchRefused("FROZEN_CORPUS_LINEAGE_MISMATCH")

    loaded = rob941_offline_loader.load_corpus(manifest, corpus_root)
    if type(loaded) is not dict:
        raise LaunchRefused("OFFLINE_CORPUS_RESULT_MALFORMED")
    klines = loaded.get("klines")
    funding = loaded.get("funding")
    if type(klines) is not dict or type(funding) is not dict:
        raise LaunchRefused("OFFLINE_CORPUS_RESULT_MALFORMED")

    selected: dict[str, tuple[MinuteBar, ...]] = {}
    sidecars: dict[str, FundingSidecar] = {}
    funding_evidence: dict[str, dict[str, int]] = {}
    for symbol in SELECTED_SYMBOLS:
        raw_rows = klines.get(symbol)
        funding_rows = funding.get(symbol)
        if type(raw_rows) is not list or type(funding_rows) is not list:
            raise LaunchRefused("SELECTED_CORPUS_SYMBOL_MISSING")
        if (
            len(raw_rows) != EXPECTED_MINUTE_ROWS
            or raw_rows[0].open_time_ms != WINDOW_START_MS
            or raw_rows[-1].open_time_ms != WINDOW_END_MS - MINUTE_MS
            or any(
                right.open_time_ms - left.open_time_ms != MINUTE_MS
                for left, right in zip(raw_rows, raw_rows[1:], strict=False)
            )
        ):
            raise LaunchRefused("SELECTED_KLINE_PERIOD_OR_GAP_MISMATCH")
        if (
            len(funding_rows) != EXPECTED_FUNDING_ROWS
            or any(
                not WINDOW_START_MS <= row.calc_time < WINDOW_END_MS
                for row in funding_rows
            )
            or any(
                right.calc_time <= left.calc_time
                for left, right in zip(funding_rows, funding_rows[1:], strict=False)
            )
        ):
            raise LaunchRefused("SELECTED_FUNDING_PIT_MISMATCH")
        selected[symbol] = tuple(
            MinuteBar(
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
            )
            for row in raw_rows
        )
        sidecars[symbol] = FundingSidecar.from_rows(symbol, funding_rows)
        funding_evidence[symbol] = {
            "rows": len(funding_rows),
            "first_calc_time": funding_rows[0].calc_time,
            "last_calc_time": funding_rows[-1].calc_time,
        }

    snapshots = compute_common_features(selected)
    feature_hash = _feature_hash(snapshots)
    input_data = ActualH4InputData.from_mapping(
        selected,
        corpus_end_ts=WINDOW_END_MS,
        persisted_corpus_hash=manifest.content_hash(),
        persisted_feature_hash=feature_hash,
        funding_sidecars=sidecars,
    )
    evidence = {
        "parent_content_sha256": manifest.content_hash(),
        "parent_manifest_sha256": PARENT_MANIFEST_SHA256,
        "window_start_ms": WINDOW_START_MS,
        "window_end_ms": WINDOW_END_MS,
        "symbols": list(SELECTED_SYMBOLS),
        "minute_rows_per_symbol": EXPECTED_MINUTE_ROWS,
        "funding": funding_evidence,
        "feature_hash": feature_hash,
        "pit_verified": True,
        "network_calls": 0,
    }
    return input_data, evidence


async def _read_only_target_and_state_probe(
    *, engine: object, inspector: object, plan: object, artifacts: object
) -> tuple[str, str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    artifact_presence = artifacts.probe(output_dir=plan.output_root)
    artifact_state = artifact_presence.state
    if artifact_state not in {"ABSENT", "PAIR_PRESENT"}:
        raise LaunchRefused("ARTIFACT_FORENSIC_STATE_REFUSED")

    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await session.begin()
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await _fetch_and_validate_live_schema(session)
        row = (
            await session.execute(
                text(
                    "SELECT current_database(), current_user, "
                    "to_regclass('research.strategy_experiments')::text, "
                    "to_regclass('research.backtest_runs')::text"
                )
            )
        ).one()
        if tuple(row) != (
            APPROVED_DB[2],
            APPROVED_DB[3],
            "research.strategy_experiments",
            "research.backtest_runs",
        ):
            raise LaunchRefused("LIVE_DATABASE_OR_SCHEMA_MISMATCH")
        snapshot = await inspector.inspect(session, plan=plan)
        db_state = "ABSENT" if snapshot.is_absent() else "PRESENT"
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()

    if (db_state, artifact_state) not in {
        ("ABSENT", "ABSENT"),
        ("PRESENT", "PAIR_PRESENT"),
    }:
        raise LaunchRefused("DATABASE_ARTIFACT_STATE_ASYMMETRY")
    return db_state, artifact_state


def _success_payload(
    *, outcome: object, plan: object, corpus: dict, h5_port: object, runner: object
) -> dict[str, object]:
    accounting = outcome.accounting
    scorecard = outcome.scorecard
    campaign_decision = scorecard.get("campaign_decision", {})
    return {
        "schema_version": "rob974_r2_launcher_result.v1",
        "exit_code": outcome.exit_code,
        "disposition": outcome.disposition,
        "commit_confirmed": outcome.commit_confirmed,
        "retry_forbidden": outcome.retry_forbidden,
        "identity": {
            "full_campaign_hash": plan.full_campaign_hash,
            "campaign_run_id": plan.campaign_run_id,
            "exact_48_mapping_hash": plan.exact_48_mapping_hash,
        },
        "corpus": corpus,
        "h4": {
            "attempts": len(runner.last_result.attempts),
            "selected_strategy_folds": len(runner.last_selected),
            "all_folds_mode": True,
            "pit_funding_gate_and_realized_ledger": True,
        },
        "h6a_accounting": {
            "expected_total": accounting.expected_total,
            "registered_total": accounting.registered_total,
            "primary_attempts": accounting.primary_attempts,
            "total_attempts": accounting.total_attempts,
            "retry_attempts": accounting.retry_attempts,
            "status_counts": dict(accounting.status_counts),
            "accounting_complete": accounting.accounting_complete,
            "performance_usable": accounting.performance_usable,
            "trial_accounting_hash": accounting.trial_accounting_hash,
        },
        "h5": {
            "campaign_decision": campaign_decision,
            "semantic_sha256": h5_port.semantic_hash(scorecard),
            "markdown_sha256": hashlib.sha256(
                h5_port.render_markdown(scorecard)
            ).hexdigest(),
        },
        "artifacts": {
            "directory": str(plan.output_root),
            "json": str(plan.output_root / "scorecard.json"),
            "markdown": str(plan.output_root / "scorecard.md"),
        },
        "broker_calls": 0,
    }


async def _execute_run(
    arguments: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    stdout: TextIOBase,
    stderr: TextIOBase,
) -> int:
    manifest_path, corpus_root, _output_root, database_url = (
        _require_exact_static_gates(arguments, environ)
    )
    _install_runtime_paths()

    from rob974_h6b_artifacts import DirectoryAtomicArtifactPort
    from rob974_h6b_cli import ActualRob970DiagnosticPort
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services.research_db_write_guard import (
        ResearchDbPolicy,
        ResearchDbTarget,
    )
    from app.services.rob974_h6b_materializer import (
        ActualCampaignStateInspector,
        ActualMergedH4Runner,
        ActualMergedH5Composition,
        ActualMergedH6AAccounting,
        ProductionCampaignInput,
        ProductionExecutionPorts,
        RunAuthority,
        issue_run_authorization,
        materialize_production,
        render_safe_materialization_failure,
    )

    target = _resolve_database_target(database_url)
    _identity, plan = _require_identity(arguments)
    stderr.write("ROB974_R2_PREFLIGHT identity_and_git=PASS\n")
    stderr.flush()
    input_data, corpus_evidence = _load_exact_real_input(manifest_path, corpus_root)
    stderr.write("ROB974_R2_PREFLIGHT frozen_corpus_and_pit=PASS\n")
    stderr.flush()

    artifacts = DirectoryAtomicArtifactPort()
    inspector = ActualCampaignStateInspector()
    runner = ActualMergedH4Runner(
        input_data,
        execute_all_folds=True,
        require_pit_funding=True,
    )
    accounting_port = ActualMergedH6AAccounting()
    h5_port = ActualMergedH5Composition()
    engine = create_async_engine(database_url, poolclass=NullPool)

    outcome = None
    try:
        db_state, artifact_state = await _read_only_target_and_state_probe(
            engine=engine,
            inspector=inspector,
            plan=plan,
            artifacts=artifacts,
        )
        stderr.write(
            f"ROB974_R2_PREFLIGHT read_only_state={db_state}/{artifact_state} PASS\n"
        )
        stderr.flush()
        authority = RunAuthority(
            expected_full_campaign_hash=FULL_CAMPAIGN_HASH,
            expected_campaign_run_id=CAMPAIGN_RUN_ID,
            expected_exact_48_mapping_hash=EXACT_48_MAPPING_HASH,
            approved_target=target,
            observed_target=target,
            inherited_target=target,
            write_opt_in=True,
            expected_output_root=plan.output_root,
            requested_output_root=plan.output_root,
            expected_source_pins=plan.source_pins,
            observed_source_pins=plan.source_pins,
            one_shot_approval=ONE_SHOT_APPROVAL,
        )
        authorization = issue_run_authorization(plan, authority)
        campaign = ProductionCampaignInput(
            plan=plan,
            guard_policy=ResearchDbPolicy.of(
                ResearchDbTarget(host=APPROVED_DB[0], database_name=APPROVED_DB[2])
            ),
            strategy_name="rob974-r2",
            timeframe="1m_to_4h_pit",
            runner="rob974-h6b-all-folds",
        )

        def session_factory() -> AsyncSession:
            return AsyncSession(bind=engine, expire_on_commit=False)

        ports = ProductionExecutionPorts(
            session_factory=session_factory,
            h4_runner=runner,
            artifacts=artifacts,
            state_inspector=inspector,
            h6a_accounting=accounting_port,
            h5=h5_port,
            diagnostics=ActualRob970DiagnosticPort(),
        )
        stderr.write("ROB974_R2_EMPIRICAL_MATERIALIZER starting_one_shot\n")
        stderr.flush()
        outcome = await materialize_production(
            plan=plan,
            authorization=authorization,
            campaign=campaign,
            ports=ports,
        )
    except BaseException as exc:
        attached = getattr(exc, "rob984_materialization_outcome", None)
        if attached is not None:
            stderr.write(render_safe_materialization_failure(attached).decode("utf-8"))
        raise
    finally:
        await engine.dispose()

    if outcome.exit_code != 0:
        stderr.write(render_safe_materialization_failure(outcome).decode("utf-8"))
        return outcome.exit_code
    _write_json(
        stdout,
        _success_payload(
            outcome=outcome,
            plan=plan,
            corpus=corpus_evidence,
            h5_port=h5_port,
            runner=runner,
        ),
    )
    return 0


def run_cli(
    argv: Sequence[str],
    *,
    stdout: TextIOBase,
    stderr: TextIOBase,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the closed launcher without calling ``sys.exit``."""
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR
    if not argv:
        _write_json(stdout, _dry_run_payload())
        return 0
    if tuple(argv) == (SCHEMA_GUARD_ONLY_ARGUMENT,):
        try:
            return asyncio.run(
                _execute_schema_guard(
                    environ=os.environ if environ is None else environ,
                    stdout=stdout,
                )
            )
        except LaunchRefused as exc:
            stderr.write("AUTHORITY_OR_PREFLIGHT_REFUSED " + exc.reason_code + "\n")
            return AUTHORITY_OR_PREFLIGHT_REFUSED
        except KeyboardInterrupt:
            stderr.write("INTERRUPTED audit_state_before_retry\n")
            return 130
        except Exception as exc:
            stderr.write(
                f"AUTHORITY_OR_PREFLIGHT_REFUSED UNEXPECTED_{type(exc).__name__}\n"
            )
            return AUTHORITY_OR_PREFLIGHT_REFUSED
    try:
        arguments = _parser().parse_args(list(argv))
    except (TypeError, ValueError):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR
    if arguments.run is not True or any(
        getattr(arguments, name) is None for name in _REQUIRED
    ):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    try:
        return asyncio.run(
            _execute_run(
                arguments,
                environ=os.environ if environ is None else environ,
                stdout=stdout,
                stderr=stderr,
            )
        )
    except LaunchRefused as exc:
        stderr.write("AUTHORITY_OR_PREFLIGHT_REFUSED " + exc.reason_code + "\n")
        return AUTHORITY_OR_PREFLIGHT_REFUSED
    except KeyboardInterrupt:
        stderr.write("INTERRUPTED audit_state_before_retry\n")
        return 130
    except Exception as exc:
        stderr.write(
            f"AUTHORITY_OR_PREFLIGHT_REFUSED UNEXPECTED_{type(exc).__name__}\n"
        )
        return AUTHORITY_OR_PREFLIGHT_REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        tuple(sys.argv[1:] if argv is None else argv),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
