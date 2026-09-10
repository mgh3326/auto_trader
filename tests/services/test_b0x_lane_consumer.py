"""Offline real-shape tests for the production B0X consumer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.b0x_lane_consumer import (
    B0XConsumerContractError,
    consume_lane_event,
    event_rows,
    production_consumer_path_readiness,
    record_terminal_disposition,
)

pytestmark = pytest.mark.unit
KST = ZoneInfo("Asia/Seoul")


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


def test_retry_and_process_restart_consume_no_additional_kickoff(
    tmp_path: Path,
) -> None:
    database = tmp_path / "consumer.sqlite3"
    event = _event("kickoff-b0x-nudge-kr-2026-09-10")
    received = datetime(2026, 9, 10, 9, 5, 59, tzinfo=KST)
    first = consume_lane_event(event, state_db=database, received_at=received)
    second = consume_lane_event(event, state_db=database, received_at=received)

    assert first.cycle_created is True
    assert first.additional_kickoffs == 1
    assert second.duplicate is True
    assert second.additional_kickoffs == 0
    assert len(event_rows(database)) == 1


def test_concurrent_duplicate_delivery_is_atomic(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    event = _event("kickoff-b0x-nudge-kr-2026-09-10")
    received = datetime(2026, 9, 10, 9, 5, 59, tzinfo=KST)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _: consume_lane_event(
                    event, state_db=database, received_at=received
                ),
                range(16),
            )
        )

    assert sum(receipt.additional_kickoffs for receipt in receipts) == 1
    assert sum(not receipt.duplicate for receipt in receipts) == 1
    assert len(event_rows(database)) == 1


def test_same_event_id_in_another_lane_is_independent(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    event_id = "kickoff-b0x-nudge-kr-2026-09-10"
    received = datetime(2026, 9, 10, 9, 5, tzinfo=KST)
    first = consume_lane_event(
        _event(event_id, lane="lane-a"), state_db=database, received_at=received
    )
    other = consume_lane_event(
        _event(event_id, lane="lane-b"), state_db=database, received_at=received
    )

    assert first.cycle_created is True
    assert other.cycle_created is True
    assert {(row["lane"], row["event_id"]) for row in event_rows(database)} == {
        ("lane-a", event_id),
        ("lane-b", event_id),
    }


def test_active_slot_is_not_replaced_or_implicitly_caught_up(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    first_id = "kickoff-b0x-nudge-kr-2026-09-10"
    next_id = "kickoff-b0x-nudge-kr-2026-09-11"
    consume_lane_event(
        _event(first_id),
        state_db=database,
        received_at=datetime(2026, 9, 10, 9, 5, tzinfo=KST),
    )
    held = consume_lane_event(
        _event(next_id),
        state_db=database,
        received_at=datetime(2026, 9, 11, 9, 5, tzinfo=KST),
    )
    assert held.disposition == "held_unconsumed_active_slot"
    assert held.cycle_created is False

    record_terminal_disposition(
        state_db=database,
        lane="b0x-source",
        event_id=first_id,
        evidence="fixture-consumed",
        failed=False,
    )
    rows = event_rows(database)
    assert [row["disposition"] for row in rows] == [
        "consumed",
        "held_unconsumed_active_slot",
    ]
    assert rows[1]["cycle_created"] is False


def test_failed_item_and_kst_date_boundary_remain_explicit(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    before = _event("kickoff-b0x-nudge-crypto-2026-09-10-T2100")
    first = consume_lane_event(
        before,
        state_db=database,
        received_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    record_terminal_disposition(
        state_db=database,
        lane=first.lane,
        event_id=first.event_id,
        evidence="fixture-failure-preserved",
        failed=True,
    )
    after = consume_lane_event(
        _event("kickoff-b0x-nudge-crypto-2026-09-11-T0100"),
        state_db=database,
        received_at=datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
    )
    assert after.cycle_created is True
    assert [row["disposition"] for row in event_rows(database)] == [
        "failed_preserved",
        "queued_cycle",
    ]


def test_harvest_ticks_never_create_cycles(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    receipts = [
        consume_lane_event(
            _event(
                f"kickoff-b0x-harvest-2026-09-10-T{tick}",
                disposition="observe_only_harvest",
                playbook="docs/runbooks/b0x-harvest.md",
            ),
            state_db=database,
            received_at=datetime(2026, 9, 10, hour, minute, 59, tzinfo=KST),
        )
        for tick, hour, minute in (("0013", 0, 13), ("0043", 0, 43))
    ]
    assert [receipt.event_id for receipt in receipts] == [
        "kickoff-b0x-harvest-2026-09-10-T0013",
        "kickoff-b0x-harvest-2026-09-10-T0043",
    ]
    assert all(
        receipt.disposition == "observed_harvest_no_cycle" for receipt in receipts
    )
    assert all(not receipt.cycle_created for receipt in receipts)
    assert all(receipt.additional_kickoffs == 0 for receipt in receipts)


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
    ),
)
def test_consumer_rejects_weekend_and_off_schedule_source_mutants(
    tmp_path: Path, event: dict[str, object], error: str
) -> None:
    with pytest.raises(B0XConsumerContractError, match=error):
        consume_lane_event(event, state_db=tmp_path / "consumer.sqlite3")


def test_policy_table_event_is_preserved_as_non_cycle_work(tmp_path: Path) -> None:
    event = _event(
        "kickoff-b0x-table-kr-2026-09-10",
        disposition="policy_table_build",
        playbook="docs/runbooks/b0x-policy-table-build.md",
    )
    receipt = consume_lane_event(
        event,
        state_db=tmp_path / "consumer.sqlite3",
        received_at=datetime(2026, 9, 10, 7, 45, 59, tzinfo=KST),
    )
    assert receipt.disposition == "queued_policy_table_build"
    assert receipt.cycle_created is False
    assert receipt.additional_kickoffs == 0


@pytest.mark.parametrize(
    ("event_id", "received_at"),
    (
        (
            "kickoff-b0x-nudge-kr-2026-09-09",
            datetime(2026, 9, 10, 9, 5, tzinfo=KST),
        ),
        (
            "kickoff-b0x-nudge-kr-2026-09-10",
            datetime(2026, 9, 10, 9, 6, tzinfo=KST),
        ),
        (
            "kickoff-b0x-nudge-crypto-2026-09-10-T0500",
            datetime(2026, 9, 10, 9, 0, tzinfo=KST),
        ),
        (
            "kickoff-b0x-nudge-crypto-2026-09-10-T2100",
            datetime(2026, 9, 11, 0, 0, tzinfo=KST),
        ),
    ),
)
def test_stale_first_delivery_is_durable_and_never_caught_up(
    tmp_path: Path,
    event_id: str,
    received_at: datetime,
) -> None:
    database = tmp_path / f"{event_id}.sqlite3"

    receipt = consume_lane_event(
        _event(event_id), state_db=database, received_at=received_at
    )

    assert receipt.duplicate is False
    assert receipt.disposition == "preserved_unconsumed_out_of_window"
    assert receipt.cycle_created is False
    assert receipt.additional_kickoffs == 0
    assert event_rows(database)[0]["disposition"] == receipt.disposition


def test_duplicate_after_kst_boundary_keeps_original_composite_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "consumer.sqlite3"
    event = _event("kickoff-b0x-nudge-crypto-2026-09-10-T2100")
    first = consume_lane_event(
        event,
        state_db=database,
        received_at=datetime(2026, 9, 10, 21, 0, 59, tzinfo=KST),
    )
    duplicate = consume_lane_event(
        event,
        state_db=database,
        received_at=datetime(2026, 9, 11, 1, 0, tzinfo=KST),
    )

    assert first.disposition == "queued_cycle"
    assert first.additional_kickoffs == 1
    assert duplicate.duplicate is True
    assert duplicate.disposition == first.disposition
    assert duplicate.additional_kickoffs == 0
    assert len(event_rows(database)) == 1


def test_naive_injected_clock_fails_before_storage_write(tmp_path: Path) -> None:
    database = tmp_path / "consumer.sqlite3"
    with pytest.raises(B0XConsumerContractError, match="timezone-aware"):
        consume_lane_event(
            _event("kickoff-b0x-nudge-kr-2026-09-10"),
            state_db=database,
            received_at=datetime(2026, 9, 10, 9, 5),
        )
    assert not database.exists()


def test_relative_state_database_fails_before_storage_write() -> None:
    with pytest.raises(B0XConsumerContractError, match="must be absolute"):
        consume_lane_event(
            _event("kickoff-b0x-nudge-kr-2026-09-10"),
            state_db=Path("relative/consumer.sqlite3"),
        )


def test_http_ingress_is_wired_but_missing_dispatch_keeps_source_fail_closed() -> None:
    readiness = production_consumer_path_readiness()
    assert readiness.ready is False
    assert readiness.ingress_wired is True
    assert readiness.dispatch_wired is False
    assert readiness.source_may_be_enabled is False
    assert "no approved B0X queued-cycle runner" in readiness.blocker
    assert "not dispatch completion" in readiness.blocker


def test_one_shot_cli_is_default_off_and_does_not_read_artifact(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "must-not-be-read.json"
    state = tmp_path / "consumer.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.b0x_lane_event_consumer",
            "--event-file",
            str(missing),
            "--state-db",
            str(state),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={
            key: value
            for key, value in os.environ.items()
            if key != "B0X_LANE_EVENT_CONSUMER_ENABLED"
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "disabled"
    assert not state.exists()


def test_legacy_direct_artifact_cli_cannot_bypass_binding_and_stable_lock(
    tmp_path: Path,
) -> None:
    event_file = tmp_path / "00001-lane.event.json"
    event_file.write_text(
        json.dumps(_event("kickoff-b0x-nudge-kr-2020-01-02")), encoding="utf-8"
    )
    state = tmp_path / "consumer.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.b0x_lane_event_consumer",
            "--event-file",
            str(event_file),
            "--state-db",
            str(state),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "B0X_LANE_EVENT_CONSUMER_ENABLED": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "blocked"
    assert receipt["consumer_execution_evidence"] is None
    assert "stable-lock HTTP poller" in receipt["reason"]
    assert not state.exists()
