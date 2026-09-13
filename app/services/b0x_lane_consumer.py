"""Durable, closed-world consumer for B0X portability lane events.

This module deliberately depends only on the standard library.  It records a
transport artifact and its disposition; it cannot construct a broker, order,
proposal, watch, approval, or strategy-loop client.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_EVENT_ID = re.compile(
    r"^kickoff-(?P<slot>b0x-[a-z0-9-]+)-(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-T(?P<tick>\d{4}))?$"
)
_LANE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DISPOSITIONS = frozenset(
    {"cycle_kickoff", "policy_table_build", "observe_only_harvest"}
)
_SOURCE_CONTRACT = {
    "b0x-table-kr": (
        "policy_table_build",
        "docs/runbooks/b0x-policy-table-build.md",
        True,
        False,
        frozenset({"0745"}),
    ),
    "b0x-table-us": (
        "policy_table_build",
        "docs/runbooks/b0x-policy-table-build.md",
        True,
        False,
        frozenset({"2200"}),
    ),
    "b0x-nudge-kr": (
        "cycle_kickoff",
        "docs/runbooks/b0x-kr-cycle.md",
        True,
        False,
        frozenset({"0905"}),
    ),
    "b0x-nudge-us": (
        "cycle_kickoff",
        "docs/runbooks/b0x-us-cycle.md",
        True,
        False,
        frozenset({"2235"}),
    ),
    "b0x-nudge-crypto": (
        "cycle_kickoff",
        "docs/runbooks/b0x-crypto-cycle.md",
        False,
        True,
        frozenset({"0100", "0500", "0900", "1300", "1700", "2100"}),
    ),
    "b0x-harvest": (
        "observe_only_harvest",
        "docs/runbooks/b0x-harvest.md",
        False,
        True,
        frozenset(
            f"{hour:02d}{minute:02d}" for hour in range(24) for minute in (13, 43)
        ),
    ),
}
_SCHEMA_INITIALIZATION_LOCK = Lock()


class B0XConsumerContractError(ValueError):
    """The supplied path or event violates the fail-closed consumer contract."""


PRODUCTION_DELIVERY_INGRESS_IMPLEMENTED = True
PRODUCTION_BUSINESS_DISPATCH_IMPLEMENTED = True
PRODUCTION_BUSINESS_DISPATCH_BLOCKER = (
    "fixed dispatch code exists, but the private binding/install/account/owner "
    "receipts and all three activation gates remain unprovided and default-off"
)


@dataclass(frozen=True)
class ConsumerPathReadiness:
    ready: bool
    ingress_wired: bool
    dispatch_wired: bool
    source_may_be_enabled: bool
    blocker: str


def production_consumer_path_readiness() -> ConsumerPathReadiness:
    """Describe code-path readiness without claiming install or dispatch."""

    return ConsumerPathReadiness(
        ready=False,
        ingress_wired=PRODUCTION_DELIVERY_INGRESS_IMPLEMENTED,
        dispatch_wired=PRODUCTION_BUSINESS_DISPATCH_IMPLEMENTED,
        source_may_be_enabled=False,
        blocker=PRODUCTION_BUSINESS_DISPATCH_BLOCKER,
    )


@dataclass(frozen=True)
class ConsumerReceipt:
    lane: str
    event_id: str
    duplicate: bool
    disposition: str
    cycle_created: bool
    additional_kickoffs: int
    durable_evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _confined_db_path(path: Path) -> Path:
    if not path.is_absolute():
        raise B0XConsumerContractError("state database path must be absolute")
    if path.exists() and not path.is_file():
        raise B0XConsumerContractError("state database path must name a file")
    parent = path.parent.resolve(strict=True)
    resolved = (parent / path.name).resolve(strict=False)
    if resolved.parent != parent:
        raise B0XConsumerContractError("state database path escapes its parent")
    return resolved


def _payload(event: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if set(event) != {"type", "owner_lane", "event_id", "text"}:
        raise B0XConsumerContractError(
            "event envelope keys differ from the closed model"
        )
    if event.get("type") != "lane.event":
        raise B0XConsumerContractError("event type must be lane.event")
    lane = event.get("owner_lane")
    event_id = event.get("event_id")
    text = event.get("text")
    if not isinstance(lane, str) or _LANE_PATTERN.fullmatch(lane) is None:
        raise B0XConsumerContractError("owner_lane is missing or invalid")
    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise B0XConsumerContractError("event_id is not a frozen B0X kickoff id")
    if not isinstance(text, str):
        raise B0XConsumerContractError("event text must be JSON text")
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise B0XConsumerContractError("event text must be valid JSON") from exc
    if not isinstance(body, dict) or body.get("source") != "b0x":
        raise B0XConsumerContractError("event text source must be b0x")
    if set(body) != {
        "date",
        "disposition",
        "playbook",
        "source",
        "slot",
        "tick",
    }:
        raise B0XConsumerContractError(
            "event text keys differ from the closed source model"
        )
    match = _EVENT_ID.fullmatch(event_id)
    assert match is not None  # guarded above; keeps the identity comparison explicit
    expected = {
        "slot": match["slot"],
        "date": match["date"],
        "tick": match["tick"],
    }
    if any(body.get(field) != value for field, value in expected.items()):
        raise B0XConsumerContractError("event id and source payload identity differ")
    if body.get("disposition") not in _DISPOSITIONS:
        raise B0XConsumerContractError("event disposition is not allowed")
    if not isinstance(body.get("playbook"), str):
        raise B0XConsumerContractError("event playbook is missing")
    try:
        expected_disposition, expected_playbook, weekdays_only, tick_scoped, ticks = (
            _SOURCE_CONTRACT[str(body["slot"])]
        )
    except KeyError as exc:
        raise B0XConsumerContractError(
            "source slot is not in the closed model"
        ) from exc
    if body["disposition"] != expected_disposition:
        raise B0XConsumerContractError(
            "source disposition differs from the closed model"
        )
    if body["playbook"] != expected_playbook:
        raise B0XConsumerContractError("source playbook differs from the closed model")
    try:
        kst_day = date.fromisoformat(str(body["date"]))
    except ValueError as exc:
        raise B0XConsumerContractError("source date is not a calendar date") from exc
    if weekdays_only and kst_day.weekday() >= 5:
        raise B0XConsumerContractError("weekday-only source cannot run on a weekend")
    if tick_scoped and body["tick"] not in ticks:
        raise B0XConsumerContractError(
            "source tick differs from the closed KST schedule"
        )
    if not tick_scoped and body["tick"] is not None:
        raise B0XConsumerContractError("date-scoped source cannot carry a tick")
    return lane, event_id, body


def _received_clock(received_at: datetime | None) -> tuple[datetime, str]:
    effective = received_at or datetime.now(UTC)
    if not isinstance(effective, datetime) or effective.tzinfo is None:
        raise B0XConsumerContractError("received_at must be timezone-aware")
    if effective.utcoffset() is None:
        raise B0XConsumerContractError("received_at must have a usable UTC offset")
    return effective.astimezone(KST), effective.astimezone(UTC).isoformat()


def _eligible_at(body: dict[str, Any], received_kst: datetime) -> bool:
    *_, tick_scoped, scheduled_ticks = _SOURCE_CONTRACT[str(body["slot"])]
    received_tick = received_kst.strftime("%H%M")
    if body["date"] != received_kst.date().isoformat():
        return False
    if received_tick not in scheduled_ticks:
        return False
    return not tick_scoped or body["tick"] == received_tick


def _eligible_first_delivery(
    body: dict[str, Any],
    received_kst: datetime,
    hub_received_kst: datetime | None,
) -> bool:
    return _eligible_at(body, received_kst) and (
        hub_received_kst is None or _eligible_at(body, hub_received_kst)
    )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    # A process-local initialization fence prevents first-use threads from
    # racing the journal-mode transition. Production poller processes are
    # separately serialized by the binding's stable POSIX lock; SQLite still
    # owns atomic business identity through PRIMARY KEY(lane,event_id).
    try:
        with _SCHEMA_INITIALIZATION_LOCK:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS b0x_lane_event (
                lane TEXT NOT NULL,
                event_id TEXT NOT NULL,
                source_payload TEXT NOT NULL,
                disposition TEXT NOT NULL,
                cycle_created INTEGER NOT NULL CHECK (cycle_created IN (0, 1)),
                received_at TEXT NOT NULL,
                terminal_evidence TEXT,
                PRIMARY KEY (lane, event_id)
            );
            CREATE TABLE IF NOT EXISTS b0x_active_lane (
                lane TEXT PRIMARY KEY,
                event_id TEXT NOT NULL
            );
            """
            )
    except BaseException:
        connection.close()
        raise
    return connection


