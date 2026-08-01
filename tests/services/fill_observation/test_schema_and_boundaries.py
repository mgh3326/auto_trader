from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.fill_observation import (
    FillObservation,
    FillProjectionCursor,
    FillProjectionOutbox,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260801_rob1195_fill_observation_foundation.py"
SERVICE_ROOT = REPO / "app/services/fill_observation"
SCHEMA_BOOTSTRAP = REPO / "tests/_schema_bootstrap.py"


def _constraint_names(model: type[object]) -> set[str]:
    return {
        str(constraint.name)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if constraint.name is not None
    }


def test_models_define_observation_outbox_and_cursor_uniqueness() -> None:
    observation_constraints = _constraint_names(FillObservation)
    outbox_constraints = _constraint_names(FillProjectionOutbox)

    assert "uq_fill_observation_identity" in observation_constraints
    assert "uq_fill_projection_outbox_delivery_key" in outbox_constraints
    assert "uq_fill_projection_outbox_observation" in outbox_constraints
    assert [column.name for column in FillProjectionCursor.__table__.primary_key] == [
        "projection_name",
        "partition_key",
    ]
    assert FillObservation.__table__.schema == "review"
    assert FillProjectionOutbox.__table__.schema == "review"
    assert FillProjectionCursor.__table__.schema == "review"


def test_observation_has_no_mutable_updated_at_column() -> None:
    assert "updated_at" not in FillObservation.__table__.columns
    assert "created_at" in FillObservation.__table__.columns
    assert "fill_delta_quantity" in FillObservation.__table__.columns
    assert "cumulative_quantity" in FillObservation.__table__.columns


def test_migration_is_single_head_and_has_no_data_movement() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260801_rob1195_fillobs"' in source
    assert (
        'down_revision: str | Sequence[str] | None = "20260728_rob1109_watch_intent"'
    ) in source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260801_rob1195_fillobs"
    ]
    assert "op.bulk_insert" not in source
    assert "INSERT INTO" not in source
    assert "UPDATE review." not in source
    assert "DELETE FROM" not in source


def test_migration_enforces_append_only_and_non_destructive_rollback() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    bootstrap = SCHEMA_BOOTSTRAP.read_text(encoding="utf-8")
    assert "reject_fill_observation_mutation" in source
    assert "BEFORE UPDATE OR DELETE ON review.fill_observations" in source
    assert "BEFORE TRUNCATE ON review.fill_observations" in source
    assert "cannot downgrade: review.fill_observations contains" in source
    assert "SCHEMA_BOOTSTRAP_VERSION = 34" in bootstrap
    assert "reject_fill_observation_mutation" in bootstrap


def test_migration_contains_durable_retry_and_cursor_fields() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "fill_projection_outbox",
        "fill_projection_cursors",
        "uq_fill_projection_outbox_observation",
        "uq_fill_projection_outbox_delivery_key",
        "attempt_count",
        "available_at",
        "lease_token",
        "lease_expires_at",
        "last_error",
        "last_fill_observation_id",
        "last_observation_identity",
    ):
        assert required in source


def test_fill_service_has_no_broker_mcp_scheduler_or_llm_imports() -> None:
    forbidden_prefixes = (
        "app.mcp_server",
        "app.tasks",
        "app.jobs",
        "app.services.brokers",
        "openai",
        "google.generativeai",
        "anthropic",
        "xai",
    )
    violations: list[str] = []
    for path in SERVICE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module: str | None = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{path.relative_to(REPO)}:{alias.name}")
            if module is not None and module.startswith(forbidden_prefixes):
                violations.append(f"{path.relative_to(REPO)}:{module}")
    assert violations == []


def test_lock_keys_are_stable_hash_bytes_and_transactions_have_no_inner_commit() -> (
    None
):
    identity_source = (SERVICE_ROOT / "identity.py").read_text(encoding="utf-8")
    repository_source = (SERVICE_ROOT / "repository.py").read_text(encoding="utf-8")
    writer_source = (SERVICE_ROOT / "writer.py").read_text(encoding="utf-8")

    assert "int.from_bytes" in identity_source
    assert "signed=True" in identity_source
    assert "hash(" not in identity_source
    assert "pg_advisory_xact_lock(:lock_key)" in repository_source
    assert ".commit(" not in repository_source
    assert "async with session.begin()" in writer_source


def test_projection_claim_blocks_unfinished_partition_predecessors() -> None:
    repository_source = (SERVICE_ROOT / "repository.py").read_text(encoding="utf-8")

    assert "unfinished_predecessor" in repository_source
    assert 'predecessor.state != "succeeded"' in repository_source
    assert ".where(~unfinished_predecessor)" in repository_source


def test_new_models_are_constructed_only_inside_service_repository() -> None:
    model_names = {
        "FillObservation",
        "FillProjectionCursor",
        "FillProjectionOutbox",
    }
    allowed = Path("app/services/fill_observation/repository.py")
    violations: list[str] = []
    for root in (REPO / "app", REPO / "scripts"):
        for path in root.rglob("*.py"):
            relative = path.relative_to(REPO)
            if relative == allowed or relative == Path(
                "app/models/fill_observation.py"
            ):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in model_names
                ):
                    violations.append(f"{relative}:{node.lineno}:{node.func.id}")
    assert violations == []


def test_no_existing_consumer_imports_the_new_writer() -> None:
    allowed_root = Path("app/services/fill_observation")
    violations: list[str] = []
    for path in (REPO / "app").rglob("*.py"):
        relative = path.relative_to(REPO)
        if allowed_root in relative.parents or relative == Path(
            "app/models/__init__.py"
        ):
            continue
        source = path.read_text(encoding="utf-8")
        if "app.services.fill_observation" in source:
            violations.append(str(relative))
    assert violations == []
