"""hk 5753 — no production code path enqueues to TaskIQ.

The TaskIQ worker is retired: its schedules moved to Dagu or were retired on
2026-11-17, and the pi deploy no longer runs ``at-worker``. A ``.kiq(`` or
``.kicker(`` call in ``app/`` or ``scripts/`` would hand a message to a broker
that nothing drains, which is the silent-loss failure this guard prevents.

The task *definitions* stay (``@broker.task``) so an operator can invoke a
body deliberately; only dispatch is banned here. Pure static scan: no
database, no network, no broker import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts")
FORBIDDEN_DISPATCH = re.compile(r"\.(kiq|kicker)\(")


def _dispatch_sites() -> list[str]:
    sites: list[str] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if FORBIDDEN_DISPATCH.search(line):
                    sites.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return sites


@pytest.mark.unit
def test_no_production_code_dispatches_to_taskiq() -> None:
    assert _dispatch_sites() == [], (
        "TaskIQ dispatch is retired (hk 5753). Remove the .kiq()/.kicker() call "
        "or make the work an explicit scripts/ one-shot."
    )


@pytest.mark.unit
def test_the_scan_actually_sees_python_under_app() -> None:
    """Guard against the scan silently matching nothing (wrong root)."""
    files = list((REPO_ROOT / "app").rglob("*.py"))
    assert len(files) > 100, len(files)
    probe = re.compile(r"\.kiq\(")
    assert not probe.search("no dispatch here")
    assert probe.search("await task.kiq(x)")
