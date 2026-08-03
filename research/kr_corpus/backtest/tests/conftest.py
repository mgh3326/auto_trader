"""tests/ conftest — three levels below repo root."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_REPO_ROOT = _HERE.parent.parent.parent.parent

assert _REPO_ROOT.joinpath("research_contracts").is_dir(), (
    f"conftest path arithmetic broken: {_REPO_ROOT}"
)
assert _PKG.joinpath("loader.py").is_file(), f"package missing at {_PKG}"

for _p in (str(_PKG), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
