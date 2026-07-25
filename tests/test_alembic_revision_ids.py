from __future__ import annotations

import ast
from pathlib import Path


def _literal_assignment(tree: ast.AST, name: str):
    for node in getattr(tree, "body", []):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return ast.literal_eval(node.value)
    raise AssertionError(f"missing {name!r} assignment")


def _as_revision_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def test_alembic_revision_ids_fit_default_version_table() -> None:
    # Alembic creates `alembic_version.version_num` as VARCHAR(32) by default.
    # Production deploys run migrations before any application code can widen it,
    # so every stored revision id must remain <= 32 chars.
    too_long: list[tuple[str, int, str]] = []
    unknown_references: list[tuple[str, str]] = []
    migrations: dict[str, tuple[Path, list[str]]] = {}

    for migration in sorted(Path("alembic/versions").glob("*.py")):
        tree = ast.parse(migration.read_text())
        revision = _literal_assignment(tree, "revision")
        down_revisions = _as_revision_list(_literal_assignment(tree, "down_revision"))
        migrations[revision] = (migration, down_revisions)

    known_revisions = set(migrations)
    for revision, (migration, down_revisions) in migrations.items():
        if len(revision) > 32:
            too_long.append((migration.name, len(revision), revision))
        for down_revision in down_revisions:
            if len(down_revision) > 32:
                too_long.append((migration.name, len(down_revision), down_revision))
            if down_revision not in known_revisions:
                unknown_references.append((migration.name, down_revision))

    assert not too_long
    assert not unknown_references
