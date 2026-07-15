"""Authoritative ROB-850 evaluation orchestration and append-only persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_evaluation import EvaluationScorecard, EvaluationVerdict
from app.services.paper_evaluation.contracts import (
    EvaluationConfigError,
    ScorecardVerdict,
    ViewName,
)
from app.services.paper_evaluation.evidence import (
    AuthoritativeEvidenceReader,
    EvaluationEvidence,
)
from app.services.paper_evaluation.gate import evaluate_paper_gate, evaluate_shadow_gate
from app.services.paper_evaluation.pnl import PaperEvaluationPnL
from app.services.paper_evaluation.scorecard import compute_conjunctive_verdict
from app.services.research_canonical_hash import canonical_sha256

__all__ = ["PaperEvaluationService"]


def _payload(verdict: ScorecardVerdict) -> dict[str, object]:
    return json.loads(verdict.model_dump_json())


def _verdict(payload: dict[str, object]) -> ScorecardVerdict:
    return ScorecardVerdict.model_validate(payload)


def _request_hash(evidence: EvaluationEvidence) -> str:
    """Hash every semantic input; the idempotency key is only a lookup key."""
    epoch = evidence.epoch
    return canonical_sha256(
        {
            "epoch_id": epoch.epoch_id,
            "assignment_id": epoch.assignment_id,
            "validation_id": epoch.validation_id,
            "cohort_id": epoch.cohort_id,
            "config_hash": epoch.config_hash,
            "experiment_hash": epoch.experiment_hash,
            "cohort_hash": epoch.cohort_hash,
            "started_at": epoch.started_at,
            "initial_equity": {
                key.value: str(value) for key, value in epoch.initial_equity.items()
            },
            "evaluated_at": evidence.paper_window.end,
            "shadow_transition_at": evidence.shadow_window.start,
            "paper_transition_at": evidence.paper_window.start,
            "evidence_manifest_hash": evidence.manifest_hash,
            "schema_id": evidence.config.schema_id,
            "formula_version": evidence.config.formula_version,
            "config_payload": evidence.config.to_hash_payload(),
        }
    )


class PaperEvaluationService:
    """Evaluate one ROB-849 assignment without any broker/network mutation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        evidence_reader: AuthoritativeEvidenceReader | None = None,
    ) -> None:
        self._session = session
        self._evidence_reader = evidence_reader or AuthoritativeEvidenceReader(session)

    async def evaluate(
        self,
        *,
        idempotency_key: str,
        evaluated_at: datetime | None = None,
        validation_id: str | None = None,
        cohort_id: str | None = None,
        assignment_id: str | None = None,
    ) -> ScorecardVerdict:
        """Load authoritative identity/evidence, compute, and atomically persist."""
        evidence = await self._evidence_reader.load(
            evaluated_at=evaluated_at or datetime.now(UTC),
            validation_id=validation_id,
            cohort_id=cohort_id,
            assignment_id=assignment_id,
        )
        request_hash = _request_hash(evidence)
        existing = await self._find_existing(
            epoch_id=evidence.epoch.epoch_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return self._replay_or_conflict(existing, request_hash)

        pnl = PaperEvaluationPnL(
            config=evidence.config,
            epoch=evidence.epoch,
            experiment_hash=evidence.epoch.experiment_hash,
            cohort_hash=evidence.epoch.cohort_hash,
        )
        metrics = {
            ViewName.BINANCE_BROKER: pnl.compute_native_evidence_view(
                view_name=ViewName.BINANCE_BROKER,
                fills=evidence.binance_fills,
                marks=evidence.binance_marks,
                window=evidence.paper_window,
            ),
            ViewName.ALPACA_BROKER: pnl.compute_native_evidence_view(
                view_name=ViewName.ALPACA_BROKER,
                fills=evidence.alpaca_fills,
                marks=evidence.alpaca_marks,
                window=evidence.paper_window,
            ),
            ViewName.CANONICAL_SHADOW: pnl.compute_shadow_evidence_view(
                observations=evidence.shadow_observations,
                window=evidence.shadow_window,
            ),
        }
        shadow_gate = evaluate_shadow_gate(
            shadow_started_at=evidence.shadow_window.start,
            evaluated_at=evidence.shadow_window.end,
        )
        paper_gate = evaluate_paper_gate(
            paper_started_at=evidence.paper_window.start,
            evaluated_at=evidence.paper_window.end,
            config_hash=evidence.epoch.config_hash,
            current_config_hash=evidence.config.config_hash(),
        )
        verdict = compute_conjunctive_verdict(
            view_metrics=metrics,
            config=evidence.config,
            shadow_gate=shadow_gate,
            paper_gate=paper_gate,
            epoch_id=evidence.epoch.epoch_id,
            experiment_hash=evidence.epoch.experiment_hash,
            cohort_hash=evidence.epoch.cohort_hash,
        )
        return await self._persist_evaluation(
            verdict=verdict,
            evidence=evidence,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def _find_existing(
        self, *, epoch_id: str, idempotency_key: str
    ) -> EvaluationVerdict | None:
        return await self._session.scalar(
            select(EvaluationVerdict).where(
                EvaluationVerdict.epoch_id == epoch_id,
                EvaluationVerdict.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _replay_or_conflict(
        row: EvaluationVerdict, request_hash: str
    ) -> ScorecardVerdict:
        if row.request_hash != request_hash:
            raise EvaluationConfigError(
                "idempotency_conflict",
                "idempotency key already belongs to a different semantic request",
            )
        return _verdict(row.verdict_payload)

    async def _persist_evaluation(
        self,
        *,
        verdict: ScorecardVerdict,
        evidence: EvaluationEvidence,
        idempotency_key: str,
        request_hash: str,
    ) -> ScorecardVerdict:
        """Persist exactly three scorecards and one verdict in a savepoint."""
        epoch = evidence.epoch
        evaluation_id = canonical_sha256(
            {
                "request_hash": request_hash,
                "idempotency_key": idempotency_key,
            }
        )
        try:
            async with self._session.begin_nested():
                for name in (
                    ViewName.BINANCE_BROKER,
                    ViewName.ALPACA_BROKER,
                    ViewName.CANONICAL_SHADOW,
                ):
                    metrics = verdict.view_metrics[name]
                    self._session.add(
                        EvaluationScorecard(
                            evaluation_id=evaluation_id,
                            epoch_id=epoch.epoch_id,
                            assignment_id=epoch.assignment_id,
                            config_hash=epoch.config_hash,
                            view_name=name.value,
                            currency=metrics.currency.value,
                            experiment_hash=epoch.experiment_hash,
                            cohort_hash=epoch.cohort_hash,
                            metrics=json.loads(metrics.model_dump_json()),
                        )
                    )
                self._session.add(
                    EvaluationVerdict(
                        evaluation_id=evaluation_id,
                        epoch_id=epoch.epoch_id,
                        assignment_id=epoch.assignment_id,
                        config_hash=epoch.config_hash,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        verdict_status=verdict.status.value,
                        verdict_payload=_payload(verdict),
                        experiment_hash=epoch.experiment_hash,
                        cohort_hash=epoch.cohort_hash,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            winner = await self._find_existing(
                epoch_id=epoch.epoch_id,
                idempotency_key=idempotency_key,
            )
            if winner is None:
                raise EvaluationConfigError("concurrent_evaluation_conflict") from None
            return self._replay_or_conflict(winner, request_hash)

        persisted = await self._find_existing(
            epoch_id=epoch.epoch_id,
            idempotency_key=idempotency_key,
        )
        if persisted is None:
            raise EvaluationConfigError("persistence_failed")
        return _verdict(persisted.verdict_payload)
