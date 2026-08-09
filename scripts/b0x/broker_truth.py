"""B0-X envelope cap inputs, derived from the broker — contract v1.5 ①.

Why this module exists
----------------------

The §4 caps (동시 포지션 · 일일 신규 진입) used to be fed from
``attributed_book.json``: a per-lane state file with **read paths only and no
write path anywhere in the repo**. No cycle ever created it, so every cycle
loaded ``None``, every counter started at zero, and a cap the contract wrote as
*per UTC day* only ever bound *within a single cycle*. On the crypto lane (4h
table, six cycles per UTC day) the effective daily-new-entry ceiling was
2 × 6 = 12, not 2. Worse, nothing looked at the venue's own resting orders, so
the same symbol at the same level could be re-submitted every cycle and stack.

The lesson the contract records (v1.5 ①) is narrower than "the counter was
wrong": **「상한이 발화했다 ≠ 그 상한이 의도한 기간 동안 구속한다」**. A single
cycle showed the cap firing correctly, and that firing read as proof the cap
worked. It is not. Only a *multi-cycle* observation can tell the two apart —
hence ``tests/scripts/b0x/test_broker_truth_envelope.py``'s two-cycle
simulations against a fixed broker fixture.

The three literal definitions (contract v1.5 ①, verbatim)
---------------------------------------------------------

    동시 포지션      = non-dust 매도가능 base 잔고의 수
    동일 심볼 재제출 = 그 심볼에 자기(crypto ``b0xc`` / KR ``b0xk``) 미체결이
                       있으면 신규 제출 금지 (중복 적층 구조 차단)
    일일 신규        = { 자기 미체결 보유 심볼 ∪ non-dust 포지션 심볼 ∪
                       당 사이클 신규 제출 } 의 distinct 수

Three consequences of taking that literally, each of which looks like a bug
until you read it against the defect above:

* **Positions are counted account-wide, not attribution-scoped.** The cap
  exists to bound what the *account* is carrying; on a shared venue an
  unattributed balance still consumes the room a B0-X entry would need. That is
  the opposite direction from :class:`~scripts.b0x.state.B0XPosition`, which is
  attribution-scoped because it drives averaging/sell sizing off B0-X's own
  cost basis. Two different questions, two different sources.
* **The re-submit rule is side-agnostic.** "그 심볼에 … 신규 제출 금지" names
  the symbol, not the side. A resting buy therefore also blocks a sell rung on
  that symbol. This is deliberately wider than the repo's usual "cap buys only,
  never block an exit" convention, and narrower would be a contract violation.
* **A resting order counts toward the daily-new set even before it fills.**
  That is what makes the cap bind *across* cycles: cycle 2 sees cycle 1's
  resting orders in the broker's own answer, with no state file in between.

Dust is **not** redefined here. ``non-dust`` keeps the contract v1.2 meaning
each lane already implements — LOT_SIZE floor 후 매도가능 수량 > 0 for the
Binance sidecar (:func:`scripts.b0x.crypto.sidecar.sellable_qty`), ≥ 1 whole
share for KRX. MIN_NOTIONAL-based widening stays forbidden.

Readability is a tri-state, and "unreadable" is not "empty"
-----------------------------------------------------------

Some venues cannot answer "what of mine is still resting?". ``kis_mock`` is one:
its pending-order inquiry (TR ``TTTC8036R``) raises for ``is_mock=True``, and
its daily-execution inquiry can return ``rt_cd=0`` with **empty rows even after
same-day mock order activity** (ROB-341,
``docs/runbooks/kis-mock-scalping-smoke.md``), so an empty answer proves
nothing. Collapsing that into "no pending orders" would silently disable
duplicate prevention on the exact lane that is about to trade.

So unreadability is its own value — :class:`PendingUnreadable` — and it fails
**closed**: :meth:`BrokerTruth.resubmit_block` refuses every symbol while it is
in effect, exactly as if each one carried a resting order. Same shape as the KR
kill-switch cancellation, which says "시도조차 구조적으로 불가" rather than
reporting a cancellation it cannot perform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: ``resubmit_block`` reason — the broker answered, and it named this symbol.
OWN_PENDING_ORDER_EXISTS: Final[str] = "own_pending_order_exists"

#: ``resubmit_block`` reason — the broker cannot answer at all, so every symbol
#: is treated as if it carried a resting order.
OWN_PENDING_UNREADABLE: Final[str] = "own_pending_unreadable"


@dataclass(frozen=True, slots=True)
class PendingUnreadable:
    """The venue offers no way to read this lane's own resting orders.

    Not an error and not an empty result: a third state, carried explicitly so
    it can neither be mistaken for "nothing is resting" nor be dropped silently
    from an observation record.
    """

    #: Machine-readable code, recorded on every blocked leg.
    reason: str
    #: Why the venue cannot answer — cite the surface, not a guess.
    detail: str

    def canonical(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class BrokerTruth:
    """The three contract v1.5 ① cap inputs, read from the broker each cycle.

    Constructed from a lane's fresh account read and nothing else. There is no
    ``from_json``/``to_json`` pair and no persistence hook on purpose: the whole
    point is that this value cannot be carried over from a previous cycle, so
    it cannot go stale and cannot silently become empty.

    Symbols use the **policy table's** spelling (``KRW-BTC``, ``005930``), not
    the venue's, because that is what derivation matches rows on. Each lane
    translates at its own boundary.
    """

    #: Symbols carrying a non-dust sellable balance — account-wide, see the
    #: module docstring on why this is not attribution-scoped.
    position_symbols: tuple[str, ...]
    #: Symbols carrying one of *this lane's own* resting orders, or the
    #: sentinel when the venue cannot be asked.
    own_pending: tuple[str, ...] | PendingUnreadable

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "position_symbols", tuple(sorted(set(self.position_symbols)))
        )
        if not isinstance(self.own_pending, PendingUnreadable):
            object.__setattr__(
                self, "own_pending", tuple(sorted(set(self.own_pending)))
            )

    # -- readability ------------------------------------------------------

    @property
    def pending_unreadable(self) -> PendingUnreadable | None:
        return (
            self.own_pending
            if isinstance(self.own_pending, PendingUnreadable)
            else None
        )

    @property
    def own_pending_symbols(self) -> tuple[str, ...]:
        """Resting-order symbols, or ``()`` when unreadable.

        Callers must not read this as "nothing is resting" — check
        :attr:`pending_unreadable` first, or (better) go through
        :meth:`resubmit_block`, which already fails closed.
        """

        return (
            () if isinstance(self.own_pending, PendingUnreadable) else self.own_pending
        )

    # -- the three literal definitions ------------------------------------

    @property
    def concurrent_position_count(self) -> int:
        """① 동시 포지션 = non-dust 매도가능 base 잔고의 수."""

        return len(self.position_symbols)

    def resubmit_block(self, symbol: str) -> tuple[str, str] | None:
        """① 동일 심볼 재제출 — ``(reason, detail)`` when a new submission for
        ``symbol`` is forbidden, ``None`` when it is allowed.

        Side-agnostic by the literal: a resting order on the symbol blocks the
        symbol, whichever way either order points.
        """

        unreadable = self.pending_unreadable
        if unreadable is not None:
            return (
                OWN_PENDING_UNREADABLE,
                (
                    f"{unreadable.reason}: {unreadable.detail} — 「조회 불가」를 "
                    "「미체결 없음」으로 취급하지 않는다 (계약 v1.5 ①)"
                ),
            )
        if symbol in self.own_pending_symbols:
            return (
                OWN_PENDING_ORDER_EXISTS,
                (
                    f"{symbol} already carries this lane's own resting order — "
                    "동일 심볼 재제출 금지 (계약 v1.5 ①, 중복 적층 구조 차단)"
                ),
            )
        return None

    def daily_new_entry_seed(self) -> set[str]:
        """① 일일 신규 — the pre-cycle half of the distinct set.

        ``자기 미체결 보유 심볼 ∪ non-dust 포지션 심볼``. Derivation unions the
        third term (``당 사이클 신규 제출``) into the returned set as it admits
        entries, so the running count is the literal's distinct count.

        When pending is unreadable the pending term is missing here — which
        would under-count. It cannot under-*enforce*, because
        :meth:`resubmit_block` refuses every symbol in that state, so no entry
        can be admitted for the count to matter.
        """

        return set(self.position_symbols) | set(self.own_pending_symbols)

    # -- record -----------------------------------------------------------

    def canonical(self) -> dict[str, Any]:
        return {
            "position_symbols": list(self.position_symbols),
            "own_pending": (
                self.own_pending.canonical()
                if isinstance(self.own_pending, PendingUnreadable)
                else list(self.own_pending)
            ),
            "own_pending_readable": self.pending_unreadable is None,
        }


class OwnPendingResubmitBlocked(RuntimeError):
    """Contract v1.5 ① 동일 심볼 재제출 금지, raised at the submission boundary."""


def assert_resubmit_allowed(truth: BrokerTruth, *, symbol: str, lane: str) -> None:
    """Last line before a venue — re-check ① immediately before dispatch.

    Derivation already applies the same rule, and this is deliberately not
    trusting it: the sidecar re-runs its enable gate, envelope lock, symbol
    allowlist and contamination check at the submission boundary for the same
    reason. A plan is an intention; this is the dispatch.
    """

    block = truth.resubmit_block(symbol)
    if block is None:
        return
    reason, detail = block
    raise OwnPendingResubmitBlocked(
        f"lane={lane} symbol={symbol} refused at the submission boundary "
        f"({reason}): {detail}"
    )


__all__ = [
    "OWN_PENDING_ORDER_EXISTS",
    "OWN_PENDING_UNREADABLE",
    "PendingUnreadable",
    "BrokerTruth",
    "OwnPendingResubmitBlocked",
    "assert_resubmit_allowed",
]
