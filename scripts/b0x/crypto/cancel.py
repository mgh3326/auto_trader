"""B0-X sidecar cancel — attribution-gated, account-wide, own-orders-only.

Contract §2-4 gives the lane the duty to cancel *its* resting orders (kill
switch: "신규 주문 중단 + 잔여 주문 취소"), and contract v1.3 ① exercises it:
the two buys placed while B0-X was missing from the machine-readable account
map are cancelled before the first cycle is re-run.

The whole point of this module is the word *its*. These Spot Demo credentials
are shared with other demo lanes (strategy-loop runbook §5), so "cancel the
open orders" and "cancel our open orders" are different operations and only
the second one is authorized. Three properties enforce that:

1. **Account-wide read, symbol-scoped never.** :func:`scan` calls
   ``get_all_open_orders`` — omitting the symbol — because an order this lane
   must not touch is, by definition, one it would not think to ask about. A
   per-symbol loop over the three allowlisted symbols would report a clean
   book while another writer rests orders on a fourth.
2. **Attribution is a prefix match, never an inference.** An order belongs to
   B0-X iff its ``clientOrderId`` starts with ``b0xc``
   (:data:`sidecar.CLIENT_ORDER_ID_PREFIX`). Everything else is *foreign* —
   not "probably ours", not "ours if the symbol matches", not "ours because
   nothing else should be running". :func:`partition` is pure and total, so
   an order can never fall through into the cancel set by accident.
3. **The prefix is re-checked at the call site.** :func:`cancel_own` asserts
   attribution again on each order immediately before dispatching the DELETE,
   so a future refactor that widens :func:`partition` still cannot reach a
   foreign order without also defeating this second check.

Foreign orders are *reported, never touched* — deciding what to do about
another lane's book is an operator/owning-lane matter (mock/CLAUDE.md §4:
"다른 전략의 보유·미체결·원장 행을 정리·청산·정정·취소하지 않는다").

Like every other mutation in this package, the DELETE only happens under an
explicit ``confirm=True``; the default is a dry run that dispatches zero
mutation HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.b0x.crypto.sidecar import CLIENT_ORDER_ID_PREFIX


class ForeignOrderCancelAttempt(RuntimeError):
    """A cancel was attempted against an order B0-X did not create.

    Raised by the call-site re-check in :func:`cancel_own`. Reaching this is
    a bug, not an operational condition: it means the partition and the
    dispatcher disagree about attribution.
    """


def is_b0x_order(client_order_id: str | None) -> bool:
    """Attribution predicate — prefix match on ``clientOrderId``, nothing else.

    ``None``/empty is **not** ours. An order the venue reports without a
    client id cannot be attributed, and an unattributable order is foreign
    by the fail-closed rule (contract §2-3 writer=1).

    The separator is part of the prefix and something must follow it. A bare
    ``"b0xc-"`` carries no order key, so it is not an id this lane can have
    minted (:func:`sidecar.client_order_id_for` always appends one) — and a
    prefix test loose enough to accept it is also loose enough to accept a
    foreign id that merely begins the same way.
    """

    if not client_order_id:
        return False
    marker = f"{CLIENT_ORDER_ID_PREFIX}-"
    text = str(client_order_id)
    return text.startswith(marker) and len(text) > len(marker)


@dataclass(frozen=True)
class OrderPartition:
    """Account-wide open orders split by attribution."""

    mine: tuple[Any, ...]
    foreign: tuple[Any, ...]

    @property
    def total(self) -> int:
        return len(self.mine) + len(self.foreign)

    def to_json(self) -> dict[str, Any]:
        return {
            "total_open_orders": self.total,
            "mine": [_order_json(order) for order in self.mine],
            "foreign": [_order_json(order) for order in self.foreign],
        }


def _order_json(order: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(order, "symbol", ""),
        "client_order_id": getattr(order, "client_order_id", ""),
        "broker_order_id": getattr(order, "broker_order_id", ""),
        "side": getattr(order, "side", ""),
        "qty": str(getattr(order, "qty", "")),
        "status": getattr(order, "status", ""),
        "attribution": (
            "b0x" if is_b0x_order(getattr(order, "client_order_id", "")) else "foreign"
        ),
    }


def partition(orders: tuple[Any, ...] | list[Any]) -> OrderPartition:
    """Split orders into B0-X's and everyone else's. Pure and total."""

    mine = tuple(o for o in orders if is_b0x_order(getattr(o, "client_order_id", "")))
    foreign = tuple(
        o for o in orders if not is_b0x_order(getattr(o, "client_order_id", ""))
    )
    return OrderPartition(mine=mine, foreign=foreign)


async def scan(client: Any) -> OrderPartition:
    """Read every resting order on the account and partition by attribution."""

    result = await client.get_all_open_orders()
    return partition(tuple(result.orders))


@dataclass(frozen=True)
class CancelOutcome:
    """What the cancel pass did — and, as loudly, what it left alone."""

    partition: OrderPartition
    cancelled: tuple[dict[str, Any], ...]
    confirm: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "confirm": self.confirm,
            "open_orders": self.partition.to_json(),
            "cancelled": list(self.cancelled),
            "not_mine_untouched": [
                _order_json(order) for order in self.partition.foreign
            ],
        }


async def cancel_own(
    client: Any,
    *,
    confirm: bool = False,
) -> CancelOutcome:
    """Cancel only the B0-X-attributed resting orders on this account.

    Foreign orders are carried through to the outcome untouched so the
    report can name them; no cancel is dispatched for them under any flag.
    """

    found = await scan(client)
    cancelled: list[dict[str, Any]] = []
    for order in found.mine:
        client_order_id = getattr(order, "client_order_id", "")
        # Defense in depth: the partition already filtered, and this asserts
        # the same invariant at the point where the DELETE actually leaves.
        if not is_b0x_order(client_order_id):
            raise ForeignOrderCancelAttempt(
                f"refusing to cancel {client_order_id!r}: not a "
                f"{CLIENT_ORDER_ID_PREFIX}- order"
            )
        result = await client.cancel_order(
            symbol=getattr(order, "symbol", ""),
            client_order_id=client_order_id,
            confirm=confirm,
        )
        cancelled.append(
            {
                "symbol": getattr(order, "symbol", ""),
                "client_order_id": client_order_id,
                "broker_order_id": getattr(order, "broker_order_id", ""),
                "side": getattr(order, "side", ""),
                "qty": str(getattr(order, "qty", "")),
                "requested": True,
                "dispatched": confirm,
                "status": getattr(result, "status", "DRY_RUN" if not confirm else ""),
            }
        )
    return CancelOutcome(partition=found, cancelled=tuple(cancelled), confirm=confirm)


__all__ = [
    "CLIENT_ORDER_ID_PREFIX",
    "CancelOutcome",
    "ForeignOrderCancelAttempt",
    "OrderPartition",
    "cancel_own",
    "is_b0x_order",
    "partition",
    "scan",
]
