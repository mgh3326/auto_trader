"""ROB-307 PR4 — TaskIQ task for the Demo scalping scheduler tick.

Registered with **no ``schedule=``** — a manual entry point only (e.g.
``taskiq kick``), never auto-scheduled. Production recurrence + activation
is a separate operator gate handled outside this repo (see
``docs/runbooks/binance-demo-scalping.md``). The runner is default-OFF
behind a two-key flag gate, so even invoking this task is a no-op until
the operator opts in.
"""

from __future__ import annotations

from typing import Any

from app.core.taskiq_broker import broker
from app.jobs.binance_demo_scalping_runner import run_demo_scalping_tick


@broker.task(task_name="binance.demo_scalping.tick")
async def binance_demo_scalping_tick() -> dict[str, Any]:
    return await run_demo_scalping_tick()
