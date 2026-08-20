"""Install the ROB-1880/ROB-1296 guard in Python children spawned by pytest.

The parent prepends this directory to ``PYTHONPATH`` and sets
``AUTO_TRADER_TEST_SOCKET_GUARD=1``. A failure here must abort interpreter
startup rather than let a child quietly run without the guard.

The guard is installed unconditionally. The child's *exemption* never comes from
the environment -- a boolean any test could export would be a general bypass --
but from a one-shot pipe whose read descriptor the parent chose to pass. Absent
descriptor means no exemption; a present but broken one is a hard failure.
"""

from __future__ import annotations

import os

if os.environ.get("AUTO_TRADER_TEST_SOCKET_GUARD") == "1":
    try:
        from tests._socket_guard import (
            install,
            read_policy_channel,
            set_current_test_exempt,
        )

        install()
        set_current_test_exempt(read_policy_channel())
    except BaseException as error:  # pragma: no cover - fatal startup path
        raise SystemExit(
            "ROB-1880 socket guard child startup installation failed"
        ) from error
