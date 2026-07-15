"""Savepoint, idempotency, and concurrent-winner contracts for ROB-850."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.paper_evaluation import EvaluationScorecard, EvaluationVerdict
from app.services.paper_evaluation.contracts import EvaluationConfigError
from app.services.paper_evaluation.service import PaperEvaluationService, _request_hash
from tests.services.paper_evaluation.test_integration import make_evidence

pytestmark = pytest.mark.unit


class _Savepoint(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    def __init__(self, *, flush_error: Exception | None = None) -> None:
        self.rows: list[object] = []
        self.flush_error = flush_error
        self.rollback_called = False

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error

    async def rollback(self) -> None:
        self.rollback_called = True


def _winner(*, request_hash: str, payload: dict[str, object]) -> EvaluationVerdict:
    return EvaluationVerdict(
        evaluation_id="e" * 64,
        epoch_id="epoch-1",
        assignment_id="assignment-1",
        config_hash="a" * 64,
        idempotency_key="same-key",
        request_hash=request_hash,
        verdict_status="gate_blocked",
        verdict_payload=payload,
        experiment_hash="b" * 64,
        cohort_hash="c" * 64,
    )


@pytest.mark.asyncio
async def test_one_evaluation_persists_exactly_three_scorecards_and_one_verdict() -> (
    None
):
    evidence = make_evidence()
    bootstrap = PaperEvaluationService(AsyncMock())  # type: ignore[arg-type]
    bootstrap._find_existing = AsyncMock(return_value=None)  # type: ignore[method-assign]
    bootstrap._persist_evaluation = AsyncMock(
        side_effect=lambda **kwargs: kwargs["verdict"]
    )  # type: ignore[method-assign]
    bootstrap._evidence_reader = AsyncMock()
    bootstrap._evidence_reader.load.return_value = evidence
    verdict = await bootstrap.evaluate(
        validation_id="validation-1",
        idempotency_key="same-key",
        evaluated_at=evidence.paper_window.end,
    )

    session = _Session()
    service = PaperEvaluationService(session)  # type: ignore[arg-type]
    stored = _winner(
        request_hash=_request_hash(evidence), payload=verdict.model_dump(mode="json")
    )
    service._find_existing = AsyncMock(return_value=stored)  # type: ignore[method-assign]
    result = await service._persist_evaluation(
        verdict=verdict,
        evidence=evidence,
        idempotency_key="same-key",
        request_hash=_request_hash(evidence),
    )

    assert result == verdict
    assert (
        len([row for row in session.rows if isinstance(row, EvaluationScorecard)]) == 3
    )
    assert len([row for row in session.rows if isinstance(row, EvaluationVerdict)]) == 1
    assert not session.rollback_called


@pytest.mark.asyncio
async def test_concurrent_loser_returns_persisted_winner_without_outer_rollback() -> (
    None
):
    evidence = make_evidence()
    request_hash = _request_hash(evidence)
    bootstrap = PaperEvaluationService(AsyncMock())  # type: ignore[arg-type]
    bootstrap._find_existing = AsyncMock(return_value=None)  # type: ignore[method-assign]
    bootstrap._persist_evaluation = AsyncMock(
        side_effect=lambda **kwargs: kwargs["verdict"]
    )  # type: ignore[method-assign]
    bootstrap._evidence_reader = AsyncMock()
    bootstrap._evidence_reader.load.return_value = evidence
    local = await bootstrap.evaluate(
        validation_id="validation-1",
        idempotency_key="same-key",
        evaluated_at=evidence.paper_window.end,
    )
    winner_payload = local.model_copy(
        update={"reason_text": "persisted winner"}
    ).model_dump(mode="json")
    winner = _winner(request_hash=request_hash, payload=winner_payload)
    session = _Session(flush_error=IntegrityError("insert", {}, Exception("unique")))
    service = PaperEvaluationService(session)  # type: ignore[arg-type]
    service._find_existing = AsyncMock(return_value=winner)  # type: ignore[method-assign]

    result = await service._persist_evaluation(
        verdict=local,
        evidence=evidence,
        idempotency_key="same-key",
        request_hash=request_hash,
    )

    assert result.reason_text == "persisted winner"
    assert not session.rollback_called


def test_same_key_different_semantic_request_is_conflict() -> None:
    first = make_evidence()
    second = make_evidence(evaluated_at=first.paper_window.end + timedelta(minutes=1))
    row = _winner(request_hash=_request_hash(first), payload={})
    with pytest.raises(
        EvaluationConfigError, match="different semantic request"
    ) as exc:
        PaperEvaluationService._replay_or_conflict(row, _request_hash(second))
    assert exc.value.reason_code == "idempotency_conflict"
