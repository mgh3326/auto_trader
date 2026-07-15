"""Authoritative, fail-closed evidence assembly for ROB-850 evaluations.

The reader starts at the ROB-849 assignment and exact run-order links.  It
never discovers broker rows by a broad cohort/correlation query and never
uses caller supplied lineage or gate timestamps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Never

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_evaluation import EvaluationConfig as EvaluationConfigRow
from app.models.paper_evaluation import EvaluationEpoch as EvaluationEpochRow
from app.models.paper_validation import PaperValidationStateTransition
from app.models.review import AlpacaPaperOrderLedger
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_cohort.signals import CanonicalTargetSignal
from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EvaluationConfig,
    EvaluationConfigError,
)
from app.services.research_canonical_hash import canonical_sha256

Venue = Literal["binance", "alpaca"]


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class NativeFill:
    venue: Venue
    native_row_id: int
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    price: Decimal
    fee: Decimal
    partial: bool
    filled_at: datetime
    client_order_id: str
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class NativeMark:
    venue: Venue
    symbol: str
    price: Decimal
    marked_at: datetime


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    snapshot_id: str
    snapshot_hash: str
    observed_at: datetime
    closes: tuple[tuple[str, Decimal], ...]
    opens: tuple[tuple[str, Decimal], ...]
    target_weights: tuple[tuple[str, Decimal], ...]
    signal_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    epoch: EpochIdentity
    config: EvaluationConfig
    shadow_window: EvaluationWindow
    paper_window: EvaluationWindow
    shadow_observations: tuple[ShadowObservation, ...]
    binance_fills: tuple[NativeFill, ...]
    alpaca_fills: tuple[NativeFill, ...]
    binance_marks: tuple[NativeMark, ...]
    alpaca_marks: tuple[NativeMark, ...]
    manifest_hash: str


def _fail(reason: str, detail: str = "") -> Never:
    raise EvaluationConfigError(reason, detail or reason)


def _aware_utc(value: datetime, context: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _fail("malformed_evidence", f"{context} is not timezone-aware")
    return value.astimezone(UTC)


def _positive_decimal(value: object, context: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _fail("malformed_evidence", f"{context} is not decimal")
    if not result.is_finite() or result <= 0:
        _fail("malformed_evidence", f"{context} must be positive and finite")
    return result


def _nonnegative_decimal(value: object | None, context: str) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _fail("malformed_evidence", f"{context} is not decimal")
    if not result.is_finite() or result < 0:
        _fail("malformed_evidence", f"{context} must be non-negative and finite")
    return result


def _alpaca_filled_at(payload: object) -> datetime | None:
    """Find a broker-provided fill timestamp without lifecycle-time fallback."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"filled_at", "filledAt", "fill_timestamp"} and isinstance(
                value, str
            ):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return parsed.astimezone(UTC)
            nested = _alpaca_filled_at(value)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _alpaca_filled_at(value)
            if nested is not None:
                return nested
    return None


