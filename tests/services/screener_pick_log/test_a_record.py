"""Task #884 A-record log contract: every considered candidate is a row.

The fanout emits ``candidate_records`` — one per source row that entered the
bounded top-N slice — and the observer writes one ``review.screener_pick_log``
row per record when ``SCREENER_PICK_LOG_ENABLED`` is on.
"""

from __future__ import annotations

import ast
import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CheckConstraint

from app.models.screener_pick_log import ScreenerPickLog
from app.services.screener_pick_log import (
    ScreenerPickRow,
    extract_pick_rows,
    maybe_record_fanout_picks,
)

pytestmark = pytest.mark.unit

_CODE_SHA = "b" * 64
_NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _record(
    source: str,
    symbol: str,
    rank: int,
    *,
    family: str | None = None,
    kind: str = "live",
    admission: str = "admitted",
    admission_reason: str = "family_round_robin_pick",
    selection_seq: int | None = None,
    source_status: str = "ok",
    data_asof: str | None = "2026-09-26",
    source_price: object = "100.10",
    gate_features: dict[str, Any] | None = None,
    selected_via: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "family": family or source.split(":")[0],
        "kind": kind,
        "rank": rank,
        "symbol": symbol,
        "admission": admission,
        "admission_reason": admission_reason,
        "selection_seq": selection_seq,
        "selected_via": selected_via,
        "source_status": source_status,
        "data_asof": data_asof,
        "source_price": source_price,
        "raw_row": {"symbol": symbol, "rank": rank},
        "gate_features": gate_features,
    }


def _a_result(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": True,
        "market": "kr",
        "bounds": {
            "top_n_per_source": 10,
            "top_n_revalidation": 10,
            "revalidation_slot_allocation": "family_round_robin",
        },
        "policy": {
            "policy_version": "test-v1",
            "content_hash": "deadbeef",
            "frozen_gates": {},
        },
        "collection": {
            "collection_version": "funnel-a1",
            "fetched_at": "2026-09-28T09:00:00+00:00",
            "selection": {
                "method": "family_round_robin",
                "family_order": ["rsi", "change_rate"],
                "picks_per_family_round": 1,
                "slots": 10,
            },
            "selected_symbol_order": ["005930"],
            "source_statuses": {"rsi": "ok", "change_rate": "ok"},
        },
        "sources": [
            {
                "source": "rsi",
                "family": "rsi",
                "kind": "live",
                "metadata": {
                    "request": {
                        "market": "kr",
                        "sort_by": "rsi",
                        "sort_order": "asc",
                        "limit": 10,
                    }
                },
            },
            {
                "source": "change_rate",
                "family": "change_rate",
                "kind": "live",
                "metadata": {
                    "request": {
                        "market": "kr",
                        "sort_by": "change_rate",
                        "sort_order": "asc",
                        "limit": 10,
                    }
                },
            },
            {
                "source": "snapshot_support_flow:support_proximity",
                "family": "snapshot_support_flow",
                "kind": "snapshot",
                "metadata": {"preset": "support_proximity"},
            },
        ],
        "candidates": [],
        "candidate_records": records,
    }


