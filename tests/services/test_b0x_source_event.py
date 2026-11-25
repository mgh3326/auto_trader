"""Closed-world validation of B0X source kickoff events (no consumer runtime)."""

from __future__ import annotations

import json

import pytest

from app.services import b0x_source_event
from app.services.b0x_source_event import (
    B0XConsumerContractError,
    validate_source_event,
)

pytestmark = pytest.mark.unit


def _event(
    event_id: str,
    *,
    lane: str = "b0x-source",
    disposition: str = "cycle_kickoff",
    playbook: str | None = None,
) -> dict[str, object]:
    identity = event_id.removeprefix("kickoff-")
    if "-T" in identity:
        base, tick = identity.rsplit("-T", 1)
    else:
        base, tick = identity, None
    slot, date = base.rsplit("-", 3)[0], "-".join(base.rsplit("-", 3)[1:])
    if playbook is None:
        playbook = {
            "b0x-nudge-kr": "docs/runbooks/b0x-kr-cycle.md",
            "b0x-nudge-us": "docs/runbooks/b0x-us-cycle.md",
            "b0x-nudge-crypto": "docs/runbooks/b0x-crypto-cycle.md",
        }.get(slot, "docs/runbooks/b0x-kr-cycle.md")
    body = {
        "date": date,
        "disposition": disposition,
        "playbook": playbook,
        "source": "b0x",
        "slot": slot,
        "tick": tick,
    }
    return {
        "type": "lane.event",
        "owner_lane": lane,
        "event_id": event_id,
        "text": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


def test_harvest_projects_its_closed_fields() -> None:
    observed = validate_source_event(
        _event(
            "kickoff-b0x-harvest-2026-09-10-T0013",
            disposition="observe_only_harvest",
            playbook="docs/runbooks/b0x-harvest.md",
        )
    )
    assert observed == {
        "lane": "b0x-source",
        "event_id": "kickoff-b0x-harvest-2026-09-10-T0013",
        "source": "b0x",
        "slot": "b0x-harvest",
        "date": "2026-09-10",
        "tick": "0013",
        "disposition": "observe_only_harvest",
        "playbook": "docs/runbooks/b0x-harvest.md",
    }


def test_policy_table_projects_without_cycle_semantics() -> None:
    observed = validate_source_event(
        _event(
            "kickoff-b0x-table-kr-2026-09-10",
            disposition="policy_table_build",
            playbook="docs/runbooks/b0x-policy-table-build.md",
        )
    )
    assert observed["disposition"] == "policy_table_build"
    assert observed["tick"] is None


@pytest.mark.parametrize(
    ("event", "error"),
    (
        (
            _event("kickoff-b0x-nudge-kr-2026-09-12"),
            "weekday-only source",
        ),
        (
            _event("kickoff-b0x-nudge-crypto-2026-09-10-T0230"),
            "closed KST schedule",
        ),
        (
            _event("kickoff-b0x-nudge-kr-2026-09-10-T0905"),
            "date-scoped source",
        ),
        (
            {**_event("kickoff-b0x-table-kr-2026-09-10"), "type": "message"},
            "event type must be lane.event",
        ),
    ),
)
def test_validator_rejects_weekend_off_schedule_and_envelope_mutants(
    event: dict[str, object], error: str
) -> None:
    with pytest.raises(B0XConsumerContractError, match=error):
        validate_source_event(event)


def test_module_keeps_no_consumer_runtime() -> None:
    """The retired consumer's state store and entry points are gone for good."""
    for name in (
        "consume_lane_event",
        "event_rows",
        "record_terminal_disposition",
        "production_consumer_path_readiness",
    ):
        assert not hasattr(b0x_source_event, name), name
