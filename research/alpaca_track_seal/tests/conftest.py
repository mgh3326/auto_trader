"""Mirrors ``research/alpaca_track_seal/conftest.py`` one level deeper (this
file lives at .../research/alpaca_track_seal/tests, THREE levels below the
repo root: tests -> alpaca_track_seal -> research -> repo root). See H1's
``research/alpaca_track/tests/conftest.py`` for the exact arithmetic-bug
class this defensive assert guards against (S10).
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # .../auto_trader.<worktree>
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"

assert _NAUTILUS_SCALPING.is_dir(), (
    f"conftest path arithmetic is broken: {_NAUTILUS_SCALPING} does not exist "
    f"(computed repo root: {_REPO_ROOT})"
)

for _p in (str(_HERE.parent), str(_NAUTILUS_SCALPING), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
