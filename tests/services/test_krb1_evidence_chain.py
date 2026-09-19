"""Append-only hash-chained gate evidence streams (ROB-1172 storage contract)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.services.krb1_evidence_chain import (
    STREAM_INITIALIZED_RECORD_TYPE,
    EvidenceChainError,
    append_record,
    genesis_row,
    open_stream,
    read_records,
    verify_stream,
)
from app.services.krb1_p0_journal import (
    SEALED_INITIAL_HEAD,
    SEALED_INITIAL_ROW_HASH,
)

pytestmark = pytest.mark.unit

STREAM = "krb1.p0_3.test_stream"
KST = dt.timezone(dt.timedelta(hours=9))


def _row(minute: int, **extra: object) -> dict[str, object]:
    return {
        "recorded_at": dt.datetime(2026, 7, 29, 17, minute, tzinfo=KST).isoformat(),
        "record_type": "TEST_RECORD",
        "payload": {"minute": minute},
        **extra,
    }


def test_open_stream_creates_deterministic_genesis(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"

    head = open_stream(path, stream_id=STREAM)

    assert head.record_count == 1
    records = read_records(path, stream_id=STREAM)
    assert records[0].record_type == STREAM_INITIALIZED_RECORD_TYPE
    assert records[0].row == genesis_row(STREAM)
    assert open_stream(path, stream_id=STREAM).chain_hash == head.chain_hash


def test_genesis_is_not_the_sealed_campaign_anchor(tmp_path: Path) -> None:
    """Gate evidence must never share the sealed KR-B1 journal anchor."""
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)

    record = read_records(path, stream_id=STREAM)[0]
    assert record.row_hash != SEALED_INITIAL_ROW_HASH
    assert record.chain_hash != SEALED_INITIAL_HEAD


def test_append_preserves_prefix_bytes_and_chains(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    prefix = path.read_bytes()

    first = append_record(
        path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(1)
    )
    second = append_record(
        path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(2)
    )

    assert first.index == 2
    assert second.index == 3
    assert path.read_bytes().startswith(prefix)
    assert verify_stream(path, stream_id=STREAM).record_count == 3


def test_tampering_with_a_row_breaks_verification(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    append_record(path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(1))

    lines = path.read_bytes().splitlines()
    envelope = json.loads(lines[1])
    envelope["row"]["payload"] = {"minute": 99}
    lines[1] = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    path.write_bytes(b"\n".join(lines) + b"\n")

    with pytest.raises(EvidenceChainError):
        verify_stream(path, stream_id=STREAM)


def test_truncating_history_breaks_verification(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    append_record(path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(1))
    lines = path.read_bytes().splitlines()
    path.write_bytes(lines[1] + b"\n")

    with pytest.raises(EvidenceChainError):
        verify_stream(path, stream_id=STREAM)


def test_recorded_at_cannot_regress(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    append_record(path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(30))

    with pytest.raises(EvidenceChainError):
        append_record(path, stream_id=STREAM, record_type="TEST_RECORD", row=_row(10))


def test_naive_recorded_at_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)

    with pytest.raises(EvidenceChainError):
        append_record(
            path,
            stream_id=STREAM,
            record_type="TEST_RECORD",
            row={"recorded_at": "2026-07-29T17:00:00", "record_type": "TEST_RECORD"},
        )


def test_cross_stream_read_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)

    with pytest.raises(EvidenceChainError):
        verify_stream(path, stream_id="krb1.p0_3.other_stream")


def test_append_requires_an_initialized_stream(tmp_path: Path) -> None:
    with pytest.raises(EvidenceChainError):
        append_record(
            tmp_path / "missing.jsonl",
            stream_id=STREAM,
            record_type="TEST_RECORD",
            row=_row(1),
        )


@pytest.mark.parametrize("stream_id", ["ab", "UPPER", "has space", "x" * 65])
def test_invalid_stream_ids_are_rejected(tmp_path: Path, stream_id: str) -> None:
    with pytest.raises(EvidenceChainError):
        open_stream(tmp_path / "stream.jsonl", stream_id=stream_id)


@pytest.mark.parametrize("record_type", ["ab", "lower_case", "HAS SPACE"])
def test_invalid_record_types_are_rejected(tmp_path: Path, record_type: str) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    with pytest.raises(EvidenceChainError):
        append_record(path, stream_id=STREAM, record_type=record_type, row=_row(1))


def test_non_finite_numbers_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    open_stream(path, stream_id=STREAM)
    with pytest.raises(EvidenceChainError):
        append_record(
            path,
            stream_id=STREAM,
            record_type="TEST_RECORD",
            row={"value": float("nan")},
        )