def _canonical_source_payload(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _consume_lane_event_in_connection(
    event: dict[str, Any],
    *,
    connection: sqlite3.Connection,
    received_at: datetime,
    hub_received_at: datetime | None = None,
) -> ConsumerReceipt:
    """Consume inside a transaction owned by the caller.

    The HTTP poller uses this path so its ingress receipt, business receipt,
    and sweep cursor share one SQLite commit. This function never commits or
    rolls back the supplied connection.
    """

    lane, event_id, body = _payload(event)
    received_kst, received = _received_clock(received_at)
    hub_received_kst = (
        _received_clock(hub_received_at)[0] if hub_received_at is not None else None
    )
    canonical_payload = _canonical_source_payload(body)
    prior = connection.execute(
        "SELECT source_payload, disposition, cycle_created FROM b0x_lane_event "
        "WHERE lane = ? AND event_id = ?",
        (lane, event_id),
    ).fetchone()
    if prior is not None:
        if prior[0] != canonical_payload:
            raise B0XConsumerContractError(
                "duplicate lane/event identity has different source payload"
            )
        return ConsumerReceipt(
            lane,
            event_id,
            True,
            str(prior[1]),
            bool(prior[2]),
            0,
            "sqlite_primary_key(lane,event_id)",
        )

    requested = str(body["disposition"])
    cycle_created = False
    if not _eligible_first_delivery(body, received_kst, hub_received_kst):
        disposition = "preserved_unconsumed_out_of_window"
    elif requested == "cycle_kickoff":
        active = connection.execute(
            "SELECT event_id FROM b0x_active_lane WHERE lane = ?", (lane,)
        ).fetchone()
        if active is None:
            connection.execute(
                "INSERT INTO b0x_active_lane(lane, event_id) VALUES (?, ?)",
                (lane, event_id),
            )
            disposition = "queued_cycle"
            cycle_created = True
        else:
            disposition = "held_unconsumed_active_slot"
    elif requested == "policy_table_build":
        disposition = "queued_policy_table_build"
    else:
        disposition = "observed_harvest_no_cycle"

    connection.execute(
        "INSERT INTO b0x_lane_event "
        "(lane, event_id, source_payload, disposition, cycle_created, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            lane,
            event_id,
            canonical_payload,
            disposition,
            int(cycle_created),
            received,
        ),
    )
    return ConsumerReceipt(
        lane,
        event_id,
        False,
        disposition,
        cycle_created,
        int(cycle_created),
        "sqlite_primary_key(lane,event_id)",
    )


