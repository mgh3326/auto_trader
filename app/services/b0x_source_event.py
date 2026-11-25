"""Closed-world validator for B0X source kickoff events.

This module is the pure, standard-library contract that the shadow replay
artifacts (``app.services.shadow_replay.portability``) use to project a source
event. It holds no state, opens no database, and constructs no broker, order,
proposal, watch, approval, or strategy-loop client. The lane-event consumer,
dispatcher and poller that once consumed events were retired (hk 6457).
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

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


class B0XConsumerContractError(ValueError):
    """The supplied event violates the fail-closed source-event contract."""


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
