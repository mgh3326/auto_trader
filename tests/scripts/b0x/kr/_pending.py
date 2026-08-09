"""Fake 자기 미체결 readers for the KR lane (contract v1.6 ①).

None of these touch a database. :class:`StatefulPendingLedger` is the
multi-cycle one: it models the single property the real ledger provides — the
submission chokepoint writes a row for every order that goes out, and a later
cycle reads those rows back — which is what makes the §4 caps bind *across*
cycles rather than only within one.
"""

from __future__ import annotations

import datetime as dt

from scripts.b0x.broker_truth import PendingUnreadable
from scripts.b0x.kr import pending_ledger as kr_pending_ledger
from scripts.b0x.kr.mock import KR_PENDING_UNREADABLE


def readable_pending(*symbols: str):
    """A reader whose ledger answers with exactly ``symbols``."""

    async def _read(
        *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...] | PendingUnreadable:
        return tuple(sorted(symbols))

    return _read


def unreadable_pending(sentinel: PendingUnreadable = KR_PENDING_UNREADABLE):
    """A reader that cannot answer — the v1.6 ④ tri-state."""

    async def _read(
        *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...] | PendingUnreadable:
        return sentinel

    return _read


def exploding_pending(exc: Exception):
    """A reader whose *underlying query* raises.

    Wraps the real :func:`scripts.b0x.kr.pending_ledger.read_own_pending`
    contract rather than short-circuiting it: the production function is the
    thing that must convert a fault into ``PendingUnreadable``, so this helper
    reproduces the fault and lets that conversion run.
    """

    async def _read(
        *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...] | PendingUnreadable:
        try:
            raise exc
        except Exception as caught:  # noqa: BLE001 — mirrors the real handler
            return kr_pending_ledger.ledger_unreadable(type(caught).__name__)

    return _read


class StatefulPendingLedger:
    """An in-memory stand-in for the ledger the submission path writes to.

    ``record`` is what the chokepoint does after a send; ``read`` is what the
    next cycle sees. Rows are stamped with the KST trading day, so the reader
    reproduces the real one's single bound (a KRX day order cannot rest past
    its accept day) without reproducing anything else — in particular it never
    inspects a lifecycle state, because the production reader does not either.
    """

    def __init__(self) -> None:
        #: ``(kst_day_label, symbol, correlation_id)`` — every recorded send.
        self.rows: list[tuple[str, str, str]] = []
        #: One entry per ``read`` call, for asserting the cycle actually asked.
        self.reads: list[str] = []

    def record(self, *, now: dt.datetime, symbol: str, correlation_id: str) -> None:
        self.rows.append(
            (kr_pending_ledger.kst_trading_day_label(now), symbol, correlation_id)
        )

    def symbols_on(self, now: dt.datetime) -> tuple[str, ...]:
        day = kr_pending_ledger.kst_trading_day_label(now)
        return tuple(
            sorted({symbol for row_day, symbol, _ in self.rows if row_day == day})
        )

    def reader(self, prefix: str = "b0xk-"):
        async def _read(
            *, now: dt.datetime, correlation_prefix: str
        ) -> tuple[str, ...] | PendingUnreadable:
            assert correlation_prefix == prefix, (
                f"cycle asked with prefix {correlation_prefix!r}, expected {prefix!r}"
            )
            self.reads.append(kr_pending_ledger.kst_trading_day_label(now))
            return self.symbols_on(now)

        return _read


__all__ = [
    "StatefulPendingLedger",
    "exploding_pending",
    "readable_pending",
    "unreadable_pending",
]
