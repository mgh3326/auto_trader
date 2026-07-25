"""Make the alpaca_track research package, the sibling nautilus_scalping
package (whose pure rob941_*/rob974_* primitives we reuse by import), and the
repo root (for the ``research_contracts`` canonical-hash authority) importable
in tests — mirrors ``research/nautilus_scalping/conftest.py``.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.rob-1059
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"

for _p in (str(_HERE), str(_NAUTILUS_SCALPING), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