def test_every_considered_candidate_becomes_a_row() -> None:
    """Admitted and rejected candidates both produce exactly one row."""

    result = _a_result(
        [
            _record("rsi", "005930", 1, selection_seq=1),
            _record("rsi", "000660", 2, selection_seq=3),
            _record(
                "rsi",
                "035420",
                3,
                admission="not_admitted",
                admission_reason="slot_pool_exhausted",
            ),
            _record(
                "change_rate",
                "005930",
                1,
                admission="admitted",
                admission_reason="duplicate_symbol_admitted_via_other_source",
                selected_via={"source": "rsi", "family": "rsi", "rank": 1},
            ),
            _record(
                "snapshot_support_flow:support_proximity",
                "STALE.1",
                1,
                kind="snapshot",
                admission="dropped_preselection",
                admission_reason="snapshot_more_than_one_session_stale",
                source_status="stale_dropped",
            ),
        ]
    )
    rows = extract_pick_rows(result, now=_NOW, code_sha256=_CODE_SHA)
    assert len(rows) == 5
    by_key = {(row.source, row.symbol): row for row in rows}
    assert set(by_key) == {
        ("rsi", "005930"),
        ("rsi", "000660"),
        ("rsi", "035420"),
        ("change_rate", "005930"),
        ("snapshot_support_flow:support_proximity", "STALE.1"),
    }
    rejected = by_key[("rsi", "035420")]
    assert rejected.admission == "not_admitted"
    assert rejected.admission_reason == "slot_pool_exhausted"
    assert rejected.selection_seq is None
    duplicate = by_key[("change_rate", "005930")]
    assert duplicate.admission_reason == ("duplicate_symbol_admitted_via_other_source")
    assert duplicate.source_params["selected_via"] == {
        "source": "rsi",
        "family": "rsi",
        "rank": 1,
    }
    dropped = by_key[("snapshot_support_flow:support_proximity", "STALE.1")]
    assert dropped.admission == "dropped_preselection"
    assert dropped.admission_reason == "snapshot_more_than_one_session_stale"
    assert dropped.source_status == "stale_dropped"
    assert dropped.source_preset == "support_proximity"


