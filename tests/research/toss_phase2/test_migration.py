from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20260804_toss_phase2_corpus.py"

spec = importlib.util.spec_from_file_location("toss_phase2_migration", MIGRATION_PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_toss_corpus_is_separate_and_venue_free() -> None:
    source = MIGRATION_PATH.read_text()

    assert migration.down_revision == "20260804_alpaca_clean_account"
    assert migration.TABLE_NAME == "kr_candles_1m_toss"
    assert len(migration.CAGG_SPECS) == 4
    assert "venue TEXT" not in source
    assert "UNIQUE (time_utc, symbol)" in source
    assert "session_segment TEXT NOT NULL" in source
    assert "UNKNOWN" not in migration.SESSION_SEGMENTS
    assert "CHECK (source = 'TOSS')" in source


def test_toss_migration_is_additive_and_least_privilege() -> None:
    source = MIGRATION_PATH.read_text()

    assert "ALTER DEFAULT PRIVILEGES" not in source
    assert "GRANT SELECT, INSERT ON TABLE research.{TABLE_NAME}" in source
    assert "GRANT ALL" not in source
    assert "DROP TABLE IF EXISTS research.{TABLE_NAME}" in source
    assert "pre_nxt BOOLEAN" in source
    assert "BOOL_AND(is_padding) AS is_padding" in source
    assert "no cagg refresh policy is installed" in source
    assert "COMMENT ON VIEW research.{view_name}" in source
    assert "COMMENT ON MATERIALIZED VIEW research.{view_name}" in source
    assert "DROP VIEW research.{view_name}" in source
    assert "DROP MATERIALIZED VIEW research.{view_name}" in source


def test_toss_migration_history_merges_the_newer_main_head() -> None:
    merge_path = (
        REPO
        / "alembic"
        / "versions"
        / "20260805_merge_toss_phase2_and_kis_mock_runner_heads.py"
    )
    source = merge_path.read_text()

    assert 'revision: str = "20260805_merge_toss_phase2_kis_mock_runner"' in source
    assert '"20260804_toss_phase2"' in source
    assert '"20260805_kis_mock_runner"' in source
    assert "op." not in source
