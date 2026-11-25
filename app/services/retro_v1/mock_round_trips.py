"""``mock_round_trips`` — paper/demo strategy round trips as chains (read only).

One chain per episode of ``review.mock_strategy_orders``: the entry leg (its
``client_order_id`` is the ``episode_id``) and every exit leg that shares the
episode. Rows are marked ``mode=mock``; they never join the live projections,
and the P&L is the journal's own figure, so ``basis=inference``.

The journal is read, never written: the recorder module is not imported and
no claim, send or reconcile happens. A chain whose leg is still unresolved
(``sending``/``uncertain``) or whose close has no realized figure carries a
row ``reason`` instead of a zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mock_strategy import MockStrategyOrder
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    RetroReadError,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_text,
    dec,
    decode_cursor,
    guarded,
    iso,
    latest,
    make_ref,
    params_digest,
    read_snapshot,
)
from app.services.retro_v1.mock_experiments import VENUE_CURRENCY

ENTRY_SIDES = ("buy", "short")
EXIT_SIDES = ("sell", "cover")
#: A leg the journal cannot resolve yet: it may or may not have reached the broker.
UNRESOLVED_LEG = ("sending", "uncertain")


def _check_venue(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in VENUE_CURRENCY:
        raise RetroReadError("validation_error")
    return value


def _episodes(rows: list[Any]) -> list[tuple[Any, list[Any], list[Any]]]:
    """Group journal legs into ``(key, entry_or_None, exit_legs)`` episodes."""
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        groups.setdefault((row.strategy_id, row.venue, row.episode_id), []).append(row)
    episodes = []
    for key, legs in groups.items():
        entry = next(
            (
                leg
                for leg in legs
                if leg.side in ENTRY_SIDES and leg.client_order_id == leg.episode_id
            ),
            None,
        )
        exits = sorted(
            (leg for leg in legs if leg.side in EXIT_SIDES),
            key=lambda leg: leg.id,
        )
        episodes.append((key, entry, exits))
    return episodes


def _state(entry: Any, exits: list[Any]) -> tuple[str, str | None]:
    """``(state, reason)`` — the closed vocabulary of one round trip."""
    if entry is None:
        return "unresolved", "incomplete_evidence"
    legs = [entry, *exits]
    if any(leg.status in UNRESOLVED_LEG for leg in legs):
        return "unresolved", "uncertain_leg"
    if entry.status == "planned":
        return "not_filled", "not_sent"
    if entry.status == "rejected":
        return "not_filled", "rejected"
    if entry.status == "canceled":
        return "not_filled", "canceled"
    if entry.status == "closed":
        if entry.realized_pnl is None:
            return "closed", "pnl_unrecorded"
        return "closed", None
    if entry.status in ("accepted", "partially_filled"):
        return "open", "awaiting_fill"
    return "open", "awaiting_close_evidence"


def _leg(leg: Any, role: str) -> dict[str, Any]:
    # Absent keys stand for "not stored" (a notional-sized leg has no quantity);
    # the closed leg shape never carries a null.
    out: dict[str, Any] = {
        "leg_ref": make_ref("mock_leg", leg.id),
        "role": role,
        "side": leg.side,
        "status": leg.status,
        "broker_order_bound": leg.broker_order_id is not None,
    }
    if leg.qty is not None:
        out["quantity"] = dec(leg.qty)
    if leg.notional is not None:
        out["notional"] = dec(leg.notional)
    return out


def round_trip_row(key: tuple[str, str, str], entry: Any, exits: list[Any]) -> Row:
    strategy_id, venue, episode_id = key
    state, reason = _state(entry, exits)
    closed = state == "closed" and entry is not None
    legs = []
    if entry is not None:
        legs.append(_leg(entry, "entry"))
    legs.extend(_leg(leg, "exit") for leg in exits)
    row = {
        "strategy_id": strategy_id,
        "round_trip_ref": make_ref(
            "mock_round_trip", f"{strategy_id}|{venue}|{episode_id}"
        ),
        "environment": venue,
        "mode": "mock",
        "side": entry.side if entry is not None else None,
        "state": state,
        "reason": reason,
        "opened_at": iso(entry.opened_at) if entry is not None else None,
        "closed_at": iso(entry.closed_at) if closed else None,
        "entry_price": dec(entry.entry_price) if entry is not None else None,
        # Exit price and P&L are the episode's totals: shown only once it closed.
        "exit_price": dec(entry.exit_price) if closed else None,
        "realized_pnl_amount": dec(entry.realized_pnl) if closed else None,
        "currency": VENUE_CURRENCY[venue],
        "unit": "currency",
        "basis": "inference",
        "legs": legs,
    }
    return row, {}


def _sort_key(entry: Any, key: tuple[str, str, str]) -> tuple[Any, ...]:
    opened = entry.opened_at if entry is not None else None
    return (
        opened is None,
        opened or datetime.min.replace(tzinfo=UTC),
        key,
    )


async def read_mock_round_trips(
    session: AsyncSession,
    *,
    strategy_id: str | None = None,
    venue: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    wanted = check_text(strategy_id, max_len=128) if strategy_id is not None else None
    wanted_venue = _check_venue(venue)
    digest = params_digest(
        op="mock_round_trips", strategy_id=wanted, venue=wanted_venue
    )
    offset = decode_cursor(cursor, digest)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    order = MockStrategyOrder
    async with read_snapshot(session):

        async def read() -> list[Any]:
            # One row past the cap: a read that reaches it is cut off, so the
            # chains it would show are incomplete and must not be presented.
            stmt = select(order).order_by(order.id).limit(MAX_SOURCE_ROWS + 1)
            if wanted is not None:
                stmt = stmt.where(order.strategy_id == wanted)
            if wanted_venue is not None:
                stmt = stmt.where(order.venue == wanted_venue)
            return list((await session.execute(stmt)).scalars().all())

        found, ok = await guarded(
            session, unknowns, "source.mock_strategy_orders", read, []
        )

    complete = ok and len(found) <= MAX_SOURCE_ROWS
    if ok and not complete:
        unknowns.note("source.mock_strategy_orders", "unavailable")
    episodes = _episodes(found) if complete else []
    episodes.sort(key=lambda item: _sort_key(item[1], item[0]))
    rows = [round_trip_row(key, entry, exits) for key, entry, exits in episodes]
    return build_response(
        as_of=as_of,
        data_as_of=latest(
            value
            for leg in (found if complete else [])
            for value in (leg.opened_at, leg.closed_at)
        ),
        rows=rows,
        total=len(rows) if complete else None,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