def test_row_field_types_and_collection_metadata() -> None:
    rows = extract_pick_rows(
        _a_result(
            [
                _record(
                    "rsi",
                    "005930",
                    1,
                    selection_seq=1,
                    gate_features={"rsi_14": 31.5, "upside_gte_25": True},
                )
            ]
        ),
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.collection_version == "funnel-a1"
    assert row.admission == "admitted"
    assert row.admission_reason == "family_round_robin_pick"
    assert row.selection_seq == 1
    assert isinstance(row.rank, int)
    assert isinstance(row.selection_seq, int)
    assert row.source_status == "ok"
    assert row.data_asof == "2026-09-26"
    assert row.fetched_at == datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    assert row.raw_row == {"symbol": "005930", "rank": 1}
    assert row.gate_features == {"rsi_14": 31.5, "upside_gte_25": True}
    assert row.decision_price_text == "100.10"
    assert isinstance(row.decision_price_text, str)
    context = row.call_context
    assert context is not None
    assert context["policy"]["policy_version"] == "test-v1"
    assert context["selection"]["method"] == "family_round_robin"
    assert context["source_statuses"]["rsi"] == "ok"


def test_duplicate_source_symbol_pairs_emit_once() -> None:
    """The uniqueness key is (call_id, source, symbol)."""

    result = _a_result(
        [
            _record("rsi", "005930", 1, selection_seq=1),
            _record("rsi", "005930", 4),
        ]
    )
    rows = extract_pick_rows(result, now=_NOW, code_sha256=_CODE_SHA)
    assert len(rows) == 1
    assert rows[0].rank == 1


@pytest.mark.asyncio
async def test_gate_off_writes_nothing_for_a_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCREENER_PICK_LOG_ENABLED", raising=False)
    writes: list[list[ScreenerPickRow]] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(list(rows))

    await maybe_record_fanout_picks(
        _a_result([_record("rsi", "005930", 1, selection_seq=1)]),
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert writes == []


@pytest.mark.asyncio
async def test_gate_on_writes_one_row_per_candidate_record() -> None:
    writes: list[list[ScreenerPickRow]] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(list(rows))

    await maybe_record_fanout_picks(
        _a_result(
            [
                _record("rsi", "005930", 1, selection_seq=1),
                _record(
                    "rsi",
                    "000660",
                    2,
                    admission="not_admitted",
                    admission_reason="slot_pool_exhausted",
                ),
                _record(
                    "change_rate",
                    "005930",
                    1,
                    admission="admitted",
                    admission_reason=("duplicate_symbol_admitted_via_other_source"),
                    selected_via={"source": "rsi", "family": "rsi", "rank": 1},
                ),
            ]
        ),
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert len(writes) == 1
    assert len(writes[0]) == 3
    assert {row.admission for row in writes[0]} == {"admitted", "not_admitted"}


@pytest.mark.asyncio
async def test_a_record_writer_errors_stay_fail_open() -> None:
    async def writer(_rows: list[ScreenerPickRow]) -> None:
        raise RuntimeError("db down")

    await maybe_record_fanout_picks(
        _a_result([_record("rsi", "005930", 1, selection_seq=1)]),
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )


@pytest.mark.asyncio
async def test_a_record_result_is_not_mutated() -> None:
    result = _a_result([_record("rsi", "005930", 1, selection_seq=1)])
    snapshot = copy.deepcopy(result)

    async def writer(rows: list[ScreenerPickRow]) -> None:
        assert len(rows) == 1

    await maybe_record_fanout_picks(
        result,
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert result == snapshot


def test_legacy_result_without_candidate_records_keeps_new_fields_null() -> None:
    """Pre-A-record payloads still project; the new columns stay nullable."""

    rows = extract_pick_rows(
        {
            "success": True,
            "market": "kr",
            "bounds": {"top_n_per_source": 10},
            "sources": [
                {
                    "source": "rsi",
                    "family": "rsi",
                    "kind": "live",
                    "metadata": {"request": {"limit": 10}},
                }
            ],
            "candidates": [
                {
                    "symbol": "005930",
                    "matched_sources": ["rsi"],
                    "source_rows": [{"source": "rsi", "family": "rsi", "rank": 1}],
                }
            ],
        },
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.collection_version is None
    assert row.admission is None
    assert row.admission_reason is None
    assert row.selection_seq is None
    assert row.source_status is None
    assert row.fetched_at is None
    assert row.raw_row is None
    assert row.gate_features is None
    assert row.call_context is None


def _literal_tuple(tree: ast.Module, name: str) -> set[str]:
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.Tuple)
        ):
            return {
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return set()


def test_model_and_migration_cover_the_same_a_record_columns() -> None:
    """Schema parity: the additive migration must mirror the ORM columns."""

    expected_columns = {
        "collection_version",
        "admission",
        "admission_reason",
        "selection_seq",
        "source_status",
        "data_asof",
        "fetched_at",
        "raw_row",
        "gate_features",
        "call_context",
    }
    expected_checks = {
        "admission_vocabulary",
        "admission_reason_nonempty",
        "selection_seq_positive",
        "source_status_vocabulary",
        "collection_version_nonempty",
        "raw_row_object",
        "gate_features_object",
        "call_context_object",
    }
    model_columns = set(ScreenerPickLog.__table__.columns.keys())
    assert expected_columns <= model_columns
    model_check_names = {
        constraint.name
        for constraint in ScreenerPickLog.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    # The Base naming convention prefixes check names on the ORM side; the
    # migration names them literally, matching the table's original DDL.
    assert {
        f"ck_screener_pick_log_{name}" for name in expected_checks
    } <= model_check_names

    migration_path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "20260928_884_fanout_a_record.py"
    )
    tree = ast.parse(migration_path.read_text())

    # upgrade(): every new column arrives via sa.Column("<name>", ...).
    added: set[str] = set()
    created_checks: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if func_name == "Column" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                added.add(arg.value)
        elif func_name == "create_check_constraint" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                created_checks.add(arg.value)
    assert expected_columns <= added
    assert created_checks == expected_checks

    # downgrade(): the loop tuples list exactly what upgrade() created.
    assert _literal_tuple(tree, "_NEW_COLUMNS") == expected_columns
    assert _literal_tuple(tree, "_NEW_CHECKS") == expected_checks
    assert "def downgrade()" in migration_path.read_text()
