"""Fail-closed authoritative evidence boundary tests."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.models.paper_cohort import PaperCohortVenueIntent
from app.services.paper_evaluation.contracts import EvaluationConfigError
from app.services.paper_evaluation.evidence import (
    AuthoritativeEvidenceReader,
    _alpaca_filled_at,
    _quote_mark,
)

pytestmark = pytest.mark.unit


def test_alpaca_fill_timestamp_requires_native_broker_evidence() -> None:
    assert _alpaca_filled_at({"submitted_at": "2026-01-01T00:00:00Z"}) is None
    assert _alpaca_filled_at({"updated_at": "2026-01-01T00:00:00Z"}) is None


def test_alpaca_fill_timestamp_accepts_nested_native_timestamp() -> None:
    assert _alpaca_filled_at(
        {"order": {"filled_at": "2026-01-01T00:00:00Z"}}
    ) == datetime(2026, 1, 1, tzinfo=UTC)


def test_alpaca_mark_uses_native_usd_symbol_without_usdt_peg() -> None:
    intent = PaperCohortVenueIntent(
        venue="alpaca",
        symbol="BTCUSDT",
        venue_quote_evidence={
            "venue": "alpaca",
            "symbol": "BTC/USD",
            "bid_price": "99",
            "ask_price": "101",
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
    )
    mark = _quote_mark(intent)
    assert mark.symbol == "BTC/USD"
    assert str(mark.price) == "100"


@pytest.mark.asyncio
async def test_reader_rejects_ambiguous_or_missing_assignment_identity() -> None:
    reader = AuthoritativeEvidenceReader(AsyncMock())  # type: ignore[arg-type]
    with pytest.raises(EvaluationConfigError) as exc:
        await reader.load(evaluated_at=datetime.now(UTC))
    assert exc.value.reason_code == "invalid_evaluation_identity"


def test_native_query_is_assignment_scoped_and_never_cohort_correlation_scoped() -> (
    None
):
    source = inspect.getsource(AuthoritativeEvidenceReader._load_native)
    assert "PaperRunOrderLink.assignment_id == assignment.assignment_id" in source
    assert "PaperRunOrderLink.cohort_id == assignment.cohort_id" in source
    assert "lifecycle_correlation_id" not in source
    assert "submitted_at" not in source
    assert "updated_at" not in source
