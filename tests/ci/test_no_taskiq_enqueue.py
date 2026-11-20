"""hk 5753 — no production code path enqueues to TaskIQ.

The TaskIQ worker is retired: its schedules moved to Dagu or were retired on
2026-11-17, and the pi deploy no longer runs ``at-worker``. A ``.kiq(`` or
``.kicker(`` *call* in ``app/`` or ``scripts/`` would hand a message to a broker
that nothing drains, which is the silent-loss failure this guard prevents.

The task *definitions* stay (``@broker.task``) so an operator can invoke a
body deliberately; only dispatch is banned here. The scan walks the AST and
matches call nodes whose callee is an attribute named ``kiq`` or ``kicker``, so
whitespace, line breaks, and comments cannot hide or fake a dispatch. Pure
static scan: no database, no network, no broker import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts")
FORBIDDEN_DISPATCH_ATTRIBUTES = frozenset({"kiq", "kicker"})


def _dispatch_sites(roots: tuple[Path, ...]) -> list[str]:
    sites: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if (
                    isinstance(callee, ast.Attribute)
                    and callee.attr in FORBIDDEN_DISPATCH_ATTRIBUTES
                ):
                    rel = (
                        path.relative_to(REPO_ROOT)
                        if path.is_relative_to(REPO_ROOT)
                        else path
                    )
                    sites.append(f"{rel}:{node.lineno}")
    return sites


@pytest.mark.unit
def test_no_production_code_dispatches_to_taskiq() -> None:
    roots = tuple(REPO_ROOT / name for name in SCAN_ROOTS)
    assert _dispatch_sites(roots) == [], (
        "TaskIQ dispatch is retired (hk 5753). Remove the .kiq()/.kicker() call "
        "or make the work an explicit scripts/ one-shot."
    )


@pytest.mark.unit
def test_the_scanner_catches_a_spaced_call_and_ignores_comments_and_strings(
    tmp_path: Path,
) -> None:
    """Drive the real scanner on sample sources, not on a regex copy of it."""
    (tmp_path / "spaced.py").write_text(
        "async def f(task, job_id):\n    await task.kiq (job_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "kicker.py").write_text(
        "async def g(task):\n    await task.kicker().with_task_id('x').kiq()\n",
        encoding="utf-8",
    )
    (tmp_path / "comment_only.py").write_text(
        "# task.kiq(job_id) in a comment\nTEXT = 'x.kiq(y)'\n",
        encoding="utf-8",
    )
    sites = _dispatch_sites((tmp_path,))
    assert not any("comment_only" in site for site in sites), sites
    assert any(site.endswith("spaced.py:2") for site in sites), sites
    assert any(site.endswith("kicker.py:2") for site in sites), sites
    # spaced.py: one .kiq call. kicker.py: one .kicker() call and one .kiq() call.
    assert len(sites) == 3, sites
