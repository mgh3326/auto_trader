"""hk 6508: the single writer — same-day rerun writes zero rows (fake session)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.fnguide import FnguideSnapshot
from app.services.fnguide_consensus.writer import FnguideSnapshotWriter
from tests.services.fnguide_consensus._helpers import parse_fixture

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@dataclass
class FakeSession:
    """Records statements; the snapshot ON CONFLICT answers from ``existing`` keys."""

    existing: set[tuple[str, str, object]] = field(default_factory=set)
    tables: list[str] = field(default_factory=list)
    next_id: int = 1

    async def execute(self, stmt):
        name = stmt.table.name
        self.tables.append(name)
        if name == "fnguide_snapshots":
            params = stmt.compile().params
            key = (params["symbol"], params["page"], params["observed_date"])
            if key in self.existing:
                return _Result(None)
            self.existing.add(key)
            self.next_id += 1
            return _Result(self.next_id)
        return _Result(None)


def test_snapshot_insert_is_on_conflict_do_nothing_and_returns_id() -> None:
    stmt = (
        pg_insert(FnguideSnapshot)
        .values(symbol="005930", page="Consensus")
        .on_conflict_do_nothing(constraint="uq_fnguide_snapshots_symbol_page_date")
        .returning(FnguideSnapshot.id)
    )
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert (
        "ON CONFLICT ON CONSTRAINT uq_fnguide_snapshots_symbol_page_date DO NOTHING"
        in compiled
    )
    assert "RETURNING fnguide_snapshots.id" in compiled


@pytest.mark.asyncio
async def test_first_write_inserts_snapshot_values_and_reports() -> None:
    session = FakeSession()
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    outcome = await FnguideSnapshotWriter(session).write_page(parsed)

    assert outcome.inserted is True
    assert outcome.value_rows == parsed.value_count > 0
    assert outcome.report_rows == parsed.report_count > 0
    assert session.tables.count("fnguide_values") >= 1
    assert session.tables.count("fnguide_reports") >= 1


@pytest.mark.asyncio
async def test_same_day_rerun_writes_zero_new_rows() -> None:
    session = FakeSession()
    parsed = parse_fixture("005930_consensus.html", symbol="005930")
    writer = FnguideSnapshotWriter(session)

    await writer.write_page(parsed)
    session.tables.clear()
    second = await writer.write_page(parsed)

    assert second.inserted is False
    assert second.value_rows == 0
    assert second.report_rows == 0
    assert session.tables == ["fnguide_snapshots"]


@pytest.mark.asyncio
async def test_no_values_page_is_stored_with_its_reason() -> None:
    session = FakeSession()
    parsed = parse_fixture("028300_consensus.html", symbol="028300")

    outcome = await FnguideSnapshotWriter(session).write_page(parsed)

    assert outcome.inserted is True
    assert parsed.reason == "all_consensus_values_null"
    assert all(v.section != "consensus" for v in parsed.values)


def test_only_the_writer_module_inserts_into_fnguide_tables() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "writer.py" and path.parent.name == "fnguide_consensus":
            continue
        text = path.read_text(encoding="utf-8")
        if (
            "insert(FnguideSnapshot)" in text
            or "insert(FnguideValue)" in text
            or "insert(FnguideReport)" in text
        ):
            offenders.append(str(path))
        if (
            "pg_insert(FnguideSnapshot)" in text
            or "pg_insert(FnguideValue)" in text
            or "pg_insert(FnguideReport)" in text
        ):
            offenders.append(str(path))
    assert offenders == []
