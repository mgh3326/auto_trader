"""Scoped sys.path bootstrap for this directory only.

``test_research_campaign_bridge_observer_effect.py`` (ROB-970 R2 stop-gate
audit item C) exercises the real ``record_attempt`` write path alongside
REAL, unmodified ``research/nautilus_scalping`` production modules (the
frozen campaign envelope/H4 walk-forward/H5 scorecard builders), which are
normally only importable when ``PYTHONPATH=research/nautilus_scalping:.``
is set externally (the project's documented convention for running the
``research/nautilus_scalping`` test suite). A bare ``pytest tests/`` (no
external PYTHONPATH) must still be able to COLLECT this directory without
breaking collection for the rest of the app test suite -- this conftest
inserts that one directory onto ``sys.path`` for collection/import
purposes, scoped by pytest to this directory (and subdirectories) only; it
has no effect on any other test directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RESEARCH_ROOT = Path(__file__).resolve().parents[3] / "research" / "nautilus_scalping"
if str(_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_ROOT))
