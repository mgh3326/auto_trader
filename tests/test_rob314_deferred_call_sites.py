"""ROB-314 — lock the deferred bundle-ensure call sites on the empty default.

The report-generation entrypoints (MCP prepare_bundle, HTTP prepare-bundle)
inject ``production_collector_registry``.
These two call sites intentionally do NOT — the refresh flow belongs to the
separate scheduler-activation track and the generic ensure tool is a manual
primitive. If a future change wires production collectors here, it must be a
deliberate decision that updates this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFERRED_FILES = [
    _REPO_ROOT / "app" / "flows" / "investment_snapshots_refresh_flow.py",
    _REPO_ROOT / "app" / "mcp_server" / "tooling" / "investment_snapshots_tools.py",
]


@pytest.mark.parametrize("path", _DEFERRED_FILES, ids=lambda p: p.name)
def test_deferred_call_site_does_not_wire_production_collectors(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import production_collector_registry" not in text, (
        f"{path.name} imports production_collector_registry. If this is "
        "intentional, update ROB-314 scope and this guard."
    )
    assert "production_collector_registry(" not in text, (
        f"{path.name} invokes production_collector_registry. If this is "
        "intentional, update ROB-314 scope and this guard."
    )
    assert "ROB-314" in text, (
        f"{path.name} must carry a ROB-314 decision marker explaining why it "
        "stays on the empty default collector registry."
    )
