"""Authoritative ROB-850 orchestration integration contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.paper_evaluation.contracts import EpochIdentity, ViewName
from app.services.paper_evaluation.evidence import (
    EvaluationEvidence,
    EvaluationWindow,
    NativeFill,
    NativeMark,
    ShadowObservation,
)
from app.services.paper_evaluation.service import (
    PaperEvaluationService,
    _request_hash,
)
from tests.services.paper_evaluation.conftest import make_evaluation_config, stable_hash

pytestmark = pytest.mark.unit


class _EvidenceReader:
    def __init__(self, evidence: EvaluationEvidence) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, object]] = []

    async def load(self, **kwargs: object) -> EvaluationEvidence:
        self.calls.append(kwargs)
        return self.evidence


def _fill(
    venue: str, row_id: int, symbol: str, side: str, qty: str, price: str, at: datetime
) -> NativeFill:
    return NativeFill(
        venue=venue,  # type: ignore[arg-type]
        native_row_id=row_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        partial=False,
        filled_at=at,
        client_order_id=f"client-{row_id}",
        broker_order_id=f"broker-{row_id}",
    )


def _marks(
    venue: str, symbols: tuple[str, str], start: datetime, end: datetime
) -> tuple[NativeMark, ...]:
    return tuple(
        NativeMark(
            venue=venue,  # type: ignore[arg-type]
            symbol=symbol,
            price=Decimal(price),
            marked_at=at,
        )
        for symbol, price, at in (
            (symbols[0], "100", start),
            (symbols[1], "100", start),
            (symbols[0], "110", end),
            (symbols[1], "110", end),
        )
    )


def make_evidence(*, evaluated_at: datetime | None = None) -> EvaluationEvidence:
    config = make_evaluation_config(
        min_observations=1,
        min_fills=1,
        min_calendar_days=7,
        fill_timing="canonical_close",
    )
    shadow_start = datetime(2026, 1, 1, tzinfo=UTC)
    paper_start = shadow_start + timedelta(days=7)
    end = evaluated_at or paper_start + timedelta(days=60)
    identity = EpochIdentity(
        epoch_id="epoch-1",
        assignment_id="assignment-1",
        validation_id="validation-1",
        cohort_id="cohort-1",
        config_hash=config.config_hash(),
        experiment_hash=stable_hash("experiment"),
        cohort_hash=stable_hash("cohort"),
        initial_equity=config.initial_equity,
        started_at=paper_start,
    )
    observations = (
        ShadowObservation(
            snapshot_id="snapshot-1",
            snapshot_hash=stable_hash("snapshot-1"),
            observed_at=shadow_start,
            closes=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("100"))),
            opens=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("100"))),
            target_weights=(("BTCUSDT", Decimal("0.5")), ("ETHUSDT", Decimal("0.5"))),
            signal_hashes=(stable_hash("signal-1"), stable_hash("signal-2")),
        ),
        ShadowObservation(
            snapshot_id="snapshot-2",
            snapshot_hash=stable_hash("snapshot-2"),
            observed_at=paper_start - timedelta(minutes=1),
            closes=(("BTCUSDT", Decimal("110")), ("ETHUSDT", Decimal("110"))),
            opens=(("BTCUSDT", Decimal("110")), ("ETHUSDT", Decimal("110"))),
            target_weights=(("BTCUSDT", Decimal("0.5")), ("ETHUSDT", Decimal("0.5"))),
            signal_hashes=(stable_hash("signal-3"), stable_hash("signal-4")),
        ),
    )
    return EvaluationEvidence(
        epoch=identity,
        config=config,
        shadow_window=EvaluationWindow(shadow_start, paper_start),
        paper_window=EvaluationWindow(paper_start, end),
        shadow_observations=observations,
        binance_fills=(
            _fill("binance", 1, "BTCUSDT", "buy", "2", "100", paper_start),
            _fill("binance", 2, "BTCUSDT", "sell", "1", "110", end),
        ),
        alpaca_fills=(
            _fill("alpaca", 3, "BTC/USD", "buy", "2", "100", paper_start),
            _fill("alpaca", 4, "BTC/USD", "sell", "1", "110", end),
        ),
        binance_marks=_marks("binance", ("BTCUSDT", "ETHUSDT"), paper_start, end),
        alpaca_marks=_marks("alpaca", ("BTC/USD", "ETH/USD"), paper_start, end),
        manifest_hash=stable_hash(f"manifest:{end.isoformat()}"),
    )


@pytest.mark.asyncio
async def test_service_accepts_only_authoritative_assignment_identity() -> None:
    evidence = make_evidence()
    reader = _EvidenceReader(evidence)
    service = PaperEvaluationService(AsyncMock(), evidence_reader=reader)  # type: ignore[arg-type]
    service._find_existing = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._persist_evaluation = AsyncMock(
        side_effect=lambda **kwargs: kwargs["verdict"]
    )  # type: ignore[method-assign]

    verdict = await service.evaluate(
        validation_id="validation-1",
        idempotency_key="evaluation-1",
        evaluated_at=evidence.paper_window.end,
    )

    assert set(verdict.view_metrics) == set(ViewName)
    assert reader.calls[0]["validation_id"] == "validation-1"
    assert verdict.view_metrics[ViewName.ALPACA_BROKER].nominal_net_pnl == Decimal("20")
    assert verdict.view_metrics[ViewName.BINANCE_BROKER].nominal_net_pnl == Decimal(
        "20"
    )


@pytest.mark.asyncio
async def test_caller_cannot_supply_hashes_epoch_or_gate_timestamps() -> None:
    service = PaperEvaluationService(
        AsyncMock(), evidence_reader=_EvidenceReader(make_evidence())
    )  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await service.evaluate(  # type: ignore[call-arg]
            validation_id="validation-1",
            idempotency_key="evaluation-1",
            experiment_hash=stable_hash("forged"),
        )


def test_request_hash_covers_time_identity_gates_config_and_manifest() -> None:
    first = make_evidence()
    later = make_evidence(evaluated_at=first.paper_window.end + timedelta(minutes=1))
    assert _request_hash(first) != _request_hash(later)
    changed_manifest = replace(first, manifest_hash=stable_hash("other"))
    assert _request_hash(first) != _request_hash(changed_manifest)
