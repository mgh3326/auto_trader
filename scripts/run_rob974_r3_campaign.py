#!/usr/bin/env python3
"""Default-disabled ROB-974 R3 exact-12 empirical campaign launcher.

CP7 contains the complete nonliteral execution, accounting, replay, and
artifact wiring.  The empirical path remains unreachable until the captain
replaces the reviewed final-refreeze literals at CP8.  No arguments and
``--plan`` are pure descriptions; ``--schema-guard-only`` is read-only.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import TextIOBase
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = REPO_ROOT / "research" / "nautilus_scalping"

APPROVED_DB = ("localhost", 5432, "rob974_r3_db", "postgres")
DATABASE_URL_ENV = "ROB974_R3_DATABASE_URL"
EXPECTED_BACKTEST_RUNNER_WIDTH = 64
EXPECTED_ALEMBIC_HEAD = "20260722_rob1023_widen_runner"
EXPECTED_TIMESCALEDB_EXTENSION = "timescaledb"
SCHEMA_GUARD_ONLY_ARGUMENT = "--schema-guard-only"
PLAN_ARGUMENT = "--plan"

PREREGISTRATION_DOCUMENT_SHA256 = (
    "b2f03a23285945c8fda84c56a040fe2466541e8250e0b01ea987ba9d315e7ac5"
)
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
PREREGISTRATION_DOCUMENT = Path(
    "/Users/mgh3326/work/herdr-strategy-prompts/rob974-r3-preregistration-2026-07-22.md"
)
EXPECTED_CORPUS_ROOT = Path(
    "/Users/mgh3326/work/herdr-artifacts/"
    "rob941-4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351/"
    "data"
)
OUTPUT_PARENT = Path("/Users/mgh3326/work/herdr-artifacts")
OUTPUT_ROOT_TEMPLATE = OUTPUT_PARENT / "rob974-r3-<full-hash-prefix>-v1"
PIT_CONFIRMATION = (
    "2025-07-01T00:00:00Z..2026-07-01T00:00:00Z/XRPUSDT,DOGEUSDT,SOLUSDT/PIT/ROB974-R3"
)
WRITE_OPT_IN = "ROB974_R3_EMPIRICAL_WRITE=YES"
ONE_SHOT_APPROVAL = "ROB-974-R3/CP8/ONE-SHOT"
R3_STRATEGY_NAME = "rob974-r3"
R3_TIMEFRAME = "1m_to_4h_pit"
R3_RUNNER = "rob974-r3-all-cell"
PATH_SCENARIOS = ("base13", "primary_stress17", "upward_stress22")
PRIMARY_STRESS_SCENARIO = "primary_stress17"

CLI_USAGE_OR_PLAN_ERROR = 2
AUTHORITY_OR_PREFLIGHT_REFUSED = 4
PRECOMMIT_FAILURE = 5
POSTCOMMIT_PUBLISH_FAILURE = 6
POSTCOMMIT_AUDIT_FAILURE = 7

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ROW_ORDER = tuple(f"S3-R3-{index:02d}" for index in range(3)) + tuple(
    f"S4-R3-{index:02d}" for index in range(9)
)
_FOLD_ORDER = tuple(f"fold-{index:02d}" for index in range(8))


class LaunchRefused(RuntimeError):
    """Fail-closed refusal with a stable, credential-free reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class PrecommitCampaignFailure(RuntimeError):
    """The database commit was not confirmed; retry requires a fresh audit."""


class PostcommitPublishFailure(RuntimeError):
    """The database committed but no-replace artifact publication failed."""


class PostcommitAuditFailure(RuntimeError):
    """Commit and publish completed, but fresh read-only verification failed."""


@dataclass(frozen=True, slots=True)
class FinalRefreezePins:
    """CP8-owned literal block; empty fields are intentionally non-authoritative."""

    status: str
    approved_integration_head_sha: str
    approved_integration_tree_sha: str
    full_campaign_hash: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    feature_source_sha256: str
    engine_source_sha256: str
    runner_source_sha256: str
    pbo_implementation_sha256: str


FINAL_REFREEZE = FinalRefreezePins(
    status="CP8_PENDING_FINAL_REFREEZE",
    approved_integration_head_sha="",
    approved_integration_tree_sha="",
    full_campaign_hash="",
    campaign_run_id="",
    exact_12_mapping_hash="",
    feature_source_sha256="",
    engine_source_sha256="",
    runner_source_sha256="",
    pbo_implementation_sha256="",
)


@dataclass(frozen=True, slots=True)
class ResolvedDatabaseTarget:
    host: str
    port: int
    database: str
    user: str


@dataclass(frozen=True, slots=True)
class LiveSchemaSnapshot:
    database: str
    user: str
    transaction_read_only: str
    timescaledb_version: str
    research_schema_present: bool
    strategy_experiments_regclass: str
    backtest_runs_regclass: str
    runner_width: int
    alembic_head: str


@dataclass(frozen=True, slots=True)
class PreflightState:
    """Auxiliary all-or-none classifier; semantic replay uses issued DTOs too."""

    database_state: str
    artifact_state: str
    registered_rows: int
    attempt_rows: int
    foreign_rows: int
    retry_rows: int
    stale_staging_rows: int
    staging_artifacts: int
    artifact_pair_valid: bool
    artifact_semantic_match: bool

    def __post_init__(self) -> None:
        if self.database_state not in {"ABSENT", "PRESENT"}:
            raise LaunchRefused("DATABASE_FORENSIC_STATE_REFUSED")
        if self.artifact_state not in {
            "ABSENT",
            "PAIR_PRESENT",
            "PARTIAL",
            "INVALID_FINAL",
            "STALE_STAGING",
        }:
            raise LaunchRefused("ARTIFACT_FORENSIC_STATE_REFUSED")
        for name in (
            "registered_rows",
            "attempt_rows",
            "foreign_rows",
            "retry_rows",
            "stale_staging_rows",
            "staging_artifacts",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise LaunchRefused("PREFLIGHT_COUNT_SURFACE_MALFORMED")
        if (
            type(self.artifact_pair_valid) is not bool
            or type(self.artifact_semantic_match) is not bool
        ):
            raise LaunchRefused("PREFLIGHT_ARTIFACT_SURFACE_MALFORMED")


@dataclass(frozen=True, slots=True)
class DatabaseProjection:
    """Read-only raw-to-issued R3 persistence projection."""

    state: PreflightState
    persisted_snapshot: object


@dataclass(frozen=True, slots=True)
class ComputedCampaign:
    """Complete pre-write result of the exact empirical composition."""

    evidence_context: object
    registration_specs: tuple[tuple[object, ...], tuple[object, ...]]
    materialization_contract: object
    attempts: tuple[object, ...]
    accounting: object
    gate_evidence: object
    relaxation_evidence: object
    oos_ledger: object
    scorecard: dict[str, object]
    artifact_pair: object
    m4_artifact_port: object
    engine_invocations: int
    accepted_decision_units: int
    basket_trades: int


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("closed CLI parse failure")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="run_rob974_r3_campaign")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--write-opt-in")
    parser.add_argument("--confirm-full-corpus-pit")
    parser.add_argument("--expected-preregistration-sha256")
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
    "expected_preregistration_sha256",
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


def _dry_run_final_hashes() -> str | dict[str, object]:
    pins = FINAL_REFREEZE
    if pins.status != "CP8_REFROZEN":
        return "PENDING_CP8"
    pins = _require_cp8_final_refreeze()
    return {
        "full_campaign_hash": pins.full_campaign_hash,
        "campaign_run_id": pins.campaign_run_id,
        "exact_12_mapping_hash": pins.exact_12_mapping_hash,
        "source_pins": {
            "feature_source_sha256": pins.feature_source_sha256,
            "engine_source_sha256": pins.engine_source_sha256,
            "runner_source_sha256": pins.runner_source_sha256,
            "pbo_implementation_sha256": pins.pbo_implementation_sha256,
        },
    }


def _dry_run_payload() -> dict[str, object]:
    return {
        "schema_version": "rob974_r3_launcher_plan.v1",
        "default_state": "DISABLED",
        "run_requested": False,
        "refreeze": {
            "status": FINAL_REFREEZE.status,
            "required_checkpoint": "CP8",
        },
        "identity": {
            "lineage": "ROB-974-R3",
            "attempts": 12,
            "family_counts": {"S3": 3, "S4": 9},
            "folds": 8,
            "phases": ["TRAIN", "OOS"],
            "final_hashes": _dry_run_final_hashes(),
        },
        "target": {
            "database": "rob974_r3_db",
            "database_url_source": DATABASE_URL_ENV,
            "required_runner_width": EXPECTED_BACKTEST_RUNNER_WIDTH,
            "required_alembic_head": EXPECTED_ALEMBIC_HEAD,
            "output_root_template": str(OUTPUT_ROOT_TEMPLATE),
        },
        "effects": {
            "empirical_runs": 0,
            "database_connections": 0,
            "database_queries": 0,
            "database_writes": 0,
            "corpus_reads": 0,
            "artifact_reads": 0,
            "artifact_writes": 0,
            "network_calls": 0,
            "broker_calls": 0,
            "orders": 0,
            "fills": 0,
        },
    }


def _install_runtime_paths() -> None:
    for path in (str(RESEARCH_ROOT), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _resolve_database_target(database_url: str) -> ResolvedDatabaseTarget:
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
        target = ResolvedDatabaseTarget(
            host=url.host or "",
            port=url.port or 0,
            database=url.database or "",
            user=url.username or "",
        )
    except (AttributeError, TypeError, ValueError):
        raise LaunchRefused("DATABASE_URL_MALFORMED") from None
    if url.drivername != "postgresql+asyncpg":
        raise LaunchRefused("DATABASE_DRIVER_MISMATCH")
    expected = ResolvedDatabaseTarget(*APPROVED_DB)
    if target != expected:
        raise LaunchRefused("DATABASE_URL_TARGET_MISMATCH")
    return target


def _create_read_only_engine(database_url: str) -> object:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )


def _create_write_engine(database_url: str) -> object:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(
        database_url,
        poolclass=NullPool,
        execution_options={"isolation_level": "SERIALIZABLE"},
    )


