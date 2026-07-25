"""Pre-send live-mutation abort signal.

Dependency-free so broker transports and order orchestrators can share the
same fail-closed signal. The callback runs immediately before a real mutation
HTTP attempt and may reject freshness, validity, or market-session policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# A callback invoked immediately before each real broker mutation.
PreSendHook = Callable[[], Awaitable[None]]


class PreSendFreshnessError(RuntimeError):
    """The live mutation is no longer allowed at its HTTP send boundary."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__(",".join(self.reason_codes) or "pre_send_freshness")
