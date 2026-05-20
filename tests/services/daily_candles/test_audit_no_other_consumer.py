"""Locks the invariant that crypto_candles_1d has exactly one reader/writer.

ROB-284 pre-implementation audit (2026-05-20): only
app/services/daily_candles/repository.py touches the table. If this test
fails after ROB-284 lands, a new consumer was added without migrating to
the new instrument-FK shape — re-evaluate before merging.
"""

from __future__ import annotations

import pathlib
import subprocess


ALLOWED = {"app/services/daily_candles/repository.py"}


def test_only_daily_candles_repository_references_crypto_candles_1d() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "grep",
            "-rln",
            "crypto_candles_1d",
            "--include=*.py",
            "app/",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    unexpected = files - ALLOWED
    assert not unexpected, (
        f"Unexpected files reference crypto_candles_1d: {sorted(unexpected)}. "
        "If you intentionally added a new consumer of crypto_candles_1d, extend "
        "the ALLOWED set in this test and explain why in your PR description. "
        "ROB-284 audit invariant — see "
        "docs/plans/ROB-284-crypto-instruments-schema-implementation-plan.md "
        "for context."
    )
