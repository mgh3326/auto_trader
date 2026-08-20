"""Every caller that can place a real Spot Demo order is enumerated here.

``BinanceSpotDemoExecutionClient.submit_order`` is the single signed-POST
chokepoint for ``demo-api.binance.com``. Each caller of it consumes a distinct,
separately reviewed operator authority, so a *new* caller is not a refactor —
it is a new execution surface. This test makes adding one a CI failure rather
than a discovery.

It also stops the docstrings from drifting into a false "sole entry point"
claim, which is exactly what happened before: the ROB-298 smoke CLI kept
calling itself the only one after the b0x sidecar had already joined it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    """Walk up to the marker rather than counting directories.

    A hard-coded parent depth silently resolves to the wrong directory when the
    file moves, and every rglob below then finds nothing — which reads as a
    clean pass.
    """

    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repository root not found")


_REPO_ROOT = _repo_root()

#: path -> the operator authority that permits it to mutate the Demo account.
APPROVED_SUBMIT_CALLERS: dict[str, str] = {
    "scripts/binance_spot_demo_smoke.py": "ROB-298 Spot Demo smoke (BUY round-trip)",
    "scripts/b0x/crypto/sidecar.py": (
        "b0x-adapter-orders-20260808 / binance_spot_demo_sidecar_buy_side_only"
    ),
    "scripts/binance_spot_demo_d2_remediation.py": (
        "binance-demo-remediation-20260820 / writer d2_remediation_single"
    ),
    "app/services/brokers/binance/spot_demo/d2_remediation_single.py": (
        "binance-demo-remediation-20260820 / the writer itself"
    ),
    "app/services/brokers/binance/spot_demo/mock_auto_limit.py": (
        "ROB-1270 J6B lane composition — structurally undispatchable: the "
        "signed registry makes assert_entry_execution_ready unsatisfiable for "
        "this lane, so the call site exists but no configuration reaches it"
    ),
}

#: Searched for callers. ``tests/`` is excluded on purpose: a test that fakes a
#: submit is not an execution surface.
_SEARCH_ROOTS = ("app", "scripts", "research")


def _calls_submit_order(path: Path) -> bool:
    """True when the file calls ``<something>.submit_order(...)``.

    AST rather than grep so a mention inside a docstring or a comment does not
    count as a call — the D2 CLI's own docstring names the method.
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "submit_order":
            return True
    return False


def _mentions_spot_demo_client(path: Path) -> bool:
    return "BinanceSpotDemoExecutionClient" in path.read_text(encoding="utf-8")


def test_no_unapproved_caller_can_place_a_spot_demo_order() -> None:
    found: dict[str, str] = {}
    scanned = 0
    for root in _SEARCH_ROOTS:
        search_root = _REPO_ROOT / root
        assert search_root.is_dir(), search_root
        for path in sorted(search_root.rglob("*.py")):
            scanned += 1
            if not _mentions_spot_demo_client(path):
                continue
            if _calls_submit_order(path):
                found[str(path.relative_to(_REPO_ROOT))] = "calls submit_order"

    # An empty scan would pass vacuously; prove the sweep actually ran.
    assert scanned > 100, scanned
    assert found, "no Spot Demo submit caller found at all — the sweep is broken"

    unapproved = sorted(set(found) - set(APPROVED_SUBMIT_CALLERS))
    assert not unapproved, (
        "new Spot Demo execution surface(s) with no named operator authority: "
        f"{unapproved}. Adding a caller of submit_order is a reviewed change, "
        "not a refactor — name its authority in APPROVED_SUBMIT_CALLERS."
    )


def test_every_approved_caller_still_exists() -> None:
    """A stale allowlist entry would quietly widen the check."""

    for relative in APPROVED_SUBMIT_CALLERS:
        assert (_REPO_ROOT / relative).is_file(), relative


def test_the_d2_writer_is_the_only_new_caller_this_change_added() -> None:
    d2_callers = {path for path in APPROVED_SUBMIT_CALLERS if "d2_remediation" in path}
    assert d2_callers == {
        "scripts/binance_spot_demo_d2_remediation.py",
        "app/services/brokers/binance/spot_demo/d2_remediation_single.py",
    }
