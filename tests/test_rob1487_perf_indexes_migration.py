"""ROB-1487: the two ``max()`` partition probes must be index-supported.

Pins the migration that adds the indexes, the single-head chain, and the ORM
metadata parity (so a future autogenerate cannot silently drop them).
"""

from __future__ import annotations

import ast
import pathlib

from app.models.invest_momentum_event_snapshot import InvestMomentumEventSnapshot
from app.models.invest_screener_snapshot import InvestScreenerSnapshot

_REPO = pathlib.Path(__file__).resolve().parents[1]
_VERSIONS = _REPO / "alembic" / "versions"

_MOMENTUM_INDEX = "ix_invest_momentum_event_snapshots_market_snapshot_at"
_SCREENER_INDEX = "ix_invest_screener_snapshots_market_support_date"
_NEW_REVISION = "20261103_rob1487_perf_idx"
_PREVIOUS_HEAD = "20261102_rob1474_mock_orders"


def _assigned_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Tuple | ast.List):
        return [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    return []


def _module_constants(tree: ast.Module) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0], node.value
        if target is None or value is None:
            continue
        if target.id in {"revision", "down_revision"}:
            values[target.id] = _assigned_strings(value)
    return values


def _chain() -> tuple[dict[str, list[str]], set[str]]:
    revisions: dict[str, list[str]] = {}
    for path in sorted(_VERSIONS.glob("*.py")):
        constants = _module_constants(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        for revision in constants.get("revision", []):
            revisions[revision] = constants.get("down_revision", [])
    parents = {down for downs in revisions.values() for down in downs}
    heads = set(revisions) - parents
    return revisions, heads


def test_migration_chain_has_a_single_head_and_it_is_this_revision() -> None:
    revisions, heads = _chain()

    assert heads == {_NEW_REVISION}
    assert revisions[_NEW_REVISION] == [_PREVIOUS_HEAD]


def test_momentum_snapshot_model_declares_the_market_snapshot_index() -> None:
    index = next(
        (
            candidate
            for candidate in InvestMomentumEventSnapshot.__table__.indexes
            if candidate.name == _MOMENTUM_INDEX
        ),
        None,
    )

    assert index is not None
    assert [column.name for column in index.columns] == ["market", "snapshot_at"]


def test_screener_model_declares_the_partial_support_index() -> None:
    index = next(
        (
            candidate
            for candidate in InvestScreenerSnapshot.__table__.indexes
            if candidate.name == _SCREENER_INDEX
        ),
        None,
    )

    assert index is not None
    assert [column.name for column in index.columns] == ["market", "snapshot_date"]
    assert "support_computed_at IS NOT NULL" in str(
        index.dialect_options["postgresql"]["where"]
    )


def test_migration_creates_both_indexes_idempotently() -> None:
    text = (_VERSIONS / "20261103_rob1487_perf_indexes.py").read_text(encoding="utf-8")

    assert text.count("CREATE INDEX IF NOT EXISTS") == 2
    assert _MOMENTUM_INDEX in text
    assert _SCREENER_INDEX in text
    assert "WHERE support_computed_at IS NOT NULL" in text
