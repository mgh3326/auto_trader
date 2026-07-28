"""ROB-1064 H6 test import wiring.

The accounting package is a pure sibling of H2 and H4.  It may consume their
public, offline authorities, but it must not import ``app`` or reach any
runtime service.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"
_ALPACA_TRACK = _REPO_ROOT / "research" / "alpaca_track"
_ALPACA_TRACK_SEAL = _REPO_ROOT / "research" / "alpaca_track_seal"
_ALPACA_TRACK_SIGNALS = _REPO_ROOT / "research" / "alpaca_track_signals"
_ALPACA_TRACK_WALKFORWARD = _REPO_ROOT / "research" / "alpaca_track_walkforward"

for _path in (
    _HERE,
    _NAUTILUS_SCALPING,
    _ALPACA_TRACK,
    _ALPACA_TRACK_SEAL,
    _ALPACA_TRACK_SIGNALS,
    _ALPACA_TRACK_WALKFORWARD,
    _REPO_ROOT,
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
