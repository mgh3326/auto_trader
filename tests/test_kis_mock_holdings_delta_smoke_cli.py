"""ROB-341 — CLI-shape tests for the holdings-delta smoke.

The CLI must parse --help and the flag surface WITHOUT importing Settings or
touching secrets/network (Settings-backed imports are lazy, inside the command
body). No order is ever placed by these tests.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.unit
def test_smoke_help_runs_without_secrets():
    out = subprocess.run(
        [sys.executable, "-m", "scripts.kis_mock_holdings_delta_smoke", "--help"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "--preflight" in out.stdout
    assert "--confirm" in out.stdout
    assert "--symbol" in out.stdout
    assert "--notional-krw" in out.stdout


@pytest.mark.unit
def test_smoke_requires_a_mode():
    # Neither --preflight nor --confirm -> usage error (exit 2), no network.
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.kis_mock_holdings_delta_smoke",
            "--symbol",
            "005930",
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 2