def consume_lane_event(
    event: dict[str, Any],
    *,
    state_db: Path,
    received_at: datetime | None = None,
    hub_received_at: datetime | None = None,
) -> ConsumerReceipt:
    """Persist one event exactly once and return its explicit disposition.

    A first delivery outside its exact source minute is preserved as
    ``preserved_unconsumed_out_of_window``. A cycle event arriving while its
    lane already owns an active event is kept as
    ``held_unconsumed_active_slot``. Releasing the active event never promotes
    either kind of held record, so there is no implicit catch-up.
    """

    _payload(event)
    db_path = _confined_db_path(state_db)
    effective_received_at = received_at or datetime.now(UTC)
    _received_clock(effective_received_at)
    if hub_received_at is not None:
        _received_clock(hub_received_at)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        receipt = _consume_lane_event_in_connection(
            event,
            connection=connection,
            received_at=effective_received_at,
            hub_received_at=hub_received_at,
        )
        connection.commit()
        return receipt
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate_source_event(event: dict[str, Any]) -> dict[str, object]:
    """Validate and project a supplied event without consuming it."""

    lane, event_id, body = _payload(event)
    return {
        "lane": lane,
        "event_id": event_id,
        "source": body["source"],
        "slot": body["slot"],
        "date": body["date"],
        "tick": body["tick"],
        "disposition": body["disposition"],
        "playbook": body["playbook"],
    }


def record_terminal_disposition(
    *, state_db: Path, lane: str, event_id: str, evidence: str, failed: bool
) -> None:
    """Close an active item with explicit evidence; never promote queued work."""

    if not evidence.strip():
        raise B0XConsumerContractError("terminal evidence must not be empty")
    connection = _connect(_confined_db_path(state_db))
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT event_id FROM b0x_active_lane WHERE lane = ?", (lane,)
        ).fetchone()
        if active != (event_id,):
            raise B0XConsumerContractError("event is not the lane's active slot")
        terminal = "failed_preserved" if failed else "consumed"
        connection.execute(
            "UPDATE b0x_lane_event SET disposition = ?, terminal_evidence = ? "
            "WHERE lane = ? AND event_id = ?",
            (terminal, evidence, lane, event_id),
        )
        connection.execute("DELETE FROM b0x_active_lane WHERE lane = ?", (lane,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def event_rows(state_db: Path) -> list[dict[str, object]]:
    """Read durable dispositions for installation/readback evidence."""

    connection = _connect(_confined_db_path(state_db))
    try:
        rows = connection.execute(
            "SELECT lane, event_id, disposition, cycle_created, received_at, "
            "terminal_evidence "
            "FROM b0x_lane_event ORDER BY received_at, lane, event_id"
        ).fetchall()
        return [
            {
                "lane": row[0],
                "event_id": row[1],
                "disposition": row[2],
                "cycle_created": bool(row[3]),
                "received_at": row[4],
                "terminal_evidence": row[5],
            }
            for row in rows
        ]
    finally:
        connection.close()


__all__ = [
    "B0XConsumerContractError",
    "ConsumerPathReadiness",
    "ConsumerReceipt",
    "PRODUCTION_DELIVERY_INGRESS_IMPLEMENTED",
    "PRODUCTION_BUSINESS_DISPATCH_BLOCKER",
    "PRODUCTION_BUSINESS_DISPATCH_IMPLEMENTED",
    "consume_lane_event",
    "event_rows",
    "production_consumer_path_readiness",
    "record_terminal_disposition",
    "validate_source_event",
]
