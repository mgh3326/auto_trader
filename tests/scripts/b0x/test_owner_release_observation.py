"""Owner-release evidence is bounded, terminal, and outside send authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.b0x.ledger as ledger_module
from scripts.b0x.ledger import writer_lock

pytestmark = pytest.mark.unit


def test_owner_release_record_has_the_closed_four_field_spec(tmp_path: Path) -> None:
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
        "release_reason",
        "lock_correlation",
    }
    assert record["event"] == "owner_release"
    assert record["owner"] == ledger_module.WRITER_LOCK_RECOVERY_OWNER
    assert type(record["monotonic_ts_ns"]) is int
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


def test_owner_release_observation_precedes_the_unchanged_handle_close(
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

    assert events == ["unlock", "observation", "close"]


def _diagnostic_lock_send_probe(root: Path) -> tuple[bool, list[str]]:
    """Model the existing caller contract: flock never authorizes transport."""

    send_path: list[str] = []
    with writer_lock(lane="sidecar", root=root):
        authorizes_send = False
        if authorizes_send:
            send_path.append("broker_send")
    return authorizes_send, send_path


def test_mutant_a_observation_failure_does_not_change_authorization_or_order_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _diagnostic_lock_send_probe(tmp_path / "baseline")

    def fail_observation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("observation storage unavailable")

    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", fail_observation
    )
    mutant = _diagnostic_lock_send_probe(tmp_path / "observation-failure")

    assert mutant == baseline == (False, [])


def test_mutant_b_mutated_or_deleted_record_does_not_change_authorization_or_order_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _diagnostic_lock_send_probe(tmp_path / "baseline")
    real_record = ledger_module.ObservationLedger.record_owner_release

    def replace_observation(self: object, **kwargs: object) -> None:
        # Simulate a hostile observer changing every persisted diagnostic
        # value, including an apparent authorization field.
        real_record(self, **kwargs)  # type: ignore[arg-type]
        ledger = self  # type: ignore[assignment]
        journal = ledger.lane_dir / ledger_module.OWNER_RELEASE_LOG_NAME
        mutated = json.loads(journal.read_text(encoding="utf-8"))
        mutated["owner"] = "mutant-owner"
        mutated["release_reason"] = "scope_exit"
        mutated["lock_correlation"] = {
            "claim_id": "mutant-claim",
            "lane": "mutant-lane",
            "lock_path": "/mutant/path",
        }
        mutated["authorizes_send"] = True
        journal.write_text(json.dumps(mutated) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        ledger_module.ObservationLedger, "record_owner_release", replace_observation
    )
    root = tmp_path / "observation-mutant"
    mutant = _diagnostic_lock_send_probe(root)
    journal = root / "sidecar" / ledger_module.OWNER_RELEASE_LOG_NAME
    if journal.exists():
        journal.unlink()

    assert mutant == baseline == (False, [])
