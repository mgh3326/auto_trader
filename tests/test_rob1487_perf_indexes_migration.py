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


def test_migration_chain_has_a_single_head_and_this_revision_is_on_it() -> None:
    revisions, heads = _chain()

    # One head, and this revision is an ancestor of it. Later migrations move
    # the head, so the head itself is not pinned here.
    assert len(heads) == 1, heads
    assert revisions[_NEW_REVISION] == [_PREVIOUS_HEAD]
    (head,) = heads
    seen: set[str] = set()
    stack = [head]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(revisions.get(current, []))
    assert _NEW_REVISION in seen


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


def test_head_replay_fixtures_exclude_account_value_tables():
    """Current metadata tables must be dropped after create_all before head replay."""
    root = pathlib.Path(__file__).resolve().parent / "services"
    fixtures = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "metadata.create_all" not in source or '"upgrade", "head"' not in source:
            continue
        fixtures.append(path)
        tree = ast.parse(source)
        post_parent_tables = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_POST_PARENT_TABLES"
                and node.value is not None
            ):
                post_parent_tables.update(_assigned_strings(node.value))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(child, ast.Attribute) and child.attr == "create_all"
                for child in ast.walk(node)
            )
        ]
        assert len(functions) == 1, path
        function = functions[0]
        create = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute) and node.attr == "create_all"
        )
        boundaries = [
            node.lineno
            for node in ast.walk(function)
            if (
                isinstance(node, (ast.Yield, ast.YieldFrom))
                or (isinstance(node, ast.Constant) and node.value == "stamp")
            )
            and node.lineno > create.lineno
        ]
        assert boundaries, path
        boundary = min(boundaries)
        awaited = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "execute"
            and create.lineno < node.lineno < boundary
        ]
        direct = {
            child.value
            for node in awaited
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        loop_drops = False
        for loop in ast.walk(function):
            if (
                not isinstance(loop, (ast.For, ast.AsyncFor))
                or loop.lineno <= create.lineno
                or not isinstance(loop.iter, ast.Name)
                or loop.iter.id != "_POST_PARENT_TABLES"
                or not isinstance(loop.target, ast.Name)
            ):
                continue
            for node in awaited:
                if node not in set(ast.walk(loop)):
                    continue
                for child in ast.walk(node):
                    if not isinstance(child, ast.JoinedStr):
                        continue
                    prefix = "".join(
                        v.value
                        for v in child.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                    names = {
                        v.value.id
                        for v in child.values
                        if isinstance(v, ast.FormattedValue)
                        and isinstance(v.value, ast.Name)
                    }
                    if "DROP TABLE" in prefix and loop.target.id in names:
                        loop_drops = True
        assert (
            "ALTER TABLE review.live_order_ledger DROP COLUMN last_reconcile_detail"
            in direct
        ), path
        for table in (
            "review.kis_us_inference_expiry_events",
            "review.account_value_snapshots",
            "review.account_external_flows",
        ):
            assert f"DROP TABLE {table}" in direct or (
                loop_drops and table in post_parent_tables
            ), (path, table)
    assert len(fixtures) >= 3
