"""Make the alpaca_track research package, the sibling nautilus_scalping
package (whose pure rob941_*/rob974_* primitives we reuse by import), and the
repo root (for the ``research_contracts`` canonical-hash authority) importable
in tests — mirrors ``research/nautilus_scalping/conftest.py``.
"""

import sys
from pathlib import Path

# S10 remediation: this file lives at .../research/alpaca_track/tests, THREE
# levels below the repo root (tests -> alpaca_track -> research -> repo
# root) -- the old code used `.parent.parent` (only two `.parent`s), which
# resolved to `.../research` and silently inserted a NONEXISTENT
# `.../research/research/nautilus_scalping` into sys.path. This was harmless
# only because the package-level `research/alpaca_track/conftest.py` (one
# level shallower, where `.parent.parent` IS correct) also runs and provides
# the real path. The `is_dir()` assert below makes a future regression here
# fail loudly at collection time instead of silently depending on that
# sibling conftest.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # .../auto_trader.<worktree>
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"

assert _NAUTILUS_SCALPING.is_dir(), (
    f"conftest path arithmetic is broken: {_NAUTILUS_SCALPING} does not exist "
    f"(computed repo root: {_REPO_ROOT})"
)

for _p in (str(_HERE), str(_NAUTILUS_SCALPING), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