async def _fetch_and_validate_live_schema(session: object) -> LiveSchemaSnapshot:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT current_database(), current_user, "
            "current_setting('transaction_read_only'), "
            "(SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), "
            "EXISTS (SELECT 1 FROM information_schema.schemata "
            "WHERE schema_name = 'research'), "
            "to_regclass('research.strategy_experiments')::text, "
            "to_regclass('research.backtest_runs')::text, "
            "(SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = 'research' AND table_name = 'backtest_runs' "
            "AND column_name = 'runner'), "
            "(SELECT version_num FROM alembic_version)"
        )
    )
    values = tuple(result.one())
    if values != (
        "rob974_r3_db",
        "postgres",
        "on",
        values[3],
        True,
        "research.strategy_experiments",
        "research.backtest_runs",
        EXPECTED_BACKTEST_RUNNER_WIDTH,
        EXPECTED_ALEMBIC_HEAD,
    ):
        raise LaunchRefused("LIVE_DATABASE_SCHEMA_MISMATCH")
    if type(values[3]) is not str or not values[3]:
        raise LaunchRefused("LIVE_DATABASE_TIMESCALEDB_MISSING")
    return LiveSchemaSnapshot(*values)


async def _read_only_schema_session(session: object) -> LiveSchemaSnapshot:
    from sqlalchemy import text

    try:
        await session.begin()
        await session.execute(text("SET TRANSACTION READ ONLY"))
        return await _fetch_and_validate_live_schema(session)
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()


async def _execute_schema_guard(
    *, environ: Mapping[str, str], stdout: TextIOBase
) -> int:
    database_url = environ.get(DATABASE_URL_ENV)
    if type(database_url) is not str or not database_url:
        raise LaunchRefused("DATABASE_URL_ENV_ABSENT")
    _resolve_database_target(database_url)
    _install_runtime_paths()
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = _create_read_only_engine(database_url)
    try:
        snapshot = await _read_only_schema_session(
            AsyncSession(bind=engine, expire_on_commit=False)
        )
    finally:
        await engine.dispose()
    _write_json(
        stdout,
        {
            "schema_version": "rob974_r3_schema_guard.v1",
            "database": snapshot.database,
            "database_user": snapshot.user,
            "timescaledb_version": snapshot.timescaledb_version,
            "runner_width": snapshot.runner_width,
            "alembic_head": snapshot.alembic_head,
            "connection_default_transaction_read_only": True,
            "transaction_read_only": True,
            "rollback": True,
            "writes": 0,
        },
    )
    return 0


def _output_root_for(full_campaign_hash: str) -> Path:
    if _HEX64_RE.fullmatch(full_campaign_hash) is None:
        raise LaunchRefused("FULL_CAMPAIGN_HASH_MALFORMED")
    return OUTPUT_PARENT / f"rob974-r3-{full_campaign_hash[:8]}-v1"


def _build_candidate_production_plan() -> object:
    _install_runtime_paths()
    from rob974_r3_plan import build_production_r3_plan

    return build_production_r3_plan()


def _validate_production_plan_shape(plan: object) -> None:
    try:
        mapping = tuple(plan.ordered_mapping)
        folds = tuple(plan.folds)
        source_pins = plan.source_pins
    except AttributeError:
        raise LaunchRefused("PRODUCTION_PLAN_TYPE_OR_SHAPE_MISMATCH") from None
    if tuple(row_id for row_id, _ in mapping) != _ROW_ORDER or len(mapping) != 12:
        raise LaunchRefused("PRODUCTION_PLAN_EXACT_12_MISMATCH")
    if tuple(fold.fold_id for fold in folds) != _FOLD_ORDER or len(folds) != 8:
        raise LaunchRefused("PRODUCTION_PLAN_EXACT_8_FOLDS_MISMATCH")
    hex_values = (
        plan.full_campaign_hash,
        plan.exact_12_mapping_hash,
        *(experiment_id for _, experiment_id in mapping),
        source_pins.feature_source_sha256,
        source_pins.engine_source_sha256,
        source_pins.runner_source_sha256,
        source_pins.pbo_implementation_sha256,
    )
    if any(_HEX64_RE.fullmatch(value) is None for value in hex_values):
        raise LaunchRefused("PRODUCTION_PLAN_IDENTITY_MALFORMED")
    if type(plan.campaign_run_id) is not str or not plan.campaign_run_id.startswith(
        "rob974r3-"
    ):
        raise LaunchRefused("PRODUCTION_PLAN_RUN_ID_MALFORMED")


def _require_plan_matches_refreeze(plan: object, pins: FinalRefreezePins) -> None:
    _validate_production_plan_shape(plan)
    source_pins = plan.source_pins
    observed = (
        plan.full_campaign_hash,
        plan.campaign_run_id,
        plan.exact_12_mapping_hash,
        source_pins.feature_source_sha256,
        source_pins.engine_source_sha256,
        source_pins.runner_source_sha256,
        source_pins.pbo_implementation_sha256,
    )
    expected = (
        pins.full_campaign_hash,
        pins.campaign_run_id,
        pins.exact_12_mapping_hash,
        pins.feature_source_sha256,
        pins.engine_source_sha256,
        pins.runner_source_sha256,
        pins.pbo_implementation_sha256,
    )
    if observed != expected:
        raise LaunchRefused("DERIVED_PLAN_DIFFERS_FROM_CP8_REFREEZE")


def _preflight_disposition(state: PreflightState) -> str:
    if type(state) is not PreflightState:
        raise LaunchRefused("PREFLIGHT_STATE_TYPE_MISMATCH")
    if (
        state.foreign_rows
        or state.retry_rows
        or state.stale_staging_rows
        or state.staging_artifacts
    ):
        raise LaunchRefused("FOREIGN_RETRY_OR_STAGING_STATE_REFUSED")
    if (
        state.database_state == "ABSENT"
        and state.artifact_state == "ABSENT"
        and state.registered_rows == 0
        and state.attempt_rows == 0
        and not state.artifact_pair_valid
        and not state.artifact_semantic_match
    ):
        return "RUN_ONCE"
    if (
        state.database_state == "PRESENT"
        and state.artifact_state == "PAIR_PRESENT"
        and state.registered_rows == 12
        and state.attempt_rows == 12
        and state.artifact_pair_valid
        and state.artifact_semantic_match
    ):
        return "REPLAY_NOOP"
    raise LaunchRefused("DATABASE_ARTIFACT_STATE_ASYMMETRY_OR_PARTIAL")


def _dispatch_preflight_disposition(
    *,
    state: PreflightState,
    run_once: Callable[[], Any],
    replay_noop: Callable[[], Any],
) -> Any:
    disposition = _preflight_disposition(state)
    if disposition == "REPLAY_NOOP":
        return replay_noop()
    return run_once()


def _require_cp8_final_refreeze() -> FinalRefreezePins:
    pins = FINAL_REFREEZE
    if pins.status != "CP8_REFROZEN":
        raise LaunchRefused("CP8_PENDING_FINAL_REFREEZE")
    if (
        _HEX40_RE.fullmatch(pins.approved_integration_head_sha) is None
        or _HEX40_RE.fullmatch(pins.approved_integration_tree_sha) is None
    ):
        raise LaunchRefused("CP8_REFREEZE_GIT_PINS_MALFORMED")
    for name in (
        "full_campaign_hash",
        "exact_12_mapping_hash",
        "feature_source_sha256",
        "engine_source_sha256",
        "runner_source_sha256",
        "pbo_implementation_sha256",
    ):
        if _HEX64_RE.fullmatch(getattr(pins, name)) is None:
            raise LaunchRefused("CP8_REFREEZE_IDENTITY_PINS_MALFORMED")
    if not pins.campaign_run_id.startswith("rob974r3-"):
        raise LaunchRefused("CP8_REFREEZE_RUN_ID_MALFORMED")
    return pins


def _git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _require_single_cp8_launcher_descendant(pins: FinalRefreezePins) -> None:
    commit_count = _git(
        "rev-list", "--count", f"{pins.approved_integration_head_sha}..HEAD"
    )
    changed_paths = tuple(
        path
        for path in _git(
            "diff",
            "--name-only",
            pins.approved_integration_head_sha,
            "HEAD",
        ).splitlines()
        if path
    )
    if commit_count != "1" or changed_paths != ("scripts/run_rob974_r3_campaign.py",):
        raise LaunchRefused("CP8_DESCENDANT_NOT_SINGLE_LAUNCHER_ONLY_COMMIT")


