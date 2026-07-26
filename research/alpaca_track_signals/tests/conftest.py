"""Make the alpaca_track_signals package, its H1/H2 producer siblings, the
sibling nautilus_scalping package, and the repo root importable in tests —
mirrors ``research/alpaca_track/tests/conftest.py`` / ``research/alpaca_track_seal/
tests/conftest.py``. See the package-level ``conftest.py`` (one directory
shallower) for the full rationale.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = (
    _HERE.parent.parent.parent
)  # tests -> alpaca_track_signals -> research -> repo root
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
