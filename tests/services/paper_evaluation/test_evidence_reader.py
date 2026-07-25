"""Fail-closed authoritative evidence boundary tests."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_evaluation.contracts import EvaluationConfigError
from app.services.paper_evaluation.evidence import (
    AuthoritativeEvidenceReader,
    _alpaca_filled_at,
    _binance_fill_values,
    _quote_mark,
    _recompute_signal,
    _snapshot_matches_row,
)

pytestmark = pytest.mark.unit


def test_alpaca_fill_timestamp_requires_native_broker_evidence() -> None:
    assert _alpaca_filled_at({"submitted_at": "2026-01-01T00:00:00Z"}) is None
    assert _alpaca_filled_at({"updated_at": "2026-01-01T00:00:00Z"}) is None


def test_alpaca_fill_timestamp_accepts_nested_native_timestamp() -> None:
    assert _alpaca_filled_at(
        {"order": {"filled_at": "2026-01-01T00:00:00Z"}}
    ) == datetime(2026, 1, 1, tzinfo=UTC)


def test_binance_fill_requires_native_actuals_without_requested_value_fallback() -> (
    None
):
    row = type(
        "LedgerRow",
        (),
        {
            "id": 1,
            "qty": Decimal("2"),
            "price": Decimal("100"),
            "extra_metadata": {},
        },
    )()
    with pytest.raises(EvaluationConfigError) as exc:
        _binance_fill_values(row)
    assert exc.value.reason_code == "missing_evidence"

    row.extra_metadata = {
        "filled_qty": "1",
        "filled_avg_price": "110",
        "fee_usdt": "0.11",
    }
    assert _binance_fill_values(row) == (
        Decimal("1"),
        Decimal("110"),
        Decimal("0.11"),
        True,
    )


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
    assert "PaperRunOrderLink.created_at >= start" not in source
    assert "start <= filled_at <= end" in source


def test_reader_passes_authoritative_cohort_into_shadow_lineage() -> None:
    source = inspect.getsource(AuthoritativeEvidenceReader.load)
    assert "self._load_shadow(" in source
    assert "cohort=cohort" in source


def _canonical_payload() -> CanonicalSnapshotPayload:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    closed = opened + timedelta(minutes=1) - timedelta(milliseconds=1)
    symbol_rows = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        symbol_rows.append(
            {
                "symbol": symbol,
                "candles": [
                    {
                        "open_time": opened,
                        "close_time": closed,
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": "100",
                        "base_volume": "1",
                        "quote_volume": "100",
                        "trade_count": 1,
                        "taker_buy_base_volume": "0.5",
                        "taker_buy_quote_volume": "50",
                    }
                ],
                "ticker": {
                    "bid_price": "99",
                    "bid_qty": "1",
                    "ask_price": "101",
                    "ask_qty": "1",
                    "fetched_at": closed,
                },
            }
        )
    payload = CanonicalSnapshotPayload.model_validate(
        {
            "schema_id": "canonical_market_snapshot.v1",
            "snapshot_id": "snapshot-1",
            "cohort_id": "cohort-1",
            "run_id": "run-1",
            "round_decision_id": "round-1",
            "source": "binance_public_spot",
            "host": "https://api.binance.com",
            "interval": "1m",
            "required_lookback": 1,
            "max_capture_skew_ms": 5000,
            "max_ticker_age_ms": 5000,
            "capture_started_at": opened,
            "capture_completed_at": closed,
            "symbols": symbol_rows,
            "content_hash": "0" * 64,
        }
    )
    return payload.model_copy(
        update={"content_hash": payload.recomputed_content_hash()}
    )


def test_shadow_snapshot_and_signal_are_recomputed_from_authoritative_identity() -> (
    None
):
    payload = _canonical_payload()
    snapshot = CanonicalMarketSnapshot(
        snapshot_id=payload.snapshot_id,
        cohort_id=payload.cohort_id,
        run_id=payload.run_id,
        round_decision_id=payload.round_decision_id,
        schema_id=payload.schema_id,
        source=payload.source,
        host=payload.host,
        interval=payload.interval,
        required_lookback=payload.required_lookback,
        max_capture_skew_ms=payload.max_capture_skew_ms,
        max_ticker_age_ms=payload.max_ticker_age_ms,
        capture_started_at=payload.capture_started_at,
        capture_completed_at=payload.capture_completed_at,
        payload=payload.model_dump(mode="json"),
        content_hash=payload.content_hash,
    )
    assert _snapshot_matches_row(payload, snapshot)
    snapshot.cohort_id = "cross-wired"
    assert not _snapshot_matches_row(payload, snapshot)
    snapshot.cohort_id = payload.cohort_id

    assignment = PaperValidationCohortAssignment(
        assignment_id="assignment-1",
        cohort_id=payload.cohort_id,
        experiment_id="e" * 64,
        strategy_version_id="strategy-v1",
        strategy_hash="s" * 64,
        config_hash="c" * 64,
        policy_hash="p" * 64,
        target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
    )
    cohort = PaperValidationCohort(
        cohort_id=payload.cohort_id, capital_notional_usd=Decimal("100")
    )
    decision = PaperCohortDecision(symbol="BTCUSDT")
    recomputed = _recompute_signal(
        payload=payload,
        decision=decision,
        assignment=assignment,
        cohort=cohort,
    )
    forged = recomputed.model_copy(
        update={"target_weight": "0.4", "signal_hash": "f" * 64}
    )
    assert forged != _recompute_signal(
        payload=payload,
        decision=decision,
        assignment=assignment,
        cohort=cohort,
    )
