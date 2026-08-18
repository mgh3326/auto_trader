"""Prefect wrapper for the ROB-1286 watch-fire repricing tick (§93차 A안).

Importable only. **No deployment is registered and no schedule is created
here** -- ROB-1286's recurrence is an operator step, and the issue's
invariant is that this flow is the single new recurring job.

🔴 **Owning repository is an open operator decision (r2 / SHOULD-3).** The
Linear design names ``robin-prefect-automations`` as the flow's home; this
file sits in auto_trader instead, next to the existing
``app/flows/*_flow.py`` wrappers that follow the same importable-only
convention. Note that ``prefect`` is not a project dependency, so this
module does **not** import in the auto_trader venv -- it is a scaffold
either way. The decision was deliberately not made here and no other
repository was touched; see the runbook §5 item 5 for both options. If the
call goes to ``robin-prefect-automations`` this is a single file move,
because all behaviour lives in ``run_gated_tick``.

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
* the claim store must be durable across *processes* for dedup to hold
  between Prefect flow runs. The only store in the repo today is
  process-local (see :mod:`app.services.watch_trigger_repricing.claims`),
  and ``run_gated_tick`` now **refuses** to run a non-dry spawner against
  it -- returning ``status="blocked"`` rather than trusting a comment. So
  arming this in production is blocked in code on the durable store
  (a migration) landing.
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