def _require_exact_static_gates(
    arguments: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    pins: FinalRefreezePins,
) -> tuple[Path, Path, Path, str]:
    expected = {
        "write_opt_in": WRITE_OPT_IN,
        "confirm_full_corpus_pit": PIT_CONFIRMATION,
        "expected_preregistration_sha256": PREREGISTRATION_DOCUMENT_SHA256,
        "expected_full_campaign_hash": pins.full_campaign_hash,
        "campaign_run_id": pins.campaign_run_id,
        "expected_mapping_hash": pins.exact_12_mapping_hash,
        "feature_source_sha256": pins.feature_source_sha256,
        "engine_source_sha256": pins.engine_source_sha256,
        "runner_source_sha256": pins.runner_source_sha256,
        "pbo_implementation_sha256": pins.pbo_implementation_sha256,
        "approved_db_host": "localhost",
        "approved_db_port": 5432,
        "approved_db_name": "rob974_r3_db",
        "approved_db_user": "postgres",
        "database_url_env": DATABASE_URL_ENV,
        "one_shot_approval": ONE_SHOT_APPROVAL,
    }
    if any(getattr(arguments, name) != value for name, value in expected.items()):
        raise LaunchRefused("EXPLICIT_GATE_LITERAL_MISMATCH")
    launcher_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if arguments.launcher_sha256 != launcher_hash:
        raise LaunchRefused("LAUNCHER_PHYSICAL_SHA256_MISMATCH")
    try:
        preregistration_hash = hashlib.sha256(
            PREREGISTRATION_DOCUMENT.read_bytes()
        ).hexdigest()
    except OSError:
        raise LaunchRefused("PREREGISTRATION_DOCUMENT_UNAVAILABLE") from None
    if preregistration_hash != PREREGISTRATION_DOCUMENT_SHA256:
        raise LaunchRefused("PREREGISTRATION_DOCUMENT_PHYSICAL_SHA256_MISMATCH")
    database_url = environ.get(DATABASE_URL_ENV)
    if type(database_url) is not str or not database_url:
        raise LaunchRefused("DATABASE_URL_ENV_ABSENT")
    _resolve_database_target(database_url)
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
    if output_root != _output_root_for(pins.full_campaign_hash):
        raise LaunchRefused("OUTPUT_ROOT_PATH_MISMATCH")
    try:
        if _git("status", "--porcelain", "--untracked-files=all"):
            raise LaunchRefused("WORKTREE_NOT_CLEAN")
        head = _git("rev-parse", "HEAD")
        tree = _git("rev-parse", "HEAD^{tree}")
        approved_tree = _git(
            "rev-parse", f"{pins.approved_integration_head_sha}^{{tree}}"
        )
        subprocess.run(
            (
                "git",
                "merge-base",
                "--is-ancestor",
                pins.approved_integration_head_sha,
                "HEAD",
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        _require_single_cp8_launcher_descendant(pins)
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("INTEGRATION_GIT_STATE_REFUSED") from None
    if approved_tree != pins.approved_integration_tree_sha:
        raise LaunchRefused("APPROVED_INTEGRATION_TREE_MISMATCH")
    if head != arguments.integration_head_sha or tree != arguments.integration_tree_sha:
        raise LaunchRefused("INTEGRATION_HEAD_OR_TREE_MISMATCH")
    return manifest, corpus_root, output_root, database_url


def _load_m4_api() -> SimpleNamespace:
    # Stable Wave-3 M4 public seam.  Imports stay off the default/plan paths.
    from rob974_r3_markdown import (
        render_r3_markdown,
        verify_r3_markdown_semantic_binding,
    )
    from rob974_r3_scorecard import (
        R3_SCORECARD_SCHEMA_VERSION,
        R3CellOOSInput,
        R3FoldOOSInput,
        build_r3_artifact_pair,
        build_r3_scorecard,
        canonical_r3_json_bytes,
        hash_r3_canonical_bytes,
        issue_r3_all_cell_oos_ledger,
        issue_r3_fold_scenario_attribution,
        issue_r3_market_input_authority,
        issue_r3_scorecard_accounting,
        issue_r3_scorecard_relaxation_evidence,
        verify_r3_artifact_pair,
    )

    if R3_SCORECARD_SCHEMA_VERSION != "rob974.r3.h5.scorecard.v1":
        raise LaunchRefused("M4_SCORECARD_SCHEMA_VERSION_DRIFT")
    return SimpleNamespace(
        R3_SCORECARD_SCHEMA_VERSION=R3_SCORECARD_SCHEMA_VERSION,
        R3CellOOSInput=R3CellOOSInput,
        R3FoldOOSInput=R3FoldOOSInput,
        build_r3_artifact_pair=build_r3_artifact_pair,
        build_r3_scorecard=build_r3_scorecard,
        canonical_r3_json_bytes=canonical_r3_json_bytes,
        hash_r3_canonical_bytes=hash_r3_canonical_bytes,
        issue_r3_all_cell_oos_ledger=issue_r3_all_cell_oos_ledger,
        issue_r3_fold_scenario_attribution=issue_r3_fold_scenario_attribution,
        issue_r3_market_input_authority=issue_r3_market_input_authority,
        issue_r3_scorecard_accounting=issue_r3_scorecard_accounting,
        issue_r3_scorecard_relaxation_evidence=(issue_r3_scorecard_relaxation_evidence),
        verify_r3_artifact_pair=verify_r3_artifact_pair,
        render_r3_markdown=render_r3_markdown,
        verify_r3_markdown_semantic_binding=verify_r3_markdown_semantic_binding,
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _feature_hash(snapshots: tuple[object, ...]) -> str:
    from research_contracts.canonical_hash import canonical_sha256

    return canonical_sha256(
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
    """Load the already-materialized frozen corpus without network access."""

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
    return input_data, {
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


def _generate_r3_s3(*, source: object, config: object) -> object:
    """Generate one R3 S3 buffer from the exact §5 formula-unit authority."""

    import rob974_h3_s3 as s3
    from rob974_r3_h3_adapter import evaluate_r3_s3_gates

    decisions: list[object] = []
    accepted: list[object] = []
    rejected: list[object] = []
    decision_times = tuple(dict.fromkeys(unit.decision_ts for unit in source.units))
    for decision_ts in decision_times:
        at_close = tuple(
            unit for unit in source.units if unit.decision_ts == decision_ts
        )
        outcomes: dict[str, object] = {}
        candidates: list[object] = []
        for unit in at_close:
            outcome = (
                s3.S3GateOutcome(None, None, "missing_required_context")
                if unit.metrics is None
                else evaluate_r3_s3_gates(unit.metrics, config)
            )
            outcomes[unit.symbol] = outcome
            if outcome.candidate is not None:
                candidates.append(outcome.candidate)
        arbitration = s3.arbitrate_s3_candidates(candidates) if candidates else None
        winner_id = arbitration.winner.identity if arbitration is not None else None
        loser_by_id = (
            {item.candidate.identity: item for item in arbitration.rejected}
            if arbitration is not None
            else {}
        )
        if arbitration is not None:
            accepted.append(arbitration.winner)
            rejected.extend(arbitration.rejected)
        for unit in at_close:
            outcome = outcomes[unit.symbol]
            candidate = outcome.candidate
            if candidate is None:
                decisions.append(
                    s3.S3UnitDecision(
                        decision_ts,
                        unit.symbol,
                        "NO_SIGNAL",
                        outcome.side,
                        None,
                        outcome.no_signal_reason,
                        None,
                    )
                )
            elif candidate.identity == winner_id:
                decisions.append(
                    s3.S3UnitDecision(
                        decision_ts,
                        unit.symbol,
                        "GENERATOR_ACCEPTED",
                        candidate.side,
                        candidate,
                        None,
                        None,
                    )
                )
            else:
                decisions.append(
                    s3.S3UnitDecision(
                        decision_ts,
                        unit.symbol,
                        "GENERATOR_REJECTED",
                        candidate.side,
                        candidate,
                        None,
                        loser_by_id[candidate.identity].reason,
                    )
                )
    output = s3.S3GeneratorOutput(
        "S3", config.config_id, tuple(decisions), tuple(accepted), tuple(rejected)
    )
    source_identities = tuple((unit.decision_ts, unit.symbol) for unit in source.units)
    output_identities = tuple(
        (decision.decision_ts, decision.symbol) for decision in output.decisions
    )
    if source_identities != output_identities:
        raise LaunchRefused("S3_GATE_SOURCE_GENERATOR_UNIT_DRIFT")
    return output


def _generate_r3_s4(
    *, feature_context: object, source: object, config: object
) -> object:
    """Generate one low-z R3 S4 buffer from the frozen S4-02 formula core."""

    import rob974_h3_s4 as s4
    from rob974_h3_manifest import get_config
    from rob974_r3_h3_adapter import (
        R3S4GateObservation,
        evaluate_r3_s4_gates,
    )

    anchor = get_config("S4-02")
    decisions: list[object] = []
    accepted: list[object] = []
    rejected: list[object] = []
    decision_times = tuple(dict.fromkeys(unit.decision_ts for unit in source.units))
    for decision_ts in decision_times:
        at_close = tuple(
            unit for unit in source.units if unit.decision_ts == decision_ts
        )
        outcomes: dict[str, object] = {}
        candidates: list[object] = []
        for unit in at_close:
            estimate_outcome = s4.estimate_s4_pair(
                feature_context, anchor, decision_ts, unit.pair
            )
            if unit.observation is None:
                if estimate_outcome.estimate is not None or (
                    estimate_outcome.rejection_reason != unit.context_failure_reason
                ):
                    raise LaunchRefused("S4_GATE_SOURCE_FORMULA_CONTEXT_DRIFT")
                outcome = s4.S4GateOutcome(None, None, unit.context_failure_reason)
            elif not 0.0 < unit.observation.phi < 1.0:
                if (
                    estimate_outcome.estimate is not None
                    or estimate_outcome.rejection_reason
                    != "phi_not_in_open_unit_interval"
                ):
                    raise LaunchRefused("S4_GATE_SOURCE_PHI_DRIFT")
                outcome = s4.S4GateOutcome(None, None, "phi_not_in_open_unit_interval")
            else:
                estimate = estimate_outcome.estimate
                if estimate is None:
                    raise LaunchRefused("S4_GATE_SOURCE_FORMULA_ESTIMATE_DRIFT")
                rebound = dataclasses.replace(estimate, config_id=config.config_id)
                if R3S4GateObservation.from_estimate(rebound) != unit.observation:
                    raise LaunchRefused("S4_GATE_SOURCE_ESTIMATE_VALUE_DRIFT")
                outcome = evaluate_r3_s4_gates(rebound, config)
            outcomes[unit.pair] = outcome
            if outcome.candidate is not None:
                candidates.append(outcome.candidate)
        arbitration = s4.arbitrate_s4_candidates(candidates) if candidates else None
        winner_id = arbitration.winner.identity if arbitration is not None else None
        loser_by_id = (
            {item.candidate.identity: item for item in arbitration.rejected}
            if arbitration is not None
            else {}
        )
        if arbitration is not None:
            accepted.append(arbitration.winner)
            rejected.extend(arbitration.rejected)
        for unit in at_close:
            outcome = outcomes[unit.pair]
            candidate = outcome.candidate
            if candidate is None:
                decisions.append(
                    s4.S4UnitDecision(
                        decision_ts,
                        unit.pair,
                        "NO_SIGNAL",
                        outcome.side,
                        None,
                        outcome.no_signal_reason,
                        None,
                    )
                )
            elif candidate.identity == winner_id:
                decisions.append(
                    s4.S4UnitDecision(
                        decision_ts,
                        unit.pair,
                        "GENERATOR_ACCEPTED",
                        candidate.side,
                        candidate,
                        None,
                        None,
                    )
                )
            else:
                decisions.append(
                    s4.S4UnitDecision(
                        decision_ts,
                        unit.pair,
                        "GENERATOR_REJECTED",
                        candidate.side,
                        candidate,
                        None,
                        loser_by_id[candidate.identity].reason,
                    )
                )
    output = s4.S4GeneratorOutput(
        "S4", config.config_id, tuple(decisions), tuple(accepted), tuple(rejected)
    )
    source_identities = tuple((unit.decision_ts, unit.pair) for unit in source.units)
    output_identities = tuple(
        (decision.decision_ts, decision.pair) for decision in output.decisions
    )
    if source_identities != output_identities:
        raise LaunchRefused("S4_GATE_SOURCE_GENERATOR_UNIT_DRIFT")
    return output


def _prepare_s3_funding_once(
    *, intents: tuple[object, ...], input_data: object, surface: object
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    import rob944_gap_funding as h4_funding
    import rob974_h2_s3_engine as s3_engine
    import rob974_h4_selection as selection
    from rob974_h2_dtos import S3NoTradeRecord, S3SignalIntent

    sidecars = dict(input_data.funding_sidecars)
    accepted: list[object] = []
    rejected: list[object] = []
    for intent in intents:
        if type(intent) is not S3SignalIntent:
            raise LaunchRefused("S3_FUNDING_INPUT_TYPE_DRIFT")
        if (intent.symbol, intent.signal_ts) not in surface.minute_index:
            accepted.append(intent)
            continue
        observed = h4_funding.evaluate_funding_entry_gate(
            sidecars[intent.symbol],
            side=intent.side,
            entry_ts_ms=intent.signal_ts,
            max_hold_ms=s3_engine.MAX_HOLD_MS,
        )
        gate = selection.s3_funding_gate(observed.expected_cost_bps)
        if gate.accepted:
            accepted.append(intent)
        else:
            rejected.append(
                S3NoTradeRecord(
                    symbol=intent.symbol,
                    side=intent.side,
                    config_id=intent.config_id,
                    fold_id=intent.fold_id,
                    signal_ts=intent.signal_ts,
                    reason=gate.reason or "funding_gate_rejected",
                )
            )
    return tuple(accepted), tuple(rejected)


def _prepare_s4_funding_once(
    *, intents: tuple[object, ...], input_data: object, surface: object
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    import rob944_gap_funding as h4_funding
    import rob974_h2_s4_engine as s4_engine
    import rob974_h4_selection as selection
    from rob974_r3_s4_dtos import R3S4NoTradeRecord, R3S4PairSignalIntent

    sidecars = dict(input_data.funding_sidecars)
    accepted: list[object] = []
    rejected: list[object] = []
    for intent in intents:
        if type(intent) is not R3S4PairSignalIntent:
            raise LaunchRefused("S4_FUNDING_INPUT_TYPE_DRIFT")
        if any(
            (symbol, intent.signal_ts) not in surface.minute_index
            for symbol in intent.pair
        ):
            accepted.append(intent)
            continue
        observed_a = h4_funding.evaluate_funding_entry_gate(
            sidecars[intent.pair[0]],
            side=intent.side_a,
            entry_ts_ms=intent.signal_ts,
            max_hold_ms=s4_engine.MAX_HOLD_MS,
        )
        observed_b = h4_funding.evaluate_funding_entry_gate(
            sidecars[intent.pair[1]],
            side=intent.side_b,
            entry_ts_ms=intent.signal_ts,
            max_hold_ms=s4_engine.MAX_HOLD_MS,
        )
        gate = selection.s4_funding_gate(
            leg_a_signed_bps=observed_a.expected_cost_bps,
            leg_b_signed_bps=observed_b.expected_cost_bps,
            weight_a=intent.weight_a,
            weight_b=intent.weight_b,
        )
        if gate.accepted:
            accepted.append(intent)
        else:
            rejected.append(
                R3S4NoTradeRecord(
                    pair=intent.pair,
                    config_id=intent.config_id,
                    fold_id=intent.fold_id,
                    signal_ts=intent.signal_ts,
                    reason=gate.reason or "funding_gate_rejected",
                )
            )
    return tuple(accepted), tuple(rejected)


def _invoke_s3_path(
    *,
    all_intents: tuple[object, ...],
    accepted_intents: tuple[object, ...],
    funding_rejections: tuple[object, ...],
    input_data: object,
    surface: object,
    config_id: str,
    fold_id: str,
    horizon_end_ts: int,
) -> object:
    import rob974_h4_adapter as h4_adapter
    from rob974_h2_dtos import S3EngineResult

    terminal = h4_adapter.invoke_actual_s3_engine(
        candidates=accepted_intents,
        minute_index=surface.minute_index,
        close_feature_index=surface.close_feature_index,
        corpus_end_ts=input_data.corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
        strategy="S3",
        config_id=config_id,
        fold_id=fold_id,
    )
    result = S3EngineResult(
        terminal.result.trades,
        (*terminal.result.no_trades, *funding_rejections),
        terminal.result.incompletes,
    )
    h4_adapter.validate_s3_terminal(all_intents, result)
    return h4_adapter.SealedS3Terminal(
        result,
        h4_adapter.seal_s3_engine_input(
            all_intents,
            corpus_end_ts=input_data.corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        ),
        h4_adapter.seal_s3_engine_output(result),
    )


def _invoke_s4_path(
    *,
    all_intents: tuple[object, ...],
    accepted_intents: tuple[object, ...],
    funding_rejections: tuple[object, ...],
    input_data: object,
    surface: object,
    config_id: str,
    fold_id: str,
    horizon_end_ts: int,
) -> object:
    from rob974_r3_h4_s4_adapter import (
        SealedR3S4Terminal,
        invoke_r3_s4_engine,
        seal_r3_s4_engine_input,
        seal_r3_s4_engine_output,
        validate_r3_s4_terminal,
    )
    from rob974_r3_s4_dtos import R3S4EngineResult

    terminal = invoke_r3_s4_engine(
        candidates=accepted_intents,
        minute_index=surface.minute_index,
        pair_close_index=surface.pair_close_index,
        corpus_end_ts=input_data.corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
        strategy="S4",
        config_id=config_id,
        fold_id=fold_id,
    )
    result = R3S4EngineResult(
        terminal.result.trades,
        (*terminal.result.no_trades, *funding_rejections),
        terminal.result.incompletes,
    )
    validate_r3_s4_terminal(all_intents, result, config_id=config_id, fold_id=fold_id)
    return SealedR3S4Terminal(
        result,
        seal_r3_s4_engine_input(
            all_intents,
            corpus_end_ts=input_data.corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        ),
        seal_r3_s4_engine_output(result),
    )


def _member_trade_key(trade: object) -> str:
    from research_contracts.canonical_hash import canonical_sha256

    if not dataclasses.is_dataclass(trade):
        raise LaunchRefused("RAW_TRADE_MEMBER_TYPE_DRIFT")
    return canonical_sha256(dataclasses.asdict(trade))


def _execute_exact_three_paths(
    *,
    config: object,
    fold: object,
    phase: str,
    generator_output: object,
    input_data: object,
    surface: object,
) -> tuple[tuple[tuple[str, object], ...], tuple[dict[str, object], ...]]:
    """Run three fresh engines after one funding decision and prove raw parity."""

    from rob974_h3_h2_adapter import adapt_s3_candidate
    from rob974_r3_h3_adapter import adapt_r3_s4_candidate_for_execution
    from rob974_r3_manifest import R3S3Config, R3S4Config
    from rob974_r3_relaxation_h2_adapter import R3H2CellFoldInput

    candidates = tuple(generator_output.accepted)
    if type(config) is R3S3Config:
        intents = tuple(
            adapt_s3_candidate(candidate, fold_id=fold.fold_id)
            for candidate in candidates
        )
        accepted, funding_rejections = _prepare_s3_funding_once(
            intents=intents, input_data=input_data, surface=surface
        )

        def invoke() -> object:
            return _invoke_s3_path(
                all_intents=intents,
                accepted_intents=accepted,
                funding_rejections=funding_rejections,
                input_data=input_data,
                surface=surface,
                config_id=config.config_id,
                fold_id=fold.fold_id,
                horizon_end_ts=(
                    fold.train_end_ms if phase == "TRAIN" else fold.oos_end_ms
                ),
            )

    elif type(config) is R3S4Config:
        intents = tuple(
            adapt_r3_s4_candidate_for_execution(candidate, fold_id=fold.fold_id)
            for candidate in candidates
        )
        accepted, funding_rejections = _prepare_s4_funding_once(
            intents=intents, input_data=input_data, surface=surface
        )

        def invoke() -> object:
            return _invoke_s4_path(
                all_intents=intents,
                accepted_intents=accepted,
                funding_rejections=funding_rejections,
                input_data=input_data,
                surface=surface,
                config_id=config.config_id,
                fold_id=fold.fold_id,
                horizon_end_ts=(
                    fold.train_end_ms if phase == "TRAIN" else fold.oos_end_ms
                ),
            )

    else:
        raise LaunchRefused("R3_CELL_CONFIG_TYPE_DRIFT")

    sources: list[tuple[str, object]] = []
    path_evidence: list[dict[str, object]] = []
    for scenario in PATH_SCENARIOS:
        terminal = invoke()
        source = R3H2CellFoldInput(
            config=config,
            fold_id=fold.fold_id,
            h3_candidates=candidates,
            engine_intents=intents,
            corpus_end_ts=input_data.corpus_end_ts,
            horizon_end_ts=(fold.train_end_ms if phase == "TRAIN" else fold.oos_end_ms),
            terminal=terminal,
        )
        sources.append((scenario, source))
        path_evidence.append(
            {
                "path_scenario": scenario,
                "input_seal_sha256": terminal.input_seal_sha256,
                "output_seal_sha256": terminal.output_seal_sha256,
                "member_trade_keys": sorted(
                    _member_trade_key(trade) for trade in terminal.result.trades
                ),
                "basket_trades": len(terminal.result.trades),
                "no_trades": len(terminal.result.no_trades),
                "incompletes": len(terminal.result.incompletes),
                "terminal_incomplete_rows": [
                    _plain(dataclasses.asdict(row))
                    for row in terminal.result.incompletes
                ],
            }
        )
    primary = dict(sources)[PRIMARY_STRESS_SCENARIO]
    for scenario, source in sources:
        if source is primary or (
            source.h3_candidates == primary.h3_candidates
            and source.engine_intents == primary.engine_intents
            and source.terminal.result == primary.terminal.result
            and source.terminal.input_seal_sha256 == primary.terminal.input_seal_sha256
            and source.terminal.output_seal_sha256
            == primary.terminal.output_seal_sha256
        ):
            continue
        raise LaunchRefused(f"RAW_PATH_PARITY_DRIFT_{scenario}")
    return tuple(sources), tuple(path_evidence)


def _registration_specs(plan: object) -> tuple[tuple[object, ...], tuple[object, ...]]:
    from app.schemas.research_backtest import StrategyExperimentIdentity

    s3_specs: list[object] = []
    s4_specs: list[object] = []
    for spec in plan.row_specs:
        components = _plain(spec.components)
        if type(components) is not dict:
            raise LaunchRefused("REGISTRATION_COMPONENT_SURFACE_MALFORMED")
        identity = StrategyExperimentIdentity(
            strategy_key=spec.strategy_key,
            strategy_version=spec.strategy_version,
            hypothesis=spec.hypothesis,
            **components,
        )
        (s3_specs if spec.row_id.startswith("S3-") else s4_specs).append(identity)
    if len(s3_specs) != 3 or len(s4_specs) != 9:
        raise LaunchRefused("REGISTRATION_FAMILY_SPLIT_NOT_EXACT_3_9")
    return tuple(s3_specs), tuple(s4_specs)


def _canonical_dataclass_hash(value: object) -> str:
    from research_contracts.canonical_hash import canonical_sha256

    if not dataclasses.is_dataclass(value):
        raise LaunchRefused("EVIDENCE_DATACLASS_TYPE_REQUIRED")
    return canonical_sha256(_plain(dataclasses.asdict(value)))


def _build_attempts(
    *,
    plan: object,
    cell_evidence: Mapping[str, list[dict[str, object]]],
    gate_reports: Mapping[tuple[str, str], object],
) -> tuple[object, ...]:
    from rob974_r3_relaxation import (
        RelaxationInputError,
        derive_terminal_attempt_reason,
    )

    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem
    from research_contracts.canonical_hash import canonical_sha256

    attempts: list[object] = []
    experiment_by_row = dict(plan.ordered_mapping)
    for config in plan.manifest_rows:
        row_id = config.config_id
        cells = tuple(cell_evidence[row_id])
        headers = tuple((cell["phase"], cell["fold_id"]) for cell in cells)
        expected_headers = tuple(
            (phase, fold.fold_id) for phase in ("TRAIN", "OOS") for fold in plan.folds
        )
        if headers != expected_headers:
            raise LaunchRefused("ATTEMPT_PHASE_FOLD_EVIDENCE_ORDER_DRIFT")
        evidence_payload = {
            "schema_version": "rob974.r3.h6a.attempt_evidence.v1",
            "row_id": row_id,
            "phase_fold_paths": list(cells),
            "section5_gate_report_sha256": {
                phase: _canonical_dataclass_hash(gate_reports[(row_id, phase)])
                for phase in ("TRAIN", "OOS")
            },
            "primary_relaxation_path": PRIMARY_STRESS_SCENARIO,
            "path_scenarios": list(PATH_SCENARIOS),
            "funding_gate_projection": "once_before_three_fresh_engines",
        }
        incomplete_rows = tuple(
            row
            for cell in cells
            for path in cell["path_evidence"]
            for row in path["terminal_incomplete_rows"]
        )
        try:
            reason_code = derive_terminal_attempt_reason(incomplete_rows)
        except (TypeError, RelaxationInputError) as exc:
            raise LaunchRefused("ATTEMPT_TERMINAL_REASON_DRIFT") from exc
        status = "rejected" if reason_code is not None else "completed"
        fold_evidence_hash = canonical_sha256(evidence_payload)
        run_identity = canonical_sha256(
            {
                "full_campaign_hash": plan.full_campaign_hash,
                "campaign_run_id": plan.campaign_run_id,
                "row_id": row_id,
                "experiment_id": experiment_by_row[row_id],
                "fold_evidence_hash": fold_evidence_hash,
            }
        )
        attempts.append(
            R3AttemptBatchItem(
                row_id=row_id,
                experiment_id=experiment_by_row[row_id],
                retry_index=0,
                status=status,
                reason_code=reason_code,
                fold_evidence_hash=fold_evidence_hash,
                run_identity=run_identity,
                evidence_payload=evidence_payload,
            )
        )
    return tuple(attempts)


def _attempt_accounting_rows(attempts: tuple[object, ...]) -> tuple[object, ...]:
    """Project the exact persistence attempts into the shared accounting DTO."""

    from rob974_h6a_accounting import AttemptAccountingRow

    return tuple(
        AttemptAccountingRow(
            row_id=item.row_id,
            experiment_id=item.experiment_id,
            retry_index=item.retry_index,
            status=item.status,
            reason_code=item.reason_code,
            fold_evidence_hash=item.fold_evidence_hash,
            run_identity=item.run_identity,
        )
        for item in attempts
    )


class _M4CampaignIssuer:
    """Bind one actual market-input authority to every M4 campaign receipt."""

    def __init__(
        self,
        *,
        api: SimpleNamespace,
        evidence_context: object,
        market_input_authority: object,
    ) -> None:
        self._api = api
        self._evidence_context = evidence_context
        self.market_input_authority = market_input_authority

    def issue_fold_scenario_attributions(
        self,
        scenario_sources: Sequence[tuple[str, object]],
    ) -> tuple[object, ...]:
        market_input_authority = self.market_input_authority
        return tuple(
            self._api.issue_r3_fold_scenario_attribution(
                path_scenario=scenario,
                source=source,
                market_input_authority=market_input_authority,
            )
            for scenario, source in scenario_sources
        )

    def issue_scorecard_accounting(self, attempts: tuple[object, ...]) -> object:
        return self._api.issue_r3_scorecard_accounting(
            evidence_context=self._evidence_context,
            attempts=attempts,
        )


class _M4ArtifactPort:
    """Narrow pure adapter accepted by the frozen directory-atomic primitive."""

    provenance = "actual_merged_h5"

    def __init__(self, *, api: SimpleNamespace, scorecard: dict, pair: object) -> None:
        self._api = api
        self._scorecard = scorecard
        self._pair = pair
        decoded = api.verify_r3_artifact_pair(
            json_bytes=pair.json_bytes,
            markdown_bytes=pair.markdown_bytes,
        )
        if decoded != scorecard:
            raise LaunchRefused("M4_ARTIFACT_PAIR_SCORECARD_SEMANTIC_DRIFT")

    def _require_scorecard(self, scorecard: Mapping[str, object]) -> None:
        if scorecard is not self._scorecard and scorecard != self._scorecard:
            raise LaunchRefused("M4_ARTIFACT_PORT_SCORECARD_DRIFT")

    def canonical_json_bytes(self, scorecard: Mapping[str, object]) -> bytes:
        self._require_scorecard(scorecard)
        observed = self._api.canonical_r3_json_bytes(scorecard)
        if observed != self._pair.json_bytes:
            raise LaunchRefused("M4_CANONICAL_JSON_DRIFT")
        return observed

    def semantic_hash(self, scorecard: Mapping[str, object]) -> str:
        canonical = self.canonical_json_bytes(scorecard)
        observed = self._api.hash_r3_canonical_bytes(canonical)
        if observed != self._pair.semantic_sha256:
            raise LaunchRefused("M4_SEMANTIC_HASH_DRIFT")
        return observed

    def render_markdown(self, scorecard: Mapping[str, object]) -> bytes:
        self._require_scorecard(scorecard)
        observed = self._api.build_r3_artifact_pair(scorecard).markdown_bytes
        if observed != self._pair.markdown_bytes:
            raise LaunchRefused("M4_MARKDOWN_RENDER_DRIFT")
        return observed


def _compute_actual_campaign(*, plan: object, input_data: object) -> ComputedCampaign:
    """Precompute all 12x2x8x3 evidence before opening a write transaction."""

    import rob974_h4_runner as h4_runner
    from rob974_r3_accounting import build_exact_12_accounting
    from rob974_r3_evidence_context import issue_r3_production_evidence_context
    from rob974_r3_gate_adapter import (
        build_production_gate_audit,
        build_production_gate_campaign_evidence,
        build_r3_s3_fold_source,
        build_r3_s4_fold_source,
    )
    from rob974_r3_manifest import R3S3Config, R3S4Config
    from rob974_r3_relaxation_h2_adapter import normalize_r3_phase_ledgers

    from app.services.rob974_h6b_materializer import _actual_execution_surface
    from app.services.rob974_r3_materializer import issue_r3_materialization_contract

    m4 = _load_m4_api()
    evidence_context = issue_r3_production_evidence_context(plan)
    market_input_authority = m4.issue_r3_market_input_authority(
        evidence_context=evidence_context,
        actual_h4_input_data=input_data,
    )
    m4_issuer = _M4CampaignIssuer(
        api=m4,
        evidence_context=evidence_context,
        market_input_authority=market_input_authority,
    )
    funding_sidecars = tuple(
        sidecar for _symbol, sidecar in input_data.funding_sidecars
    )
    if tuple(sidecar.symbol for sidecar in funding_sidecars) != SELECTED_SYMBOLS:
        raise LaunchRefused("PIT_FUNDING_SIDECAR_ORDER_DRIFT")

    gate_reports: dict[tuple[str, str], object] = {}
    primary_sources: dict[tuple[str, str], list[object]] = {
        (phase, config.config_id): []
        for phase in ("TRAIN", "OOS")
        for config in plan.manifest_rows
    }
    oos_fold_inputs: dict[str, list[object]] = {
        config.config_id: [] for config in plan.manifest_rows
    }
    cell_evidence: dict[str, list[dict[str, object]]] = {
        config.config_id: [] for config in plan.manifest_rows
    }
    engine_invocations = 0
    accepted_decision_units = 0
    basket_trades = 0

    for phase in ("TRAIN", "OOS"):
        phase_gate_sources: dict[str, list[object]] = {
            config.config_id: [] for config in plan.manifest_rows
        }
        for fold in plan.folds:
            surface = _actual_execution_surface(
                input_data,
                phase=h4_runner.phase_for_fold(
                    fold, "train" if phase == "TRAIN" else "selected_oos"
                ),
            )
            feature_context = surface.phase_context.feature_context
            for config in plan.manifest_rows:
                if type(config) is R3S3Config:
                    gate_source = build_r3_s3_fold_source(
                        feature_context=feature_context,
                        fold=fold,
                        phase=phase,
                        config=config,
                    )
                    generator_output = _generate_r3_s3(
                        source=gate_source, config=config
                    )
                elif type(config) is R3S4Config:
                    gate_source = build_r3_s4_fold_source(
                        feature_context=feature_context,
                        fold=fold,
                        phase=phase,
                        config=config,
                    )
                    generator_output = _generate_r3_s4(
                        feature_context=feature_context,
                        source=gate_source,
                        config=config,
                    )
                else:  # pragma: no cover - plan independently seals this
                    raise LaunchRefused("R3_MANIFEST_CONFIG_TYPE_DRIFT")
                phase_gate_sources[config.config_id].append(gate_source)
                scenario_sources, path_evidence = _execute_exact_three_paths(
                    config=config,
                    fold=fold,
                    phase=phase,
                    generator_output=generator_output,
                    input_data=input_data,
                    surface=surface,
                )
                engine_invocations += len(scenario_sources)
                accepted_decision_units += len(generator_output.accepted)
                primary = dict(scenario_sources)[PRIMARY_STRESS_SCENARIO]
                primary_sources[(phase, config.config_id)].append(primary)
                basket_trades += len(primary.terminal.result.trades)
                cell_evidence[config.config_id].append(
                    {
                        "phase": phase,
                        "fold_id": fold.fold_id,
                        "accepted_decision_units": len(generator_output.accepted),
                        "path_evidence": list(path_evidence),
                    }
                )
                if phase == "OOS":
                    receipts = m4_issuer.issue_fold_scenario_attributions(
                        scenario_sources
                    )
                    oos_fold_inputs[config.config_id].append(
                        m4.R3FoldOOSInput(scenario_attributions=receipts)
                    )
        for config in plan.manifest_rows:
            gate_reports[(config.config_id, phase)] = build_production_gate_audit(
                evidence_context=evidence_context,
                phase=phase,
                config=config,
                fold_sources=tuple(phase_gate_sources[config.config_id]),
            )

    if engine_invocations != 12 * 2 * 8 * 3:
        raise LaunchRefused("ACTUAL_ENGINE_INVOCATION_COUNT_NOT_576")
    ordered_reports = tuple(
        gate_reports[(config.config_id, phase)]
        for config in plan.manifest_rows
        for phase in ("TRAIN", "OOS")
    )
    gate_evidence = build_production_gate_campaign_evidence(
        evidence_context=evidence_context,
        reports=ordered_reports,
    )
    train_evidence = normalize_r3_phase_ledgers(
        phase="TRAIN",
        sources=tuple(
            source
            for config in plan.manifest_rows
            for source in primary_sources[("TRAIN", config.config_id)]
        ),
    )
    oos_evidence = normalize_r3_phase_ledgers(
        phase="OOS",
        sources=tuple(
            source
            for config in plan.manifest_rows
            for source in primary_sources[("OOS", config.config_id)]
        ),
    )
    relaxation_evidence = m4.issue_r3_scorecard_relaxation_evidence(
        evidence_context=evidence_context,
        oos_evidence=oos_evidence,
        train_evidence=train_evidence,
    )
    oos_ledger = m4.issue_r3_all_cell_oos_ledger(
        evidence_context=evidence_context,
        cells=tuple(
            m4.R3CellOOSInput(
                config_id=config.config_id,
                folds=tuple(oos_fold_inputs[config.config_id]),
            )
            for config in plan.manifest_rows
        ),
    )
    attempts = _build_attempts(
        plan=plan,
        cell_evidence=cell_evidence,
        gate_reports=gate_reports,
    )
    registration_specs = _registration_specs(plan)
    mapping = dict(plan.ordered_mapping)
    materialization_contract = issue_r3_materialization_contract(
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        s3_specs=registration_specs[0],
        s4_specs=registration_specs[1],
        row_id_to_experiment_id=mapping,
    )
    attempt_accounting_rows = _attempt_accounting_rows(attempts)
    accounting = build_exact_12_accounting(
        campaign_run_id=plan.campaign_run_id,
        ordered_mapping=plan.ordered_mapping,
        registered_total=12,
        attempts=attempt_accounting_rows,
    )
    scorecard_accounting = m4_issuer.issue_scorecard_accounting(attempts)
    if scorecard_accounting.report != accounting:
        raise LaunchRefused("M4_ISSUED_ACCOUNTING_REPORT_DRIFT")
    scorecard = m4.build_r3_scorecard(
        evidence_context=evidence_context,
        accounting=scorecard_accounting,
        oos_ledger=oos_ledger,
        gate_evidence=gate_evidence,
        relaxation_evidence=relaxation_evidence,
    )
    artifact_pair = m4.build_r3_artifact_pair(scorecard)
    if (
        m4.hash_r3_canonical_bytes(m4.canonical_r3_json_bytes(scorecard))
        != artifact_pair.semantic_sha256
    ):
        raise LaunchRefused("M4_SCORECARD_CANONICAL_HASH_DRIFT")
    m4_artifact_port = _M4ArtifactPort(api=m4, scorecard=scorecard, pair=artifact_pair)
    return ComputedCampaign(
        evidence_context=evidence_context,
        registration_specs=registration_specs,
        materialization_contract=materialization_contract,
        attempts=attempts,
        accounting=accounting,
        gate_evidence=gate_evidence,
        relaxation_evidence=relaxation_evidence,
        oos_ledger=oos_ledger,
        scorecard=scorecard,
        artifact_pair=artifact_pair,
        m4_artifact_port=m4_artifact_port,
        engine_invocations=engine_invocations,
        accepted_decision_units=accepted_decision_units,
        basket_trades=basket_trades,
    )


def _expected_registration_projection(
    *, plan: object, computed: ComputedCampaign
) -> dict[str, dict[str, object]]:
    from app.services.research_canonical_hash import (
        compute_identity_hashes,
        encode_manifest,
    )

    expected: dict[str, dict[str, object]] = {}
    specs = (*computed.registration_specs[0], *computed.registration_specs[1])
    for (row_id, experiment_id), spec in zip(plan.ordered_mapping, specs, strict=True):
        expected[row_id] = {
            "experiment_id": experiment_id,
            "strategy_key": spec.strategy_key,
            "strategy_version": spec.strategy_version,
            "hypothesis": spec.hypothesis,
            "supersedes_experiment_id": spec.supersedes_experiment_id,
            "manifest": encode_manifest(spec.components()),
            **compute_identity_hashes(spec.components()),
        }
    return expected


def _parse_persisted_attempt(
    *,
    raw_row: Mapping[str, object],
    plan: object,
    experiment_pk_by_row: Mapping[str, int],
) -> tuple[object | None, bool]:
    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem

    raw = raw_row.get("raw_payload")
    if not isinstance(raw, Mapping):
        return None, False
    retry_index = raw.get("retry_index")
    retry = type(retry_index) is int and retry_index != 0
    row_id = raw.get("row_id")
    if type(row_id) is not str or row_id not in dict(plan.ordered_mapping):
        return None, retry
    expected_experiment_id = dict(plan.ordered_mapping)[row_id]
    required_outer = (
        raw.get("full_campaign_hash") == plan.full_campaign_hash,
        raw.get("campaign_run_id") == plan.campaign_run_id,
        raw.get("exact_12_mapping_hash") == plan.exact_12_mapping_hash,
        raw.get("experiment_id") == expected_experiment_id,
        raw.get("retry_index") == 0,
        raw_row.get("strategy_experiment_id") == experiment_pk_by_row.get(row_id),
        raw_row.get("trial_status") == raw.get("status"),
        raw_row.get("runner") == R3_RUNNER,
        raw_row.get("trial_idempotency_key")
        == f"{plan.campaign_run_id}:{expected_experiment_id}:0",
    )
    if not all(required_outer) or not isinstance(raw.get("evidence_payload"), Mapping):
        return None, retry
    try:
        attempt = R3AttemptBatchItem(
            row_id=row_id,
            experiment_id=expected_experiment_id,
            retry_index=0,
            status=raw["status"],
            reason_code=raw.get("reason_code"),
            fold_evidence_hash=raw["fold_evidence_hash"],
            run_identity=raw["run_identity"],
            evidence_payload=raw["evidence_payload"],
        )
    except (KeyError, TypeError, ValueError):
        return None, retry
    if raw.get("r3_h6a_evidence_fingerprint") != attempt.fingerprint():
        return None, retry
    return attempt, retry


def _project_database_rows(
    *,
    plan: object,
    computed: ComputedCampaign,
    experiment_rows: Sequence[Mapping[str, object]],
    attempt_rows: Sequence[Mapping[str, object]],
    artifact_state: str,
    staging_artifacts: int,
    artifact_pair_valid: bool,
    artifact_semantic_match: bool,
) -> DatabaseProjection:
    from app.services.research_canonical_hash import canonical_ast_json
    from app.services.rob974_r3_materializer import R3PersistedSnapshot

    expected = _expected_registration_projection(plan=plan, computed=computed)
    row_id_by_experiment = {
        values["experiment_id"]: row_id for row_id, values in expected.items()
    }
    valid_experiments: dict[str, Mapping[str, object]] = {}
    mismatch_row_ids: list[str] = []
    out_of_plan_experiment_ids: list[str] = []
    foreign_rows = 0
    for row in experiment_rows:
        experiment_id = row.get("experiment_id")
        row_id = row_id_by_experiment.get(experiment_id)
        if row_id is None:
            foreign_rows += 1
            if type(experiment_id) is str:
                out_of_plan_experiment_ids.append(experiment_id)
            continue
        values = expected[row_id]
        matches = all(
            row.get(name) == value
            for name, value in values.items()
            if name != "manifest"
        )
        try:
            manifest_matches = canonical_ast_json(row.get("manifest")) == (
                canonical_ast_json(values["manifest"])
            )
        except (TypeError, ValueError):
            manifest_matches = False
        primary_key = row.get("id")
        if (
            not matches
            or not manifest_matches
            or type(primary_key) is not int
            or primary_key <= 0
            or row_id in valid_experiments
        ):
            mismatch_row_ids.append(row_id)
            foreign_rows += 1
            continue
        valid_experiments[row_id] = row

    experiment_pk_by_row = {
        row_id: row["id"] for row_id, row in valid_experiments.items()
    }
    valid_attempts: dict[str, object] = {}
    retry_rows = 0
    for row in attempt_rows:
        attempt, retry = _parse_persisted_attempt(
            raw_row=row,
            plan=plan,
            experiment_pk_by_row=experiment_pk_by_row,
        )
        retry_rows += int(retry)
        if attempt is None or attempt.row_id in valid_attempts:
            foreign_rows += 1
            continue
        valid_attempts[attempt.row_id] = attempt

    registered_mapping = tuple(
        (row_id, experiment_id)
        for row_id, experiment_id in plan.ordered_mapping
        if row_id in valid_experiments
    )
    attempts = tuple(
        valid_attempts[row_id] for row_id in _ROW_ORDER if row_id in valid_attempts
    )
    status_counts = tuple(
        (status, sum(item.status == status for item in attempts))
        for status in ("completed", "rejected", "crashed", "timeout")
    )
    database_state = "ABSENT" if not experiment_rows and not attempt_rows else "PRESENT"
    snapshot = R3PersistedSnapshot(
        campaign_run_id=plan.campaign_run_id,
        registered_mapping=registered_mapping,
        attempts=attempts,
        status_counts=status_counts,
        mismatch_row_ids=tuple(dict.fromkeys(mismatch_row_ids)),
        out_of_plan_experiment_ids=tuple(dict.fromkeys(out_of_plan_experiment_ids)),
    )
    return DatabaseProjection(
        state=PreflightState(
            database_state=database_state,
            artifact_state=artifact_state,
            registered_rows=len(registered_mapping),
            attempt_rows=len(attempts),
            foreign_rows=foreign_rows,
            retry_rows=retry_rows,
            stale_staging_rows=0,
            staging_artifacts=staging_artifacts,
            artifact_pair_valid=artifact_pair_valid,
            artifact_semantic_match=artifact_semantic_match,
        ),
        persisted_snapshot=snapshot,
    )


async def _fetch_database_rows(
    session: object,
) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    from sqlalchemy import text

    experiments_result = await session.execute(
        text(
            "SELECT id, experiment_id, strategy_key, strategy_version, "
            "hypothesis, supersedes_experiment_id, strategy_hash, code_hash, "
            "params_hash, dataset_manifest_hash, universe_hash, pit_hash, "
            "frozen_config_hash, policy_hash, benchmark_hash, cost_hash, "
            "mdd_hash, manifest FROM research.strategy_experiments ORDER BY id"
        )
    )
    attempts_result = await session.execute(
        text(
            "SELECT id, strategy_experiment_id, trial_idempotency_key, "
            "trial_status, runner, raw_payload "
            "FROM research.backtest_runs ORDER BY id"
        )
    )
    return (
        tuple(dict(row) for row in experiments_result.mappings().all()),
        tuple(dict(row) for row in attempts_result.mappings().all()),
    )


def _inspect_artifacts(
    *, artifacts: object, output_root: Path, computed: ComputedCampaign
) -> tuple[str, int, bool, bool]:
    presence = artifacts.probe(output_dir=output_root)
    state = presence.state
    if state == "PAIR_PRESENT":
        try:
            inspection = artifacts.inspect(
                scorecard=computed.scorecard,
                output_dir=output_root,
                h5_port=computed.m4_artifact_port,
            )
        except Exception:
            return "PAIR_PRESENT", 0, False, False
        exact = getattr(inspection, "disposition", None) == "EXACT_ARTIFACT_REPLAY"
        return "PAIR_PRESENT", 0, exact, exact
    return state, len(presence.staging_dirs), False, False


async def _read_only_campaign_state_session(
    *,
    session: object,
    plan: object,
    computed: ComputedCampaign,
    artifacts: object,
    output_root: Path,
) -> DatabaseProjection:
    from sqlalchemy import text

    try:
        await session.begin()
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await _fetch_and_validate_live_schema(session)
        experiment_rows, attempt_rows = await _fetch_database_rows(session)
        artifact = _inspect_artifacts(
            artifacts=artifacts,
            output_root=output_root,
            computed=computed,
        )
        return _project_database_rows(
            plan=plan,
            computed=computed,
            experiment_rows=experiment_rows,
            attempt_rows=attempt_rows,
            artifact_state=artifact[0],
            staging_artifacts=artifact[1],
            artifact_pair_valid=artifact[2],
            artifact_semantic_match=artifact[3],
        )
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()


def _coarse_preflight_disposition(state: PreflightState) -> str:
    if type(state) is not PreflightState:
        raise LaunchRefused("COARSE_PREFLIGHT_STATE_TYPE_MISMATCH")
    if (
        state.foreign_rows
        or state.retry_rows
        or state.stale_staging_rows
        or state.staging_artifacts
    ):
        raise LaunchRefused("COARSE_FOREIGN_RETRY_OR_STAGING_STATE_REFUSED")
    if (
        state.database_state == "ABSENT"
        and state.artifact_state == "ABSENT"
        and state.registered_rows == 0
        and state.attempt_rows == 0
    ):
        return "RUN_ONCE"
    if (
        state.database_state == "PRESENT"
        and state.artifact_state == "PAIR_PRESENT"
        and state.registered_rows == 12
        and state.attempt_rows == 12
    ):
        return "POTENTIAL_REPLAY"
    raise LaunchRefused("COARSE_DATABASE_ARTIFACT_ASYMMETRY_OR_PARTIAL")


async def _read_only_coarse_state_session(
    *,
    session: object,
    plan: object,
    registration_specs: tuple[tuple[object, ...], tuple[object, ...]],
    artifacts: object,
    output_root: Path,
) -> DatabaseProjection:
    """Refuse non-runnable facility shapes before corpus or engine work."""

    from sqlalchemy import text

    try:
        await session.begin()
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await _fetch_and_validate_live_schema(session)
        experiment_rows, attempt_rows = await _fetch_database_rows(session)
        presence = artifacts.probe(output_dir=output_root)
        return _project_database_rows(
            plan=plan,
            computed=SimpleNamespace(registration_specs=registration_specs),
            experiment_rows=experiment_rows,
            attempt_rows=attempt_rows,
            artifact_state=presence.state,
            staging_artifacts=len(presence.staging_dirs),
            artifact_pair_valid=False,
            artifact_semantic_match=False,
        )
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()


def _validate_exact_replay(
    *, projection: DatabaseProjection, computed: ComputedCampaign
) -> object:
    from app.services.rob974_r3_materializer import validate_r3_persisted_snapshot

    seal = validate_r3_persisted_snapshot(
        contract=computed.materialization_contract,
        snapshot=projection.persisted_snapshot,
        recomputed_attempts=computed.attempts,
    )
    expected_status_counts = tuple(computed.accounting.status_counts.items())
    if (
        seal.registered_total != 12
        or seal.primary_attempts != 12
        or seal.total_attempts != 12
        or seal.retry_attempts != 0
        or seal.status_counts != expected_status_counts
    ):
        raise LaunchRefused("PERSISTED_SNAPSHOT_ACCOUNTING_SEAL_DRIFT")
    return seal


def _publish_artifact_pair(
    *, artifacts: object, staged: object, h5_port: object
) -> object:
    return artifacts.publish(staged, h5_port=h5_port)


async def _coordinate_stage_commit_publish(
    *,
    session: object,
    persist: Callable[[], Any],
    stage: Callable[[], object],
    publish: Callable[[object], object],
) -> tuple[object, object, object]:
    """Own the deliberate stage→commit→publish asymmetry with no retry."""

    commit_confirmed = False
    try:
        persisted = await persist()
        staged = stage()
        await session.commit()
        commit_confirmed = True
    except BaseException as exc:
        if session.in_transaction():
            try:
                await session.rollback()
            except BaseException:
                pass
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        raise PrecommitCampaignFailure(
            "PRECOMMIT_FAILED_AUDIT_STATE_BEFORE_RETRY"
        ) from exc
    try:
        published = publish(staged)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        if not commit_confirmed:  # pragma: no cover - local invariant
            raise AssertionError("publication reached without confirmed commit")
        raise PostcommitPublishFailure(
            "POSTCOMMIT_PUBLISH_FAILED_RETRY_FORBIDDEN"
        ) from exc
    return persisted, staged, published


async def _materialize_run_once(
    *,
    engine: object,
    plan: object,
    computed: ComputedCampaign,
    artifacts: object,
    output_root: Path,
) -> tuple[object, object]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.research_db_write_guard import (
        ResearchDbPolicy,
        ResearchDbTarget,
    )
    from app.services.rob974_r3_h6a_bridge import (
        RECORD_R3_ATTEMPTS_OPERATION_KIND,
        REGISTER_R3_CAMPAIGN_OPERATION_KIND,
        R3ApprovedMutationContext,
        record_r3_attempts,
        register_r3_campaign,
    )
    from app.services.rob974_r3_materializer import (
        R3PersistedSnapshot,
        validate_r3_persisted_snapshot,
    )

    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await session.begin()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:campaign_run_id, 0))"),
            {"campaign_run_id": plan.campaign_run_id},
        )
        experiment_rows, attempt_rows = await _fetch_database_rows(session)
        artifact = _inspect_artifacts(
            artifacts=artifacts,
            output_root=output_root,
            computed=computed,
        )
        locked_projection = _project_database_rows(
            plan=plan,
            computed=computed,
            experiment_rows=experiment_rows,
            attempt_rows=attempt_rows,
            artifact_state=artifact[0],
            staging_artifacts=artifact[1],
            artifact_pair_valid=artifact[2],
            artifact_semantic_match=artifact[3],
        )
        if _preflight_disposition(locked_projection.state) != "RUN_ONCE":
            raise LaunchRefused("LOCKED_ONE_SHOT_STATE_IS_NOT_FRESH")

        mapping = dict(plan.ordered_mapping)
        policy = ResearchDbPolicy.of(
            ResearchDbTarget(host="localhost", database_name="rob974_r3_db")
        )
        common_approval = {
            "canonical_plan_hash": plan.full_campaign_hash,
            "derived_run_id": plan.campaign_run_id,
            "exact_12_mapping_hash": plan.exact_12_mapping_hash,
            "approval_token": ONE_SHOT_APPROVAL,
        }
        register_approval = R3ApprovedMutationContext(
            operation_kind=REGISTER_R3_CAMPAIGN_OPERATION_KIND,
            **common_approval,
        )
        attempt_approval = R3ApprovedMutationContext(
            operation_kind=RECORD_R3_ATTEMPTS_OPERATION_KIND,
            **common_approval,
        )

        async def persist() -> object:
            registered_s3, registered_s4 = await register_r3_campaign(
                session,
                approved=register_approval,
                full_campaign_hash=plan.full_campaign_hash,
                campaign_run_id=plan.campaign_run_id,
                s3_specs=computed.registration_specs[0],
                s4_specs=computed.registration_specs[1],
                row_id_to_experiment_id=mapping,
                guard_opt_in_enabled=True,
                guard_policy=policy,
            )
            registered = (*registered_s3, *registered_s4)
            if len(registered) != 12:
                raise LaunchRefused("REGISTERED_RESULT_NOT_EXACT_12")
            pk_mapping: dict[str, int] = {}
            for (row_id, _experiment_id), row in zip(
                plan.ordered_mapping, registered, strict=True
            ):
                primary_key = getattr(row, "id", None)
                if type(primary_key) is not int or primary_key <= 0:
                    raise LaunchRefused("REGISTERED_PRIMARY_KEY_MALFORMED")
                pk_mapping[row_id] = primary_key
            if len(set(pk_mapping.values())) != 12:
                raise LaunchRefused("REGISTERED_PRIMARY_KEYS_NOT_UNIQUE")
            stored = await record_r3_attempts(
                session,
                approved=attempt_approval,
                full_campaign_hash=plan.full_campaign_hash,
                campaign_run_id=plan.campaign_run_id,
                row_id_to_experiment_id=mapping,
                row_id_to_experiment_pk=pk_mapping,
                attempts=computed.attempts,
                strategy_name=R3_STRATEGY_NAME,
                timeframe=R3_TIMEFRAME,
                runner=R3_RUNNER,
                guard_opt_in_enabled=True,
                guard_policy=policy,
            )
            if len(stored) != 12:
                raise LaunchRefused("STORED_ATTEMPT_RESULT_NOT_EXACT_12")
            snapshot = R3PersistedSnapshot(
                campaign_run_id=plan.campaign_run_id,
                registered_mapping=plan.ordered_mapping,
                attempts=computed.attempts,
                status_counts=tuple(computed.accounting.status_counts.items()),
            )
            return validate_r3_persisted_snapshot(
                contract=computed.materialization_contract,
                snapshot=snapshot,
                recomputed_attempts=computed.attempts,
            )

        persisted, _staged, published = await _coordinate_stage_commit_publish(
            session=session,
            persist=persist,
            stage=lambda: artifacts.stage(
                scorecard=computed.scorecard,
                output_dir=output_root,
                h5_port=computed.m4_artifact_port,
            ),
            publish=lambda staged: _publish_artifact_pair(
                artifacts=artifacts,
                staged=staged,
                h5_port=computed.m4_artifact_port,
            ),
        )
        return persisted, published
    finally:
        if session.in_transaction():
            await session.rollback()
        await session.close()


async def _postcommit_read_only_audit(
    *,
    database_url: str,
    plan: object,
    computed: ComputedCampaign,
    artifacts: object,
    output_root: Path,
) -> object:
    """Reopen the committed state and require exact semantic replay."""

    from sqlalchemy.ext.asyncio import AsyncSession

    try:
        engine = _create_read_only_engine(database_url)
        try:
            projection = await _read_only_campaign_state_session(
                session=AsyncSession(bind=engine, expire_on_commit=False),
                plan=plan,
                computed=computed,
                artifacts=artifacts,
                output_root=output_root,
            )
            if _preflight_disposition(projection.state) != "REPLAY_NOOP":
                raise LaunchRefused("POSTCOMMIT_STATE_NOT_EXACT_REPLAY")
            return _validate_exact_replay(projection=projection, computed=computed)
        finally:
            await engine.dispose()
    except BaseException as exc:
        raise PostcommitAuditFailure(
            "POSTCOMMIT_READ_ONLY_AUDIT_FAILED_RETRY_FORBIDDEN"
        ) from exc


def _scorecard_result_sections(
    scorecard: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    operational = scorecard.get("operational")
    campaign_verdict = scorecard.get("campaign_verdict")
    if not isinstance(operational, Mapping) or not isinstance(
        campaign_verdict, Mapping
    ):
        raise LaunchRefused("SCORECARD_RESULT_SECTIONS_MISSING")
    status = operational.get("status")
    incomplete_reasons = operational.get("incomplete_reasons")
    verdict_status = campaign_verdict.get("operational_status")
    research_decision = campaign_verdict.get("research_decision")
    reason_codes = campaign_verdict.get("reason_codes")
    if (
        status not in ("COMPLETE", "INCOMPLETE")
        or verdict_status != status
        or type(incomplete_reasons) is not list
        or any(type(item) is not str for item in incomplete_reasons)
        or type(reason_codes) is not list
        or any(type(item) is not str for item in reason_codes)
        or (
            status == "INCOMPLETE"
            and (not incomplete_reasons or research_decision is not None)
        )
        or (
            status == "COMPLETE"
            and (
                incomplete_reasons
                or research_decision not in ("CONTINUE", "NARROW", "TERMINATE")
            )
        )
    ):
        raise LaunchRefused("SCORECARD_RESULT_SECTIONS_MALFORMED")
    return (
        {
            "status": status,
            "incomplete_reasons": list(incomplete_reasons),
        },
        {
            "operational_status": verdict_status,
            "research_eligible": status == "COMPLETE",
            "research_decision": research_decision,
            "reason_codes": list(reason_codes),
        },
    )


def _result_payload(
    *,
    disposition: str,
    plan: object,
    corpus_evidence: Mapping[str, object],
    computed: ComputedCampaign,
    output_root: Path,
) -> dict[str, object]:
    operational, campaign_verdict = _scorecard_result_sections(computed.scorecard)
    return {
        "schema_version": "rob974_r3_launcher_result.v1",
        "exit_code": 0,
        "disposition": disposition,
        "commit_confirmed": disposition == "MATERIALIZED",
        "retry_forbidden": True,
        "identity": {
            "full_campaign_hash": plan.full_campaign_hash,
            "campaign_run_id": plan.campaign_run_id,
            "exact_12_mapping_hash": plan.exact_12_mapping_hash,
        },
        "corpus": dict(corpus_evidence),
        "execution": {
            "configs": 12,
            "phases": 2,
            "folds": 8,
            "path_scenarios": list(PATH_SCENARIOS),
            "engine_invocations": computed.engine_invocations,
            "accepted_decision_units": computed.accepted_decision_units,
            "basket_trades": computed.basket_trades,
            "primary_relaxation_path": PRIMARY_STRESS_SCENARIO,
            "funding_gate_evaluated_once_before_paths": True,
        },
        "accounting": {
            "expected_total": computed.accounting.expected_total,
            "registered_total": computed.accounting.registered_total,
            "primary_attempts": computed.accounting.primary_attempts,
            "total_attempts": computed.accounting.total_attempts,
            "retry_attempts": computed.accounting.retry_attempts,
            "status_counts": dict(computed.accounting.status_counts),
            "accounting_complete": computed.accounting.accounting_complete,
            "performance_usable": computed.accounting.performance_usable,
            "trial_accounting_hash": computed.accounting.trial_accounting_hash,
        },
        "operational": operational,
        "campaign_verdict": campaign_verdict,
        "artifacts": {
            "directory": str(output_root),
            "json": str(output_root / "scorecard.json"),
            "markdown": str(output_root / "scorecard.md"),
            "semantic_sha256": computed.artifact_pair.semantic_sha256,
            "markdown_sha256": computed.artifact_pair.markdown_sha256,
        },
        "network_calls": 0,
        "broker_calls": 0,
        "order_calls": 0,
        "fill_calls": 0,
    }


async def _run_after_refreeze_preflight(
    arguments: argparse.Namespace,
    *,
    pins: FinalRefreezePins,
    environ: Mapping[str, str],
    stdout: TextIOBase,
    stderr: TextIOBase,
) -> int:
    manifest_path, corpus_root, output_root, database_url = _require_exact_static_gates(
        arguments, environ, pins=pins
    )
    _install_runtime_paths()
    plan = _build_candidate_production_plan()
    _require_plan_matches_refreeze(plan, pins)
    stderr.write("ROB974_R3_PREFLIGHT identity_git_paths_db=PASS\n")
    stderr.flush()

    from rob974_h6b_artifacts import DirectoryAtomicArtifactPort
    from sqlalchemy.ext.asyncio import AsyncSession

    artifacts = DirectoryAtomicArtifactPort()
    registration_specs = _registration_specs(plan)
    coarse_engine = _create_read_only_engine(database_url)
    try:
        coarse_projection = await _read_only_coarse_state_session(
            session=AsyncSession(bind=coarse_engine, expire_on_commit=False),
            plan=plan,
            registration_specs=registration_specs,
            artifacts=artifacts,
            output_root=output_root,
        )
    finally:
        await coarse_engine.dispose()
    coarse_disposition = _coarse_preflight_disposition(coarse_projection.state)
    stderr.write(
        "ROB974_R3_PREFLIGHT coarse_read_only_state="
        f"{coarse_projection.state.database_state}/"
        f"{coarse_projection.state.artifact_state} "
        f"{coarse_disposition}=PASS\n"
    )
    stderr.flush()

    input_data, corpus_evidence = _load_exact_real_input(manifest_path, corpus_root)
    stderr.write("ROB974_R3_PREFLIGHT frozen_corpus_pit_funding=PASS\n")
    stderr.flush()
    computed = _compute_actual_campaign(plan=plan, input_data=input_data)
    stderr.write("ROB974_R3_EMPIRICAL exact_12x2x8x3_precomputed_before_write=PASS\n")
    stderr.flush()

    read_engine = _create_read_only_engine(database_url)
    try:
        projection = await _read_only_campaign_state_session(
            session=AsyncSession(bind=read_engine, expire_on_commit=False),
            plan=plan,
            computed=computed,
            artifacts=artifacts,
            output_root=output_root,
        )
    finally:
        await read_engine.dispose()
    disposition = _preflight_disposition(projection.state)
    stderr.write(
        "ROB974_R3_PREFLIGHT read_only_state="
        f"{projection.state.database_state}/"
        f"{projection.state.artifact_state} {disposition}=PASS\n"
    )
    stderr.flush()

    if disposition == "REPLAY_NOOP":
        _validate_exact_replay(projection=projection, computed=computed)
        _write_json(
            stdout,
            _result_payload(
                disposition="REPLAY_NOOP",
                plan=plan,
                corpus_evidence=corpus_evidence,
                computed=computed,
                output_root=output_root,
            ),
        )
        return 0

    write_engine = _create_write_engine(database_url)
    try:
        await _materialize_run_once(
            engine=write_engine,
            plan=plan,
            computed=computed,
            artifacts=artifacts,
            output_root=output_root,
        )
    finally:
        await write_engine.dispose()
    await _postcommit_read_only_audit(
        database_url=database_url,
        plan=plan,
        computed=computed,
        artifacts=artifacts,
        output_root=output_root,
    )
    stderr.write(
        "ROB974_R3_POSTCOMMIT fresh_read_only_exact_replay_and_artifact_pair=PASS\n"
    )
    stderr.flush()
    _write_json(
        stdout,
        _result_payload(
            disposition="MATERIALIZED",
            plan=plan,
            corpus_evidence=corpus_evidence,
            computed=computed,
            output_root=output_root,
        ),
    )
    return 0


def _execute_refrozen_run(
    arguments: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    stdout: TextIOBase,
    stderr: TextIOBase,
) -> int:
    pins = _require_cp8_final_refreeze()
    return asyncio.run(
        _run_after_refreeze_preflight(
            arguments,
            pins=pins,
            environ=environ,
            stdout=stdout,
            stderr=stderr,
        )
    )


def run_cli(
    argv: Sequence[str],
    *,
    stdout: TextIOBase,
    stderr: TextIOBase,
    environ: Mapping[str, str] | None = None,
) -> int:
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR
    if not argv or tuple(argv) == (PLAN_ARGUMENT,):
        try:
            _write_json(stdout, _dry_run_payload())
            return 0
        except LaunchRefused as exc:
            stderr.write("AUTHORITY_OR_PREFLIGHT_REFUSED " + exc.reason_code + "\n")
            return AUTHORITY_OR_PREFLIGHT_REFUSED
    active_environ = os.environ if environ is None else environ
    if tuple(argv) == (SCHEMA_GUARD_ONLY_ARGUMENT,):
        try:
            return asyncio.run(
                _execute_schema_guard(environ=active_environ, stdout=stdout)
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
        return _execute_refrozen_run(
            arguments,
            environ=active_environ,
            stdout=stdout,
            stderr=stderr,
        )
    except PrecommitCampaignFailure:
        stderr.write(
            "PRECOMMIT_FAILURE commit_unconfirmed "
            "audit_database_and_staging_before_retry\n"
        )
        return PRECOMMIT_FAILURE
    except PostcommitPublishFailure:
        stderr.write(
            "POSTCOMMIT_PUBLISH_FAILURE commit_confirmed "
            "retry_forbidden_manual_artifact_forensics_required\n"
        )
        return POSTCOMMIT_PUBLISH_FAILURE
    except PostcommitAuditFailure:
        stderr.write(
            "POSTCOMMIT_AUDIT_FAILURE commit_and_publish_confirmed "
            "retry_forbidden_manual_database_artifact_forensics_required\n"
        )
        return POSTCOMMIT_AUDIT_FAILURE
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
