"""ROB-843 — durable ledger-write error signal.

The order-tracking fail-close is now a DURABLE write-ahead reservation
(``review.order_send_intents``; see ``reservation.py``), not a process-local
flag — so it survives restart and a fresh DB session. This module retains only
the write-error type used to distinguish a benign on-conflict no-op from a lost
native write.
"""

from __future__ import annotations


class LedgerWriteError(RuntimeError):
    """A durable ledger write failed with a real DB error (not a benign
    on-conflict no-op)."""
