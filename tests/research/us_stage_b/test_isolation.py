from __future__ import annotations

import ast
from pathlib import Path

from research.us_stage_b.source import USStageBDailyBar

MODULE_ROOT = Path(__file__).parents[3] / "research" / "us_stage_b"


def test_us_engine_is_additive_and_does_not_import_legacy_signal_surfaces() -> None:
    forbidden_prefixes = (
        "app",
        "research.crypto_stage_b",
        "research.kr_corpus",
        "research.three_market_shadow",
        "research.us_corpus",
    )
    offenders: list[str] = []
    for path in sorted(MODULE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith(forbidden_prefixes) for name in names):
                offenders.append(f"{path.name}:{node.lineno}:{names!r}")
    assert offenders == []


def test_us_stage_b_input_exposes_only_the_frozen_candidate_fields() -> None:
    assert tuple(USStageBDailyBar.__dataclass_fields__) == (
        "symbol",
        "session_date",
        "open",
        "adjusted_close",
        "volume",
    )
