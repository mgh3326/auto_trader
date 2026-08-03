"""Make the kr_corpus.backtest package and repo root importable in tests."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # .../auto_trader.<worktree>

assert _REPO_ROOT.joinpath("research_contracts").is_dir(), (
    f"conftest path arithmetic broken: research_contracts missing under {_REPO_ROOT}"
)

for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
