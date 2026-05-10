"""Static-source guard: SPA must not call ForexFactory directly (ROB-184)."""

from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parents[1] / "frontend" / "invest" / "src"


def _all_source_files() -> list[Path]:
    exts = {".ts", ".tsx", ".js", ".jsx"}
    return [p for p in FRONTEND_SRC.rglob("*") if p.suffix in exts]


def test_frontend_does_not_reference_forexfactory_host():
    for path in _all_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "nfs.faireconomy.media" not in text, path
        assert "ff_calendar_thisweek" not in text, path
        assert "ff_calendar_nextweek" not in text, path
