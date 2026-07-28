"""Make the ROB-1064 H6 package and its pure producer siblings importable."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
_PACKAGE = _HERE.parent
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"
_ALPACA_TRACK_SEAL = _REPO_ROOT / "research" / "alpaca_track_seal"
_ALPACA_TRACK_WALKFORWARD = _REPO_ROOT / "research" / "alpaca_track_walkforward"

for _path in (
    _PACKAGE,
    _NAUTILUS_SCALPING,
    _ALPACA_TRACK_SEAL,
    _ALPACA_TRACK_WALKFORWARD,
    _REPO_ROOT,
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
