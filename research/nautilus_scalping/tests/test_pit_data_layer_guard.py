# tests/test_pit_data_layer_guard.py
import ast
from pathlib import Path

import artifact_paths

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = [
    "pit_klines_fetcher.py",
    "pit_bars.py",
    "build_pit_universe.py",
    "pit_universe.py",
    "campaign_specs.py",
    "campaign_controls.py",
    "run_rob353_campaign.py",
    "rob941_frozen_scope.py",
    "rob941_archive_fetch.py",
    "rob941_kline_schema.py",
    "rob941_gaps.py",
    "rob941_funding_sidecar.py",
    "rob941_manifest.py",
    "rob941_corpus_builder.py",
    "rob941_persistence.py",
    "rob941_offline_loader.py",
    "build_rob941_corpus.py",
    "rob946_campaign_identity.py",
]


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_app_imports_in_data_layer():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            assert not name.startswith("app"), (
                f"{mod} imports forbidden app module {name!r}"
            )


def test_pit_data_root_is_gitignored(monkeypatch):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)
    root = artifact_paths.pit_data_root()
    gitignore = (_ROOT / ".gitignore").read_text()
    assert "data/" in gitignore
    assert root.name == "data"
