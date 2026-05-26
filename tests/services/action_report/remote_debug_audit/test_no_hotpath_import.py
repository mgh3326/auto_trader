import ast
import pathlib

_HOTPATH = [
    "app/services/action_report/snapshot_backed/generator.py",
    "app/services/action_report/snapshot_backed/collectors/registry.py",
    "app/services/action_report/snapshot_backed/collectors/optional_stubs.py",
]


def test_hotpath_does_not_import_remote_debug_audit() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    for rel in _HOTPATH:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any("remote_debug_audit" in m for m in imported), (
            f"{rel} must not import remote_debug_audit (operator-only)"
        )
