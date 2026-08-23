"""Observation-only screener pick log: gate, fail-open, exact decimal."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.services.screener_pick_log import (
    ScreenerPickRow,
    exact_decimal_text,
    extract_pick_rows,
    maybe_record_fanout_picks,
)

pytestmark = pytest.mark.unit

_CODE_SHA = "a" * 64
_NOW = datetime(2026, 8, 23, 13, 30, tzinfo=UTC)


def _fanout_result(*, price: object = "100.10") -> dict[str, Any]:
    return {
        "success": True,
        "market": "kr",
        "bounds": {"top_n_per_source": 10},
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
            }
        ],
        "candidates": [
            {
                "symbol": "005930",
                "matched_sources": ["rsi"],
                "source_rows": [
                    {
                        "source": "rsi",
                        "family": "rsi",
                        "kind": "live",
                        "rank": 1,
                    }
                ],
                "funnel": {"base_eligibility": {"current_price": price}},
            }
        ],
    }


def test_exact_decimal_text_preserves_scale() -> None:
    assert exact_decimal_text("100.10") == "100.10"
    assert exact_decimal_text(Decimal("100.10")) == "100.10"
    assert exact_decimal_text(100) == "100"
    with pytest.raises(TypeError, match="float"):
        exact_decimal_text(100.10)
    with pytest.raises(TypeError, match="bool"):
        exact_decimal_text(True)


def test_extract_uses_exact_decimal_string_not_float() -> None:
    rows = extract_pick_rows(
        _fanout_result(price="100.10"),
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.decision_price_text == "100.10"
    assert isinstance(row.decision_price_text, str)
    assert row.market == "kr"
    assert row.source == "rsi"
    assert row.symbol == "005930"
    assert row.rank == 1
    assert row.source_sort_by == "rsi"
    assert row.source_sort_order == "asc"
    assert row.source_limit == 10
    assert row.recorded_at_kst.endswith("+09:00")
    assert row.fanout_code_sha256 == _CODE_SHA
    UUID(str(row.call_id))


@pytest.mark.asyncio
async def test_gate_off_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCREENER_PICK_LOG_ENABLED", raising=False)
    writes: list[list[ScreenerPickRow]] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(list(rows))

    await maybe_record_fanout_picks(
        _fanout_result(),
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert writes == []


@pytest.mark.asyncio
async def test_gate_on_writes_extracted_rows() -> None:
    writes: list[list[ScreenerPickRow]] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(list(rows))

    await maybe_record_fanout_picks(
        _fanout_result(price="100.10"),
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert len(writes) == 1
    assert writes[0][0].decision_price_text == "100.10"


@pytest.mark.asyncio
async def test_fail_open_swallows_writer_errors() -> None:
    async def writer(_rows: list[ScreenerPickRow]) -> None:
        raise RuntimeError("db down")

    await maybe_record_fanout_picks(
        _fanout_result(),
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )


@pytest.mark.asyncio
async def test_fanout_return_is_unchanged() -> None:
    original = _fanout_result(price="100.10")
    snapshot = copy.deepcopy(original)
    writes: list[int] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(len(rows))
        rows[0].source_params["mutated"] = True  # type: ignore[index]

    await maybe_record_fanout_picks(
        original,
        enabled=True,
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert writes == [1]
    assert original == snapshot


@pytest.mark.asyncio
async def test_gate_off_via_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENER_PICK_LOG_ENABLED", "false")
    writes: list[list[ScreenerPickRow]] = []

    async def writer(rows: list[ScreenerPickRow]) -> None:
        writes.append(list(rows))

    await maybe_record_fanout_picks(
        _fanout_result(),
        write_rows=writer,
        now=_NOW,
        code_sha256=_CODE_SHA,
    )
    assert writes == []
