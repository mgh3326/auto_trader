"""Was 2023-2024 KR already read? Answers from the repository itself.

Each row is a place in the repository whose own text says a KR experiment or the
corpus design used that period. One hit is enough: the period cannot be proved
untouched, so it is labelled diagnostics only and never called a sealed test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Probe:
    path: str
    pattern: str
    meaning: str


PROBES: tuple[Probe, ...] = (
    Probe(
        "research/kr_corpus/config.py",
        r'validation: str = "2023-01-01\.\.2024-12-31"',
        "kr-corpus-v1 assigns 2023-2024 to its validation window",
    ),
    Probe(
        "research/kr_corpus/backtest/windows.py",
        r'EXPLORATION_WINDOW = ClosedWindow\("2015-01-01", "2024-12-31"\)',
        "the corpus backtest treats 2015-2024 as one exploration window",
    ),
    Probe(
        "research/underwater_spike_trim_study/README.md",
        r"\| kr \| `kr-corpus-v1` run `20260803-1001` \| 2015-01-01 → 2024-12-31",
        "the underwater spike trim study read KR through 2024-12-31",
    ),
    Probe(
        "research/kr_corpus/d3_engine/calibration_run.py",
        r"2015-2024 exploration corpus",
        "D3 calibration loaded 2015-2024 as warm-up",
    ),
    Probe(
        "research/kr_corpus/d3_engine/primary_corpus.py",
        r"year < 2015 or year > 2024",
        "the D3 primary run reads corpus years 2015 through 2024",
    ),
)


@dataclass(frozen=True, slots=True)
class Hit:
    path: str
    line: int
    meaning: str


def run_audit(root: Path) -> tuple[Hit, ...]:
    """Return the probes that matched. A missing file is not evidence of absence."""

    hits: list[Hit] = []
    for probe in PROBES:
        path = root / probe.path
        if not path.is_file():
            continue
        for number, text in enumerate(path.read_text().splitlines(), start=1):
            if re.search(probe.pattern, text):
                hits.append(Hit(probe.path, number, probe.meaning))
                break
    return tuple(hits)


def provably_untouched(hits: tuple[Hit, ...]) -> bool:
    return not hits
