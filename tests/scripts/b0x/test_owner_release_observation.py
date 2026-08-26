"""Owner-release evidence is bounded, terminal, and outside send authority."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import scripts.b0x.ledger as ledger_module
from scripts.b0x.kr import kiwoom_cycle
from scripts.b0x.kr.kiwoom_coordination import (
    production_kiwoom_coordination_factory,
    resolve_kiwoom_lane_entry,
)
from scripts.b0x.ledger import writer_lock

pytestmark = pytest.mark.unit


def test_owner_release_record_has_the_closed_field_spec(tmp_path: Path) -> None:
    lane = "sidecar"

    with writer_lock(lane=lane, root=tmp_path):
        pass

    path = tmp_path / lane / ledger_module.OWNER_RELEASE_LOG_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert len(lines[0].encode("utf-8")) <= ledger_module.MAX_OWNER_RELEASE_RECORD_BYTES
    record = json.loads(lines[0])
    assert set(record) == {
        "event",
        "owner",
        "monotonic_ts_ns",
        "wall_clock_ts",
        "release_reason",
        "lock_correlation",
    }
    assert record["event"] == "owner_release"
    assert record["owner"] == ledger_module.WRITER_LOCK_RECOVERY_OWNER
    assert type(record["monotonic_ts_ns"]) is int
    assert type(record["wall_clock_ts"]) is str
    parsed_wall_clock_ts = dt.datetime.fromisoformat(record["wall_clock_ts"])
    assert parsed_wall_clock_ts.tzinfo is not None
    assert parsed_wall_clock_ts.utcoffset() is not None
    assert record["release_reason"] == "scope_exit"
    assert record["release_reason"] in ledger_module.OWNER_RELEASE_REASONS
    assert set(record["lock_correlation"]) == {"claim_id", "lane", "lock_path"}
    assert record["lock_correlation"]["lane"] == lane
    assert record["lock_correlation"]["lock_path"] == str(
        tmp_path / f".{lane}.writer.lock"
    )

    with writer_lock(lane=lane, root=tmp_path):
        pass
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["monotonic_ts_ns"] <= records[1]["monotonic_ts_ns"]


def test_owner_release_is_terminal_on_exception_and_close_is_still_guaranteed(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="body failure"):
        with writer_lock(lane="sidecar", root=tmp_path):
            raise RuntimeError("body failure")

    path = tmp_path / "sidecar" / ledger_module.OWNER_RELEASE_LOG_NAME
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["release_reason"] == "scope_exception"

    # The original LOCK_UN -> close finally path remains usable.
    with writer_lock(lane="sidecar", root=tmp_path):
        pass
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_owner_release_closes_handle_before_observation_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_flock = ledger_module.fcntl.flock
    real_close = ledger_module.os.close
    real_record = ledger_module.ObservationLedger.record_owner_release

    def tracked_flock(handle: int, operation: int) -> None:
        if operation == ledger_module.fcntl.LOCK_UN:
            events.append("unlock")
        real_flock(handle, operation)

    def tracked_record(self: object, **kwargs: object) -> None:
        events.append("observation")
        real_record(self, **kwargs)  # type: ignore[arg-type]

    def tracked_close(handle: int) -> None:
        events.append("close")
        real_close(handle)

    monkeypatch.setattr(ledger_module.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", tracked_record
    )
    monkeypatch.setattr(ledger_module.os, "close", tracked_close)

    with writer_lock(lane="sidecar", root=tmp_path):
        pass

    assert events == ["unlock", "close", "observation"]


def test_owner_release_close_failure_is_recorded_with_the_correct_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = ledger_module.os.close
    close_calls: list[int] = []

    def fail_close(handle: int) -> None:
        close_calls.append(handle)
        raise OSError("close failed")

    monkeypatch.setattr(ledger_module.os, "close", fail_close)
    with pytest.raises(OSError, match="close failed"):
        with writer_lock(lane="sidecar", root=tmp_path):
            pass

    path = tmp_path / "sidecar" / ledger_module.OWNER_RELEASE_LOG_NAME
    record = json.loads(path.read_text(encoding="utf-8"))
    assert close_calls
    assert record["release_reason"] == "close_exception"

    # The failed close was deliberately injected; restore it before proving
    # the lock path itself remains usable.
    monkeypatch.setattr(ledger_module.os, "close", real_close)
    with writer_lock(lane="sidecar", root=tmp_path):
        pass


def test_owner_release_closes_handle_when_observation_raises_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = ledger_module.os.close
    close_calls: list[int] = []
    real_record = ledger_module.ObservationLedger.record_owner_release

    def tracked_close(handle: int) -> None:
        close_calls.append(handle)
        real_close(handle)

    def interrupt_observation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(ledger_module.os, "close", tracked_close)
    monkeypatch.setattr(
        ledger_module.ObservationLedger,
        "record_owner_release",
        interrupt_observation,
    )

    with pytest.raises(KeyboardInterrupt):
        with writer_lock(lane="sidecar", root=tmp_path):
            pass

    assert len(close_calls) == 1

    # A closed descriptor also means the same lane can be acquired again.
    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", real_record
    )
    with writer_lock(lane="sidecar", root=tmp_path):
        pass


AUTHORIZATION_NOW = dt.datetime(2026, 8, 12, 3, 0, tzinfo=dt.UTC)


async def _run_production_authorization_path(root: Path) -> dict[str, object]:
    """Call the real Kiwoom cycle authorization boundary without broker I/O."""

    entry = resolve_kiwoom_lane_entry()
    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=AUTHORIZATION_NOW,
        out_dir=root,
        confirm=True,
        ordering=True,
        coordination_factory=production_kiwoom_coordination_factory(),
        coordination_entry=entry,
    )
    return {
        "authorization": outcome.record["coordination"],
        "order_path": {
            "zero_order_reason": outcome.zero_order_reason,
            "orders": outcome.record["orders"],
            "planned": outcome.record["planned"],
            "blocked": outcome.record["blocked"],
            "submitted": outcome.record["submitted"],
        },
    }


@pytest.mark.asyncio
async def test_isolation_observation_removal_does_not_change_production_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await _run_production_authorization_path(tmp_path / "baseline")

    def remove_observation(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", remove_observation
    )
    without_observation = await _run_production_authorization_path(
        tmp_path / "observation-removed"
    )

    assert without_observation == baseline


def _write_hostile_observation(root: Path) -> None:
    lane = kiwoom_cycle.LANE
    journal = root / lane / ledger_module.OWNER_RELEASE_LOG_NAME
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "event": ledger_module.OWNER_RELEASE_EVENT,
                "owner": "hostile-observer",
                "monotonic_ts_ns": 1,
                "wall_clock_ts": "2026-08-12T03:00:00+00:00",
                "release_reason": "scope_exit",
                "lock_correlation": {
                    "claim_id": "hostile-claim",
                    "lane": lane,
                    "lock_path": "/hostile/path",
                },
                "authorizes_send": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_isolation_hostile_observation_does_not_change_production_authorization(
    tmp_path: Path,
) -> None:
    baseline = await _run_production_authorization_path(tmp_path / "baseline")
    hostile_root = tmp_path / "hostile-observation"
    _write_hostile_observation(hostile_root)
    with_hostile_observation = await _run_production_authorization_path(hostile_root)

    assert with_hostile_observation == baseline


@pytest.mark.asyncio
async def test_isolation_observation_failure_does_not_change_production_order_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await _run_production_authorization_path(tmp_path / "baseline")

    def fail_observation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("observation storage unavailable")

    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", fail_observation
    )
    with_failed_observation = await _run_production_authorization_path(
        tmp_path / "observation-failure"
    )

    assert with_failed_observation == baseline
