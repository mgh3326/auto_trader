"""ROB-1060 H2 — make the alpaca_track_seal package, the sibling
nautilus_scalping package (whose pure ``canonical_hash`` shim we reuse by
import — the SAME typed canonical AST authority H1/ROB-846 use), and the repo
root (for the ``research_contracts`` canonical-hash authority) importable in
tests. Mirrors ``research/alpaca_track/conftest.py`` and
``research/nautilus_scalping/conftest.py``.

This package is a deliberate SIBLING of ``research/alpaca_track/`` (H1), not a
subpackage of it: H1's own import guard
(``research/alpaca_track/tests/test_import_and_time_guards.py``) recursively
scans everything under ``research/alpaca_track/`` and forbids ``app`` imports
anywhere in that tree. H2's optional ROB-846 registry-registration CLI
legitimately needs a deferred ``app.*`` import (see ``registry_cli.py``), so
placing H2 as a sibling — never inside H1's tree — avoids tripping H1's guard
while adding nothing to what H1 must scan.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.<worktree>
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"

assert _NAUTILUS_SCALPING.is_dir(), (
    f"conftest path arithmetic is broken: {_NAUTILUS_SCALPING} does not exist "
    f"(computed repo root: {_REPO_ROOT})"
)

for _p in (str(_HERE), str(_NAUTILUS_SCALPING), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
