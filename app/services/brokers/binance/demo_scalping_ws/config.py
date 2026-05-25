"""ROB-317 — WS scalping daemon gate configuration (default-disabled).

Three independent gates, all default false:

* ``BINANCE_DEMO_SCALPING_ENABLED``     — master capability (shared)
* ``BINANCE_DEMO_SCALPING_WS_ENABLED``  — long-running WS daemon gate
* ``BINANCE_DEMO_SCALPING_WS_CONFIRM``  — real Demo order-mutation gate

The daemon does NOT reuse the scheduler confirm flag
(``BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM``). Daemon and scheduler are gated
independently so enabling one never silently enables the other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_BASE_ENV = "BINANCE_DEMO_SCALPING_ENABLED"
_WS_ENV = "BINANCE_DEMO_SCALPING_WS_ENABLED"
_WS_CONFIRM_ENV = "BINANCE_DEMO_SCALPING_WS_CONFIRM"


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class WsDaemonGates:
    """Resolved daemon gate state. Defaults to fully disabled."""

    base_enabled: bool
    ws_enabled: bool
    ws_confirm: bool

    @property
    def daemon_active(self) -> bool:
        """True only when the daemon may subscribe and evaluate triggers."""
        return self.base_enabled and self.ws_enabled

    @property
    def mutation_allowed(self) -> bool:
        """True only when real Demo order mutation is permitted (all three on)."""
        return self.base_enabled and self.ws_enabled and self.ws_confirm

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WsDaemonGates:
        source = dict(os.environ) if env is None else env
        return cls(
            base_enabled=_truthy(source.get(_BASE_ENV)),
            ws_enabled=_truthy(source.get(_WS_ENV)),
            ws_confirm=_truthy(source.get(_WS_CONFIRM_ENV)),
        )
