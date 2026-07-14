"""ROB-850 read-only orchestration service.

The :class:`PaperEvaluationService` orchestrates the three-view P&L
computation, gate evaluation, conjunctive verdict, and idempotent
persistence to the ROB-850-owned ``evaluation_verdicts`` table.

Hard guarantees
----------------
* **Read-only with respect to broker ledgers.**  The service never calls
  any ``record_*``, ``claim_*``, ``reserve_*``, ``submit``,
  ``place_order``, ``_transition``, or ``update_state`` method on any
  ledger service.  Only the Protocol-defined read methods
  (``closed_rows_since``, ``list_by_correlation_id``,
  ``list_snapshots``) are invoked.
* **No broker service module import.**  The service operates entirely
  through the injected Protocol readers from :mod:`app.services.paper_evaluation.pnl`.
* **No promotion state transition.**  The verdict is evidence only;
  ROB-848 owns the promotion decision.
* **No USDT/USD conversion.**  Each view's nominal P&L stays in its
  native currency.  No cross-view nominal total is emitted.
* **All financial math uses** :class:`decimal.Decimal`.
* **Idempotent and concurrent-safe.**  Replay with the same
  ``(epoch_id, idempotency_key, request_hash)`` returns the existing
  verdict; conflicting replay raises ``idempotency_conflict``;
  concurrent writes resolve to one winner via the
  ``uq_evaluation_verdict_idempotency`` / ``uq_evaluation_verdict_epoch``
  unique constraints.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_evaluation import EvaluationVerdict
from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EvaluationConfig,
    EvaluationConfigError,
    GateVerdict,
    ScorecardVerdict,
    ViewName,
)
from app.services.paper_evaluation.gate import (
    evaluate_paper_gate,
    evaluate_shadow_gate,
)
from app.services.paper_evaluation.pnl import (
    PaperEvaluationPnL,
    _AlpacaLedgerReader,
    _BinanceLedgerReader,
    _SnapshotReader,
)
from app.services.paper_evaluation.scorecard import compute_conjunctive_verdict
from app.services.research_canonical_hash import canonical_sha256

__all__ = ["PaperEvaluationService"]


# ---------------------------------------------------------------------------
# Request-hash computation
# ---------------------------------------------------------------------------


def _compute_request_hash(
    *,
    epoch: EpochIdentity,
    idempotency_key: str,
) -> str:
    """Deterministic SHA-256 of ``(epoch_id, config_hash, idempotency_key)``.

    The hash captures the full evaluation request identity so that a
    replay with the same inputs produces the same hash (safe replay),
    while a conflicting replay (same idempotency_key, different
    epoch/config) produces a different hash (conflict detection).
    """
    return canonical_sha256(
        {
            "epoch_id": epoch.epoch_id,
            "config_hash": epoch.config_hash,
            "idempotency_key": idempotency_key,
        }
    )


# ---------------------------------------------------------------------------
# Verdict serialisation helpers
# ---------------------------------------------------------------------------


def _verdict_to_payload(verdict: ScorecardVerdict) -> dict[str, object]:
    """Convert a :class:`ScorecardVerdict` to a JSON-safe dict for JSONB.

    Uses ``model_dump_json`` then ``json.loads`` to guarantee that every
    ``Decimal``, ``datetime``, ``StrEnum``, and tuple is serialised to
    JSON-native types.  This is essential for stable JSONB storage and
    deterministic round-trip reads.
    """
    return json.loads(verdict.model_dump_json())


def _payload_to_verdict(payload: dict[str, object]) -> ScorecardVerdict:
    """Reconstruct a :class:`ScorecardVerdict` from a JSONB payload."""
    return ScorecardVerdict.model_validate(payload)


# ---------------------------------------------------------------------------
# Target-weights derivation
# ---------------------------------------------------------------------------


def _derive_target_weights(config: EvaluationConfig) -> dict[str, Decimal]:
    """Derive shadow-view target weights from the frozen config.

    Maps the canonical-shadow benchmark symbols to the frozen
    :class:`BenchmarkWeights` (BTCUSDT → ``btc_weight``,
    ETHUSDT → ``eth_weight``).  Any unrecognised benchmark symbol
    receives a zero weight.
    """
    shadow_mapping = config.views[ViewName.CANONICAL_SHADOW]
    bw = config.benchmark_weights
    weights: dict[str, Decimal] = {}
    for symbol in shadow_mapping.benchmark_symbols:
        if symbol == "BTCUSDT":
            weights[symbol] = bw.btc_weight
        elif symbol == "ETHUSDT":
            weights[symbol] = bw.eth_weight
        else:
            weights[symbol] = Decimal("0")
    return weights


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaperEvaluationService:
    """Read-only 3-view evaluation orchestration service.

    The service computes the three independent P&L views (Binance USDT,
    Alpaca USD, canonical shadow USDT), evaluates the 7/60-day gates,
    computes the conjunctive verdict, and persists it idempotently to
    ``research.evaluation_verdicts``.

    The caller is responsible for pre-creating the
    :class:`EvaluationConfig`, :class:`EvaluationEpoch`, and the
    underlying ``PaperValidationCohort`` rows; the service only inserts
    into ``evaluation_verdicts`` (the ROB-850-owned evidence table).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        binance_reader: _BinanceLedgerReader,
        alpaca_reader: _AlpacaLedgerReader,
        snapshot_reader: _SnapshotReader,
    ) -> None:
        self._session = session
        self._binance_reader = binance_reader
        self._alpaca_reader = alpaca_reader
        self._snapshot_reader = snapshot_reader

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        *,
        epoch: EpochIdentity,
        config: EvaluationConfig,
        experiment_hash: str,
        cohort_hash: str,
        idempotency_key: str,
        shadow_started_at: datetime | None = None,
        paper_started_at: datetime | None = None,
        evaluated_at: datetime | None = None,
    ) -> ScorecardVerdict:
        """Orchestrate the full 3-view evaluation and persist the verdict.

        Parameters
        ----------
        epoch:
            The frozen :class:`EpochIdentity` for this evaluation boundary.
        config:
            The frozen :class:`EvaluationConfig` (V1 schema).
        experiment_hash:
            64-hex SHA-256 of the research experiment identity.
        cohort_hash:
            64-hex SHA-256 of the paper-validation cohort identity.
        idempotency_key:
            Caller-supplied idempotency key scoped to ``(epoch_id)``.
        shadow_started_at:
            If provided, the 7-day shadow soak gate is evaluated from
            this timestamp to ``evaluated_at``.
        paper_started_at:
            If provided, the 60-day paper promotion gate is evaluated
            from this timestamp to ``evaluated_at``.
        evaluated_at:
            The evaluation timestamp (defaults to ``datetime.now(UTC)``).

        Returns
        -------
        ScorecardVerdict
            The deterministic conjunctive verdict, persisted to
            ``evaluation_verdicts``.

        Raises
        ------
        EvaluationConfigError
            ``idempotency_conflict`` — same ``(epoch_id, idempotency_key)``
            with a different ``request_hash``.
            ``concurrent_evaluation_conflict`` — concurrent write by
            another caller for the same epoch.
        """
        if evaluated_at is None:
            evaluated_at = datetime.now(UTC)

        request_hash = _compute_request_hash(
            epoch=epoch, idempotency_key=idempotency_key
        )

        # 1. Idempotency check (pre-compute).
        existing = await self._find_existing_verdict(
            epoch_id=epoch.epoch_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.request_hash == request_hash:
                return _payload_to_verdict(existing.verdict_payload)
            raise EvaluationConfigError(
                "idempotency_conflict",
                (
                    f"idempotency_key={idempotency_key!r} already used "
                    f"for epoch={epoch.epoch_id!r} with a different "
                    "request_hash"
                ),
            )

        # 2. Compute 3-view P&L.
        view_metrics = await self._compute_views(
            epoch=epoch,
            config=config,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )

        # 3. Evaluate gates.
        shadow_gate = self._maybe_evaluate_shadow_gate(
            shadow_started_at=shadow_started_at,
            evaluated_at=evaluated_at,
        )
        paper_gate = self._maybe_evaluate_paper_gate(
            paper_started_at=paper_started_at,
            evaluated_at=evaluated_at,
            epoch_config_hash=epoch.config_hash,
            current_config_hash=config.config_hash(),
        )

        # 4. Conjunctive verdict.
        verdict = compute_conjunctive_verdict(
            view_metrics=view_metrics,
            config=config,
            shadow_gate=shadow_gate,
            paper_gate=paper_gate,
            epoch_id=epoch.epoch_id,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )

        # 5. Persist (idempotent + concurrent-safe).
        await self._persist_verdict(
            verdict=verdict,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )

        return verdict

    # ------------------------------------------------------------------
    # View computation
    # ------------------------------------------------------------------

    async def _compute_views(
        self,
        *,
        epoch: EpochIdentity,
        config: EvaluationConfig,
        experiment_hash: str,
        cohort_hash: str,
    ) -> dict[ViewName, object]:
        """Compute all three evaluation views using the injected readers.

        * Binance: ``since = epoch.started_at``
        * Alpaca: ``correlation_ids = [epoch.cohort_id]`` (cohort-scoped)
        * Shadow: ``cohort_id = epoch.cohort_id``,
          ``since = epoch.started_at``, ``target_weights`` derived from
          the frozen config benchmark weights.
        """
        pnl = PaperEvaluationPnL(
            config=config,
            epoch=epoch,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )

        since = epoch.started_at

        binance_metrics = await pnl.compute_binance_view(
            self._binance_reader,
            since=since,
        )

        alpaca_metrics = await pnl.compute_alpaca_view(
            self._alpaca_reader,
            correlation_ids=[epoch.cohort_id],
        )

        target_weights = _derive_target_weights(config)
        shadow_metrics = await pnl.compute_shadow_view(
            self._snapshot_reader,
            target_weights,
            cohort_id=epoch.cohort_id,
            since=since,
        )

        return {
            ViewName.BINANCE_BROKER: binance_metrics,
            ViewName.ALPACA_BROKER: alpaca_metrics,
            ViewName.CANONICAL_SHADOW: shadow_metrics,
        }

    # ------------------------------------------------------------------
    # Gate evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_evaluate_shadow_gate(
        *,
        shadow_started_at: datetime | None,
        evaluated_at: datetime,
    ) -> GateVerdict | None:
        if shadow_started_at is None:
            return None
        return evaluate_shadow_gate(
            shadow_started_at=shadow_started_at,
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _maybe_evaluate_paper_gate(
        *,
        paper_started_at: datetime | None,
        evaluated_at: datetime,
        epoch_config_hash: str,
        current_config_hash: str,
    ) -> GateVerdict | None:
        if paper_started_at is None:
            return None
        return evaluate_paper_gate(
            paper_started_at=paper_started_at,
            evaluated_at=evaluated_at,
            config_hash=epoch_config_hash,
            current_config_hash=current_config_hash,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _find_existing_verdict(
        self,
        *,
        epoch_id: str,
        idempotency_key: str,
    ) -> EvaluationVerdict | None:
        """Query for an existing verdict by ``(epoch_id, idempotency_key)``."""
        result = await self._session.execute(
            select(EvaluationVerdict).where(
                EvaluationVerdict.epoch_id == epoch_id,
                EvaluationVerdict.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def _persist_verdict(
        self,
        *,
        verdict: ScorecardVerdict,
        idempotency_key: str,
        request_hash: str,
        experiment_hash: str,
        cohort_hash: str,
    ) -> None:
        """Insert the verdict idempotently.

        On unique-constraint violation (concurrent write), re-read the
        existing verdict and either replay it (matching request_hash)
        or raise ``concurrent_evaluation_conflict``.
        """
        payload = _verdict_to_payload(verdict)
        row = EvaluationVerdict(
            epoch_id=verdict.epoch_id,
            config_hash=verdict.config_hash,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            verdict_status=verdict.status.value,
            verdict_payload=payload,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            existing = await self._find_existing_verdict(
                epoch_id=verdict.epoch_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.request_hash == request_hash:
                    # Concurrent replay of the same request — safe.
                    return
                raise EvaluationConfigError(
                    "concurrent_evaluation_conflict",
                    (
                        f"concurrent verdict already persisted for "
                        f"epoch={verdict.epoch_id!r}, "
                        f"idempotency_key={idempotency_key!r} with a "
                        "different request_hash"
                    ),
                ) from None
            # The conflict is on ``uq_evaluation_verdict_epoch`` (one
            # verdict per epoch, different idempotency_key).
            raise EvaluationConfigError(
                "concurrent_evaluation_conflict",
                (
                    f"concurrent verdict already persisted for "
                    f"epoch={verdict.epoch_id!r} under a different "
                    "idempotency_key"
                ),
            ) from None
