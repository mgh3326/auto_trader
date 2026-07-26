"""ROB-1062 H4 — make the alpaca_track_walkforward package, its three
producer siblings (``research/alpaca_track`` H1 data-layer, ``research/
alpaca_track_seal`` H2 registry seal, ``research/alpaca_track_signals`` H3
signal engines — all consumed by import, never edited), the sibling
``research/nautilus_scalping`` package (whose pure ``canonical_hash`` shim we
reuse — the SAME typed canonical AST authority H1/H2/H3 use), and the repo
root (for ``research_contracts``) importable in tests.

This package is a deliberate SIBLING of H1/H2/H3, not a subpackage of any of
them — mirrors H3's own conftest rationale for being a sibling of H1/H2: H4's
static OOS-mask/no-bypass guard and PnL-surface guards recursively scan
everything under ``research/alpaca_track_walkforward/`` only; they must never
scan (or be scanned as part of) H1/H2/H3's own trees, and H1/H2/H3's own
guards must never have to account for H4's modules.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.<worktree>
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"
_ALPACA_TRACK = _REPO_ROOT / "research" / "alpaca_track"
_ALPACA_TRACK_SEAL = _REPO_ROOT / "research" / "alpaca_track_seal"
_ALPACA_TRACK_SIGNALS = _REPO_ROOT / "research" / "alpaca_track_signals"

assert _NAUTILUS_SCALPING.is_dir(), (
    f"conftest path arithmetic is broken: {_NAUTILUS_SCALPING} does not exist "
    f"(computed repo root: {_REPO_ROOT})"
)
assert _ALPACA_TRACK.is_dir(), f"{_ALPACA_TRACK} does not exist"
assert _ALPACA_TRACK_SEAL.is_dir(), f"{_ALPACA_TRACK_SEAL} does not exist"
assert _ALPACA_TRACK_SIGNALS.is_dir(), f"{_ALPACA_TRACK_SIGNALS} does not exist"

for _p in (
    str(_HERE),
    str(_ALPACA_TRACK),
    str(_ALPACA_TRACK_SEAL),
    str(_ALPACA_TRACK_SIGNALS),
    str(_NAUTILUS_SCALPING),
    str(_REPO_ROOT),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