def _quote_mark(intent: PaperCohortVenueIntent) -> NativeMark:
    quote = intent.venue_quote_evidence
    try:
        venue = str(quote["venue"])
        symbol = str(quote["symbol"])
        bid = _positive_decimal(quote["bid_price"], "quote bid")
        ask = _positive_decimal(quote["ask_price"], "quote ask")
        marked_at = datetime.fromisoformat(
            str(quote["fetched_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        _fail("malformed_evidence", "invalid venue quote")
    if venue != intent.venue or bid >= ask:
        _fail("cross_wired_evidence", "venue quote identity or spread mismatch")
    return NativeMark(
        venue=intent.venue,  # type: ignore[arg-type]
        symbol=symbol,
        price=(bid + ask) / 2,
        marked_at=_aware_utc(marked_at, "quote fetched_at"),
    )


class AuthoritativeEvidenceReader:
    """Load one assignment's canonical and exact linked native evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self,
        *,
        evaluated_at: datetime,
        validation_id: str | None = None,
        cohort_id: str | None = None,
        assignment_id: str | None = None,
    ) -> EvaluationEvidence:
        evaluated_at = _aware_utc(evaluated_at, "evaluated_at")
        assignment = await self._load_assignment(
            validation_id=validation_id,
            cohort_id=cohort_id,
            assignment_id=assignment_id,
        )
        cohort = await self._session.scalar(
            select(PaperValidationCohort).where(
                PaperValidationCohort.cohort_id == assignment.cohort_id
            )
        )
        if cohort is None:
            _fail("missing_evidence", "cohort missing")
        if assignment.experiment_hash != assignment.experiment_id:
            _fail("lineage_mismatch", "assignment experiment identity mismatch")

        transitions = tuple(
            (
                await self._session.scalars(
                    select(PaperValidationStateTransition)
                    .where(
                        PaperValidationStateTransition.validation_id
                        == assignment.validation_id
                    )
                    .order_by(PaperValidationStateTransition.sequence)
                )
            ).all()
        )
        expected = (
            assignment.experiment_hash,
            cohort.cohort_hash,
            assignment.config_hash,
            assignment.cohort_id,
        )
        if not transitions or any(
            (
                item.experiment_hash,
                item.cohort_hash,
                item.config_hash,
                item.cohort_id,
            )
            != expected
            for item in transitions
        ):
            _fail("transition_lineage_mismatch")
        shadow_start = self._transition_at(transitions, "shadow_soak")
        paper_start = self._transition_at(transitions, "paper_active")
        if not shadow_start < paper_start <= evaluated_at:
            _fail("invalid_evaluation_window")

        epoch_row = await self._session.scalar(
            select(EvaluationEpochRow)
            .where(
                EvaluationEpochRow.cohort_id == assignment.cohort_id,
                EvaluationEpochRow.assignment_id == assignment.assignment_id,
                EvaluationEpochRow.validation_id == assignment.validation_id,
                EvaluationEpochRow.experiment_hash == assignment.experiment_hash,
                EvaluationEpochRow.cohort_hash == cohort.cohort_hash,
            )
            .order_by(EvaluationEpochRow.started_at.desc())
        )
        if epoch_row is None:
            _fail("missing_evidence", "evaluation epoch missing")
        config_row = await self._session.scalar(
            select(EvaluationConfigRow).where(
                EvaluationConfigRow.config_hash == epoch_row.config_hash
            )
        )
        if config_row is None:
            _fail("missing_evidence", "evaluation config missing")
        config = EvaluationConfig.model_validate(config_row.payload)
        if config.config_hash() != config_row.config_hash:
            _fail("config_hash_mismatch")
        epoch = EpochIdentity(
            epoch_id=epoch_row.epoch_id,
            assignment_id=epoch_row.assignment_id,
            validation_id=epoch_row.validation_id,
            cohort_id=epoch_row.cohort_id,
            config_hash=epoch_row.config_hash,
            experiment_hash=epoch_row.experiment_hash,
            cohort_hash=epoch_row.cohort_hash,
            initial_equity=epoch_row.initial_equity,
            started_at=epoch_row.started_at,
            reset_reason=epoch_row.reset_reason,
            prior_epoch_id=epoch_row.prior_epoch_id,
        )
        if dict(epoch.initial_equity) != dict(config.initial_equity):
            _fail("initial_equity_mismatch")

        shadow = await self._load_shadow(
            assignment=assignment,
            start=shadow_start,
            end=paper_start,
        )
        (
            binance_fills,
            alpaca_fills,
            binance_marks,
            alpaca_marks,
        ) = await self._load_native(
            assignment=assignment,
            start=paper_start,
            end=evaluated_at,
        )
        manifest = canonical_sha256(
            {
                "identity": {
                    "epoch_id": epoch.epoch_id,
                    "assignment_id": epoch.assignment_id,
                    "validation_id": epoch.validation_id,
                    "cohort_id": epoch.cohort_id,
                    "config_hash": epoch.config_hash,
                    "experiment_hash": epoch.experiment_hash,
                    "cohort_hash": epoch.cohort_hash,
                },
                "windows": {
                    "shadow": [shadow_start, paper_start],
                    "paper": [paper_start, evaluated_at],
                },
                "shadow": [
                    [item.snapshot_id, item.snapshot_hash, item.signal_hashes]
                    for item in shadow
                ],
                "fills": [
                    [item.venue, item.native_row_id, item.filled_at]
                    for item in (*binance_fills, *alpaca_fills)
                ],
                "marks": [
                    [item.venue, item.symbol, str(item.price), item.marked_at]
                    for item in (*binance_marks, *alpaca_marks)
                ],
            }
        )
        return EvaluationEvidence(
            epoch=epoch,
            config=config,
            shadow_window=EvaluationWindow(shadow_start, paper_start),
            paper_window=EvaluationWindow(paper_start, evaluated_at),
            shadow_observations=shadow,
            binance_fills=binance_fills,
            alpaca_fills=alpaca_fills,
            binance_marks=binance_marks,
            alpaca_marks=alpaca_marks,
            manifest_hash=manifest,
        )

    async def _load_assignment(
        self,
        *,
        validation_id: str | None,
        cohort_id: str | None,
        assignment_id: str | None,
    ) -> PaperValidationCohortAssignment:
        if validation_id is not None and (
            cohort_id is not None or assignment_id is not None
        ):
            _fail("invalid_evaluation_identity")
        if validation_id is None and (cohort_id is None or assignment_id is None):
            _fail("invalid_evaluation_identity")
        query = select(PaperValidationCohortAssignment)
        if validation_id is not None:
            query = query.where(
                PaperValidationCohortAssignment.validation_id == validation_id
            )
        else:
            query = query.where(
                PaperValidationCohortAssignment.cohort_id == cohort_id,
                PaperValidationCohortAssignment.assignment_id == assignment_id,
            )
        rows = tuple((await self._session.scalars(query)).all())
        if len(rows) != 1:
            _fail("missing_evidence", "assignment identity is not unique")
        return rows[0]

    @staticmethod
    def _transition_at(
        transitions: tuple[PaperValidationStateTransition, ...], state: str
    ) -> datetime:
        matches = [item.created_at for item in transitions if item.new_state == state]
        if len(matches) != 1:
            _fail("missing_evidence", f"{state} transition missing or duplicated")
        return _aware_utc(matches[0], f"{state} transition")

    async def _load_shadow(
        self,
        *,
        assignment: PaperValidationCohortAssignment,
        start: datetime,
        end: datetime,
    ) -> tuple[ShadowObservation, ...]:
        rows = (
            await self._session.execute(
                select(PaperCohortDecision, CanonicalMarketSnapshot)
                .join(
                    CanonicalMarketSnapshot,
                    CanonicalMarketSnapshot.snapshot_id
                    == PaperCohortDecision.snapshot_id,
                )
                .where(
                    PaperCohortDecision.cohort_id == assignment.cohort_id,
                    PaperCohortDecision.assignment_id == assignment.assignment_id,
                    PaperCohortDecision.mode == "shadow",
                    CanonicalMarketSnapshot.capture_completed_at >= start,
                    CanonicalMarketSnapshot.capture_completed_at < end,
                )
                .order_by(CanonicalMarketSnapshot.capture_completed_at)
            )
        ).all()
        grouped: dict[
            str, list[tuple[PaperCohortDecision, CanonicalMarketSnapshot]]
        ] = defaultdict(list)
        for decision, snapshot in rows:
            grouped[snapshot.snapshot_id].append((decision, snapshot))
        observations: list[ShadowObservation] = []
        for pairs in grouped.values():
            snapshot = pairs[0][1]
            payload = CanonicalSnapshotPayload.model_validate(snapshot.payload)
            if payload.recomputed_content_hash() != snapshot.content_hash:
                _fail("snapshot_hash_mismatch")
            if any(
                decision.snapshot_hash != snapshot.content_hash
                or decision.cohort_id != snapshot.cohort_id
                for decision, _ in pairs
            ):
                _fail("cross_wired_evidence")
            signals = [
                CanonicalTargetSignal.model_validate(item.signal_payload)
                for item, _ in pairs
            ]
            if len(signals) != 2 or {item.symbol for item in signals} != {
                "BTCUSDT",
                "ETHUSDT",
            }:
                _fail("malformed_evidence", "shadow snapshot needs both symbols")
            for signal, (decision, _) in zip(signals, pairs, strict=True):
                if (
                    signal.recomputed_signal_hash() != decision.signal_hash
                    or signal.signal_hash != decision.signal_hash
                    or signal.assignment_id != assignment.assignment_id
                    or signal.cohort_id != assignment.cohort_id
                    or signal.config_hash != assignment.config_hash
                    or signal.experiment_id != assignment.experiment_id
                    or Decimal(signal.target_weight)
                    != Decimal(assignment.target_weights[signal.symbol])
                ):
                    _fail("signal_hash_mismatch")
            closes: list[tuple[str, Decimal]] = []
            opens: list[tuple[str, Decimal]] = []
            for symbol_payload in payload.symbols:
                candle = symbol_payload.candles[-1]
                closes.append(
                    (symbol_payload.symbol, _positive_decimal(candle.close, "close"))
                )
                opens.append(
                    (symbol_payload.symbol, _positive_decimal(candle.open, "open"))
                )
            observations.append(
                ShadowObservation(
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.content_hash,
                    observed_at=_aware_utc(
                        snapshot.capture_completed_at, "snapshot time"
                    ),
                    closes=tuple(sorted(closes)),
                    opens=tuple(sorted(opens)),
                    target_weights=tuple(
                        sorted(
                            (item.symbol, Decimal(item.target_weight))
                            for item in signals
                        )
                    ),
                    signal_hashes=tuple(sorted(item.signal_hash for item in signals)),
                )
            )
        if any(
            a.observed_at >= b.observed_at
            for a, b in zip(observations, observations[1:], strict=False)
        ):
            _fail("out_of_order_evidence")
        return tuple(observations)

    async def _load_native(
        self,
        *,
        assignment: PaperValidationCohortAssignment,
        start: datetime,
        end: datetime,
    ) -> tuple[
        tuple[NativeFill, ...],
        tuple[NativeFill, ...],
        tuple[NativeMark, ...],
        tuple[NativeMark, ...],
    ]:
        rows = (
            await self._session.execute(
                select(
                    PaperRunOrderLink,
                    PaperCohortVenueIntent,
                    PaperCohortDecision,
                    CanonicalMarketSnapshot,
                )
                .join(
                    PaperCohortVenueIntent,
                    PaperCohortVenueIntent.intent_id == PaperRunOrderLink.intent_id,
                )
                .join(
                    PaperCohortDecision,
                    PaperCohortDecision.decision_id == PaperRunOrderLink.decision_id,
                )
                .join(
                    CanonicalMarketSnapshot,
                    CanonicalMarketSnapshot.snapshot_id
                    == PaperRunOrderLink.snapshot_id,
                )
                .where(
                    PaperRunOrderLink.cohort_id == assignment.cohort_id,
                    PaperRunOrderLink.assignment_id == assignment.assignment_id,
                )
            )
        ).all()
        fills: dict[str, list[NativeFill]] = {"binance": [], "alpaca": []}
        marks: dict[str, list[NativeMark]] = {"binance": [], "alpaca": []}
        native_keys: set[tuple[str, int]] = set()
        for link, intent, decision, snapshot in rows:
            exact = (
                link.intent_id == intent.intent_id,
                link.decision_id == intent.decision_id == decision.decision_id,
                link.assignment_id == intent.assignment_id == decision.assignment_id,
                link.cohort_id
                == intent.cohort_id
                == decision.cohort_id
                == snapshot.cohort_id,
                link.snapshot_id
                == intent.snapshot_id
                == decision.snapshot_id
                == snapshot.snapshot_id,
                link.snapshot_hash
                == intent.snapshot_hash
                == decision.snapshot_hash
                == snapshot.content_hash,
                link.symbol == intent.symbol == decision.symbol,
                link.venue == intent.venue,
            )
            if not all(exact):
                _fail("cross_wired_evidence")
            key = (link.native_ledger_kind, link.native_ledger_row_id)
            if key in native_keys:
                _fail("duplicate_evidence")
            native_keys.add(key)
            mark = _quote_mark(intent)
            if start <= mark.marked_at <= end:
                marks[link.venue].append(mark)
            if link.venue == "binance":
                row = await self._session.get(
                    BinanceDemoOrderLedger, link.native_ledger_row_id
                )
                if (
                    row is None
                    or row.client_order_id != link.client_order_id
                    or str(row.broker_order_id) != link.broker_order_id
                ):
                    _fail("cross_wired_evidence", "linked Binance row mismatch")
                filled_at = row.filled_at
                metadata = row.extra_metadata or {}
                qty, price = metadata.get("filled_qty", row.qty), row.price
                fee = metadata.get("fee_usdt")
                partial = bool(metadata.get("is_partial_fill", False))
                side = row.side.lower()
            else:
                row = await self._session.get(
                    AlpacaPaperOrderLedger, link.native_ledger_row_id
                )
                if (
                    row is None
                    or row.client_order_id != link.client_order_id
                    or str(row.broker_order_id) != link.broker_order_id
                ):
                    _fail("cross_wired_evidence", "linked Alpaca row mismatch")
                filled_at = _alpaca_filled_at(row.raw_responses)
                qty, price, fee, side = (
                    row.filled_qty,
                    row.filled_avg_price,
                    row.fee_amount,
                    row.side,
                )
                partial = (
                    row.requested_qty is not None
                    and row.filled_qty is not None
                    and Decimal(str(row.filled_qty)) < Decimal(str(row.requested_qty))
                )
                if row.currency != "USD":
                    _fail("currency_mismatch")
            if filled_at is None:
                _fail("missing_evidence", f"{link.venue} fill timestamp missing")
            filled_at = _aware_utc(filled_at, "fill timestamp")
            if not start <= filled_at <= end:
                continue
            if side not in {"buy", "sell"}:
                _fail("malformed_evidence", "invalid fill side")
            fills[link.venue].append(
                NativeFill(
                    venue=link.venue,  # type: ignore[arg-type]
                    native_row_id=link.native_ledger_row_id,
                    symbol=mark.symbol,
                    side=side,  # type: ignore[arg-type]
                    quantity=_positive_decimal(qty, "filled quantity"),
                    price=_positive_decimal(price, "fill price"),
                    fee=_nonnegative_decimal(fee, "fill fee"),
                    partial=partial,
                    filled_at=filled_at,
                    client_order_id=link.client_order_id,
                    broker_order_id=link.broker_order_id,
                )
            )
        for venue in ("binance", "alpaca"):
            fills[venue].sort(key=lambda item: (item.filled_at, item.native_row_id))
            marks[venue].sort(key=lambda item: (item.marked_at, item.symbol))
        return (
            tuple(fills["binance"]),
            tuple(fills["alpaca"]),
            tuple(marks["binance"]),
            tuple(marks["alpaca"]),
        )
