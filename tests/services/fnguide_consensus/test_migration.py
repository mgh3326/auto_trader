"""hk 6508: the migration revision must fit alembic_version.version_num (VARCHAR(32))."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20261125_hk6508_fnguide.py"
)
ALEMBIC_VERSION_COLUMN_LENGTH = 32


def _module_strings() -> dict[str, str]:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                found[node.target.id] = node.value.value
    return found


def test_revision_fits_alembic_version_column() -> None:
    revision = _module_strings()["revision"]
    assert len(revision) <= ALEMBIC_VERSION_COLUMN_LENGTH, revision


def test_file_name_matches_revision() -> None:
    assert MIGRATION.stem == _module_strings()["revision"]


def test_revises_the_current_head() -> None:
    assert _module_strings()["down_revision"] == "20261125_hk6503_kr_trading_flags"
