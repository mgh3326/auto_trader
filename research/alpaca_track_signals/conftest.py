"""ROB-1061 H3 — make the alpaca_track_signals package, its two producer
siblings (``research/alpaca_track`` H1 data-layer, ``research/alpaca_track_seal``
H2 registry seal — both consumed by import, never edited), the sibling
``research/nautilus_scalping`` package (whose pure ``canonical_hash`` shim we
reuse — the SAME typed canonical AST authority H1/H2/ROB-846 use), and the
repo root (for ``research_contracts``) importable in tests.

This package is a deliberate SIBLING of ``research/alpaca_track/`` and
``research/alpaca_track_seal/`` (H1/H2), not a subpackage of either — mirrors
``research/alpaca_track_seal/conftest.py``'s own rationale for being a sibling
of H1: H3's static no-PnL/import guard
(``tests/test_no_forbidden_imports_and_pnl_surface.py``) recursively scans
everything under ``research/alpaca_track_signals/`` only; it must never scan
(or be scanned as part of) H1/H2's own trees, and H1/H2's own guards must
never have to account for H3's modules.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.<worktree>
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"
_ALPACA_TRACK = _REPO_ROOT / "research" / "alpaca_track"
_ALPACA_TRACK_SEAL = _REPO_ROOT / "research" / "alpaca_track_seal"

assert _NAUTILUS_SCALPING.is_dir(), (
    f"conftest path arithmetic is broken: {_NAUTILUS_SCALPING} does not exist "
    f"(computed repo root: {_REPO_ROOT})"
)
assert _ALPACA_TRACK.is_dir(), f"{_ALPACA_TRACK} does not exist"
assert _ALPACA_TRACK_SEAL.is_dir(), f"{_ALPACA_TRACK_SEAL} does not exist"

for _p in (
    str(_HERE),
    str(_ALPACA_TRACK),
    str(_ALPACA_TRACK_SEAL),
    str(_NAUTILUS_SCALPING),
    str(_REPO_ROOT),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
