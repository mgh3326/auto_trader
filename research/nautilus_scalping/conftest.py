"""Make the research package and the auto_trader repo root importable in tests.

The research venv (3.13) does not install auto_trader; we only need the pure
``signal.py`` module, whose import chain is stdlib-only (verified ROB-316).
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.rob-316

for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
