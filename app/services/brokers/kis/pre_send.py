"""ROB-843 P1 — pre-send freshness abort signal.

Dependency-free so both the KIS transport (``base.py``) and the order
orchestrator (``order_execution.py``) can reference it without an import cycle.
Raised by a mock-only pre-send callback invoked immediately before each real KIS
HTTP mutation; the orchestrator converts it into a ``pre_send_blocked`` result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# A mock-only callback invoked immediately before each real KIS HTTP mutation.
PreSendHook = Callable[[], Awaitable[None]]


class PreSendFreshnessError(RuntimeError):
    """The live book is no longer tradeable at the actual HTTP send boundary."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__(",".join(self.reason_codes) or "pre_send_freshness")
