from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.krb1_p0_journal import (
    GENESIS_HASH,
    JournalError,
    append_journal_row,
    build_entry,
    compute_row_hash,
    create_journal,
    verify_journal,
)

pytestmark = pytest.mark.unit

_INITIAL_ROW = {
    "next_stage": "P0_10_KRX_SESSIONS",
    "p0_state": "NOT_STARTED",
    "record_type": "SEAL_INITIALIZED",
    "recorded_date": "2026-07-28",
    "study_id": "KRB1-CSM60-H5-v1",
}
_INITIAL_ROW_HASH = "48335298149e92cdfbbb83f7f604b488074d33122f9c5ad15fe8d42b3925d8b8"
_INITIAL_HEAD = "be117294febe0c8280949a37e35baf95246f527049484b2c20ee890591408229"


def test_initial_row_reproduces_sealed_hashes() -> None:
    entry = build_entry(
        index=1,
        row=_INITIAL_ROW,
        previous_chain_hash=GENESIS_HASH,
    )
    assert compute_row_hash(_INITIAL_ROW) == _INITIAL_ROW_HASH
    assert entry.row_hash == _INITIAL_ROW_HASH
    assert entry.chain_hash == _INITIAL_HEAD


def test_create_verify_and_append_without_rewriting_prefix(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    initial = create_journal(path, _INITIAL_ROW)
    prefix = path.read_bytes()

    appended = append_journal_row(
        path,
        {
            "p0_state": "IN_PROGRESS",
            "record_type": "P0_START_ANCHOR",
            "study_id": "KRB1-CSM60-H5-v1",
        },
    )

    assert initial.index == 1
    assert appended.index == 2
    assert path.read_bytes().startswith(prefix)
    head = verify_journal(path)
    assert head.row_count == 2
    assert head.chain_hash == appended.chain_hash


def test_create_refuses_to_replace_existing_journal(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text("operator-owned", encoding="utf-8")
    with pytest.raises(JournalError, match="cannot create new journal"):
        create_journal(path, _INITIAL_ROW)
    assert path.read_text(encoding="utf-8") == "operator-owned"


def test_create_rejects_a_different_genesis_row(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    with pytest.raises(JournalError, match="does not match the sealed"):
        create_journal(path, {"record_type": "OTHER"})
    assert not path.exists()


@pytest.mark.parametrize("field", ["row_hash", "chain_hash"])
def test_verify_rejects_hash_tampering(tmp_path: Path, field: str) -> None:
    path = tmp_path / "journal.jsonl"
    entry = create_journal(path, _INITIAL_ROW).as_dict()
    entry[field] = "0" * 64
    path.write_text(
        json.dumps(
            entry,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JournalError, match=f"{field} mismatch"):
        verify_journal(path)


def test_verify_rejects_noncanonical_or_truncated_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    entry = build_entry(
        index=1,
        row=_INITIAL_ROW,
        previous_chain_hash=GENESIS_HASH,
    ).as_dict()
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(JournalError, match="end with a newline"):
        verify_journal(path)


def test_append_rejects_nonfinite_values_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    create_journal(path, _INITIAL_ROW)
    before = path.read_bytes()
    with pytest.raises(JournalError, match="not canonical-JSON encodable"):
        append_journal_row(path, {"bad": float("nan")})
    assert path.read_bytes() == before


def test_append_requires_an_initialized_existing_journal(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(JournalError, match="cannot append journal"):
        append_journal_row(missing, {"record_type": "P0_START_ANCHOR"})
    assert not missing.exists()

    empty = tmp_path / "empty.jsonl"
    empty.touch()
    with pytest.raises(JournalError, match="no sealed initial row"):
        append_journal_row(empty, {"record_type": "P0_START_ANCHOR"})
    assert empty.read_bytes() == b""
