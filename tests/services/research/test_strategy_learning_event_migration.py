from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    REPO_ROOT / "alembic" / "versions" / "20260728_rob1115_strategy_learning_events.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("rob1115_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_single_chain_and_has_up_down_entrypoints() -> None:
    module = _module()
    assert module.revision == "20260728_rob1115_learning"
    assert module.down_revision == "20260728_rob1103_watch_links"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_contains_fk_checks_uniques_and_full_append_only_fence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    expected_checks = {
        "ck_strategy_learning_event_hashes",
        "ck_strategy_learning_event_experiment_id",
        "ck_strategy_learning_event_stage",
        "ck_strategy_learning_event_verdict",
        "ck_strategy_learning_event_failure_class",
        "ck_strategy_learning_event_reason_codes",
        "ck_strategy_learning_event_evidence_refs",
        "ck_strategy_learning_event_failure_fingerprint",
        "ck_strategy_learning_event_learning_payload",
        "ck_strategy_learning_event_nonblank_audit",
    }
    assert "research.strategy_experiments.experiment_id" in source
    assert 'sa.Column("experiment_id", sa.String(64), nullable=True)' in source
    assert "uq_strategy_learning_event_memory_event_id" in source
    assert "uq_strategy_learning_event_idempotency_key" in source
    for name in expected_checks:
        assert f'name=op.f("{name}")' in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source
