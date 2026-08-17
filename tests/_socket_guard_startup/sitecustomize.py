"""Install the ROB-1880 guard in Python children spawned by pytest.

The parent startup plugin prepends this directory to ``PYTHONPATH`` and sets
``AUTO_TRADER_TEST_SOCKET_GUARD=1``. A failure here must abort interpreter
startup rather than let a child quietly run without the guard.
"""

from __future__ import annotations

import os

if os.environ.get("AUTO_TRADER_TEST_SOCKET_GUARD") == "1":
    try:
        from tests._socket_guard import install

        install()
    except BaseException as error:  # pragma: no cover - fatal startup path
        raise SystemExit(
            "ROB-1880 socket guard child startup installation failed"
        ) from error
