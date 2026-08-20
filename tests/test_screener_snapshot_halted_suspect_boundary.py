"""ROB-1309 hard invariant (ROB-1236 halted_suspect): screen_stocks_snapshot
and screen_stocks_enrich must not touch the halted_suspect data model at all.

`halted_suspect` semantics (halt detection, `meta.halted_suspect_excluded`
reporting, fail-open candle-history-lookup-failure handling) live exclusively
in the `screen_stocks`/`analyze_stock`/`discover_buy_candidates_fanout` path:
`app/mcp_server/tooling/screening/halt_filter.py`, consumed by
`screening/entrypoint.py`, `analysis_analyze.py`, and
`buy_candidate_fanout.py`. The ROB-1309 split touched a COMPLETELY SEPARATE
code path (`screener_snapshot_tool.py`/`screener_enrich_tool.py`, reading
`invest_screener_snapshots`/`invest_crypto_screener_snapshots` via
`build_screener_results`) and must never import or call into the halt-filter
module — this is a static, machine-checked guarantee rather than a prose
claim, per the ROB-1309 checkpoint review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Every module ROB-1236's halted_suspect semantics live in or are consumed
# from. screen_stocks_snapshot/screen_stocks_enrich must reach NONE of these,
# directly or transitively through their own module-level imports.
_HALT_SUSPECT_MODULES = frozenset(
    {
        "app.mcp_server.tooling.screening.halt_filter",
        "app.mcp_server.tooling.screening.entrypoint",
        "app.mcp_server.tooling.analysis_analyze",
        "app.mcp_server.tooling.buy_candidate_fanout",
        "app.services.halt_detection",
    }
)

_SCREENER_SPLIT_FILES = (
    ROOT / "app" / "mcp_server" / "tooling" / "screener_snapshot_tool.py",
    ROOT / "app" / "mcp_server" / "tooling" / "screener_enrich_tool.py",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.unit
@pytest.mark.parametrize("path", _SCREENER_SPLIT_FILES, ids=lambda p: p.name)
def test_screener_split_never_imports_halt_suspect_modules(path: Path) -> None:
    imported = _imported_modules(path)
    hits = {m for m in imported if m in _HALT_SUSPECT_MODULES}
    assert not hits, (
        f"{path.name} imports halted_suspect module(s) {hits} — "
        "screen_stocks_snapshot/screen_stocks_enrich must stay on the "
        "invest_screener_snapshots/build_screener_results path and never "
        "reach the screen_stocks/analyze_stock halt_filter path (ROB-1236 "
        "hard invariant, ROB-1309 scope boundary)."
    )


@pytest.mark.unit
def test_screener_split_row_schema_has_no_halted_field() -> None:
    """screen_stocks_snapshot's row schema never carried a halted/
    halted_suspect field — confirming ROB-1236 semantics were never part of
    this tool's data model in the first place (not something this PR had to
    preserve because it was never present)."""
    schema_path = ROOT / "app" / "schemas" / "invest_screener.py"
    text = schema_path.read_text(encoding="utf-8")
    assert "halted" not in text.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_screen_stocks_snapshot_response_has_no_halted_suspect_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call-level confirmation (not just static import absence): a real
    screen_stocks_snapshot_impl call never emits halted_suspect/
    halted_suspect_excluded keys anywhere in its response."""
    from app.mcp_server.tooling import screener_snapshot_tool as tool

    class _FakeCM:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"symbol": "005930", "market": "kr"}],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: object) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr"
    )

    def _walk_keys(obj: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(str(k))
                keys |= _walk_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                keys |= _walk_keys(item)
        return keys

    all_keys = {k.lower() for k in _walk_keys(out)}
    assert not any("halted" in k for k in all_keys)
