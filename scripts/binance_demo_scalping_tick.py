"""ROB-307 — operator CLI for one default-OFF Demo scalping scheduler tick.

Thin, env-driven wrapper over
``app.jobs.binance_demo_scalping_runner.run_demo_scalping_tick``: it runs one
gated tick and prints a single-line JSON summary to stdout so an external
scheduler (e.g. a *paused* Prefect deployment) can parse it. There are no
flags — behaviour is entirely env-gated, matching the runner contract:

* ``BINANCE_DEMO_SCALPING_ENABLED`` + ``BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED``
  — both must be truthy or the tick is a no-op (builds zero clients).
* ``BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM`` — only when truthy are real Demo
  orders placed; otherwise every entry is a dry-run (zero broker mutation).

Demo hosts only; no live/testnet path. No schedule is registered here —
production recurrence + activation is a separate operator gate (runbook).

Exit codes: 0 for a disabled (gate-off) or clean tick; 1 when the tick ran
with per-symbol errors or the runner raised.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.jobs.binance_demo_scalping_runner import run_demo_scalping_tick


def exit_code_for(summary: dict[str, Any]) -> int:
    """Map a tick summary to a process exit code.

    A gate-off (``disabled``) tick is a healthy no-op (0). A tick that ``ran``
    is a failure (1) only if it collected per-symbol errors, so an external
    scheduler treats a partially-failed tick as a failed run.
    """
    if summary.get("status") == "ran" and int(summary.get("error_count", 0)) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        summary = asyncio.run(run_demo_scalping_tick())
    except Exception as exc:  # noqa: BLE001 - serialize any failure for the scheduler
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return exit_code_for(summary)


if __name__ == "__main__":
    sys.exit(main())
