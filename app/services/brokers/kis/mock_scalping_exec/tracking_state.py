"""ROB-843 P1-2 — ledger-tracking degradation state + write-error signal.

When an accepted broker order cannot be durably tracked (neither a native
ledger row nor a synthetic fallback row persists), the daily broker-order count
would silently undercount and let the cap be exceeded. We instead mark tracking
as unavailable so the next automated order fails closed.

Process-local only: cross-process reservation/tracking is ROB-853's scope. A
single scalping daemon is one process, so this is sufficient here.
"""

from __future__ import annotations


class LedgerWriteError(RuntimeError):
    """A durable ledger write failed with a real DB error (not a benign
    on-conflict no-op)."""


_tracking_unavailable = False


def mark_ledger_tracking_unavailable() -> None:
    global _tracking_unavailable
    _tracking_unavailable = True


def is_ledger_tracking_unavailable() -> bool:
    return _tracking_unavailable


def reset_ledger_tracking_state() -> None:
    """Test/operator hook to clear the degraded flag."""
    global _tracking_unavailable
    _tracking_unavailable = False
