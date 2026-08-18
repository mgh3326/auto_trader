"""Prefect wrapper for the ROB-1286 watch-fire repricing tick (§93차 A안).

Importable only. **No deployment is registered and no schedule is created
here** -- ROB-1286's recurrence is an operator step, and the issue's
invariant is that this flow is the single new recurring job. Placing the
flow in this repo (next to the existing ``app/flows/*_flow.py`` wrappers,
which follow the same importable-only convention) keeps ROB-1286 to one
repository; ``robin-prefect-automations`` is untouched by this change.

This file is a shell on purpose. ``prefect`` is not a project dependency,
so all behaviour lives in
:func:`app.services.watch_trigger_repricing.orchestrator.run_gated_tick`,
which imports and tests without it.

Three gates stand between an import and a spawned session:

* ``settings.WATCH_TRIGGER_REPRICING_ENABLED`` (default ``False``) -- off,
  the tick returns ``disabled`` before reading anything.
* the spawner argument -- the default is
  :class:`~app.services.watch_trigger_repricing.spawn.DrySessionSpawner`,
  which starts nothing. This PR ships no live spawner.
* the claim store must be durable across flow runs for dedup to hold
  between ticks. The only store in the repo today is process-local (see
  :mod:`app.services.watch_trigger_repricing.claims`), so arming this in
  production is blocked on the durable store landing.
"""

from __future__ import annotations

from typing import Any

from prefect import flow

from app.services.watch_trigger_repricing.orchestrator import run_gated_tick
from app.services.watch_trigger_repricing.selection import CandidateEvent

__all__ = ["watch_trigger_repricing_flow"]


@flow(name="watch-trigger-repricing")
async def watch_trigger_repricing_flow(
    events: list[CandidateEvent] | None = None,
) -> dict[str, Any]:
    return run_gated_tick(events=events or [])
