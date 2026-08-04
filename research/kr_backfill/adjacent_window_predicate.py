"""Frozen structural-equivalence predicate for KRX one-minute source checks.

``ADJACENT_WINDOW_EQUIVALENT_V1`` is deliberately narrower than a numerical
tolerance.  It may classify a raw one-minute mismatch only when the complete
two-minute structural contract is satisfied.  It never changes the raw result:
``RAW_1M_EXACT`` remains ``FAIL`` whenever a raw OHLCV cell differs.

This is a pure, local module.  It performs no fetches, database writes, or
broker/account operations.  Phase B collection and higher-timeframe
reaggregation remain separate operator work.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

PREDICATE_NAME = "ADJACENT_WINDOW_EQUIVALENT_V1"
PREDICATE_VERSION = "1.0.0"

COMPARED_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
NORMALISATION_RULE = "int(round(x)) for finite numeric OHLCV inputs"

KST = timezone(timedelta(hours=9), name="KST")
KST_UTC_OFFSET = timedelta(hours=9)
KST_TIMEZONE_NAME = "Asia/Seoul"
KRX_REGULAR_SEGMENT = "KRX_REGULAR"
REGULAR_SESSION_START = time(9, 0)
REGULAR_SESSION_END = time(15, 30)

# The predicate does not discover an offset.  V1's already-frozen label
# convention is KST bar-start labels at the same timestamp on both sources.
FROZEN_BAR_LABEL_CONVENTION = "KRX_1M_BAR_START_KST_V1"
FROZEN_OFFSET_MINUTES = 0

REQUIRED_HIGHER_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h")


@dataclass(frozen=True)
class Invariant:
    """One immutable clause from the governing predicate contract."""

    identifier: str
    name: str
    rule: str

    def as_record(self) -> dict[str, str]:
        return {"id": self.identifier, "name": self.name, "rule": self.rule}


INVARIANT_SPEC: tuple[Invariant, ...] = (
    Invariant(
        "I1",
        "same_semantics",
        "KRX regular session; identical adjustment and timezone; completed "
        "session; latest-session exclusion applied first; and both sources "
        "contain the candidate minutes.",
    ),
    Invariant(
        "I2",
        "fixed_timestamp_convention",
        "Use only the frozen bar-label convention and offset; do not search "
        "for an offset after comparison.",
    ),
    Invariant(
        "I3",
        "isolated_adjacent_pairs",
        "The mismatch-minute set must partition exactly into disjoint adjacent "
        "two-minute pairs. Neighbours t-1 and t+2 must not mismatch; chains, "
        "one-sided minutes, and duplicate pairs fail.",
    ),
    Invariant(
        "I4",
        "outer_prices_exact",
        "open_A(t) equals open_B(t), and close_A(t+1) equals close_B(t+1).",
    ),
    Invariant(
        "I5",
        "two_minute_ohlc_exact",
        "The two-minute maximum high and minimum low are exactly equal.",
    ),
    Invariant(
        "I6",
        "two_minute_volume_exact",
        "The two-minute volume sum is exactly equal.",
    ),
    Invariant(
        "I7",
        "movement_cancels",
        "Per-minute volume deltas are non-zero and exact opposites.",
    ),
    Invariant(
        "I8",
        "fields_restricted",
        "Every OHLCV cell outside accepted pairs remains exact; synthetic or "
        "cumulative value is excluded from this predicate.",
    ),
    Invariant(
        "I9",
        "fail_closed",
        "Any failure remains an ordinary mismatch and makes the shard gate "
        "FAIL; neither mismatch size nor match-rate can rescue it.",
    ),
)


def predicate_spec_payload() -> dict[str, Any]:
    """Return the canonical payload whose hash identifies this frozen contract."""

    return {
        "name": PREDICATE_NAME,
        "version": PREDICATE_VERSION,
        "compared_fields": list(COMPARED_FIELDS),
        "normalisation_rule": NORMALISATION_RULE,
        "timestamp_convention": {
            "bar_label": FROZEN_BAR_LABEL_CONVENTION,
            "offset_minutes": FROZEN_OFFSET_MINUTES,
            "timezone": KST_TIMEZONE_NAME,
        },
        "invariants": [invariant.as_record() for invariant in INVARIANT_SPEC],
    }


def _spec_hash() -> str:
    payload = json.dumps(
        predicate_spec_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# The literal is intentionally checked at import time. Changing even one byte of
# the canonical contract without an explicit new version/hash makes the gate
# unavailable instead of silently reinterpreting prior audit records.
PREDICATE_SPEC_SHA256 = (
    "207d3f6f1a87413b8788446fde59e6fcad866478674fb99411c804df41de1e99"
)
if _spec_hash() != PREDICATE_SPEC_SHA256:
    raise RuntimeError(
        "ADJACENT_WINDOW_EQUIVALENT_V1 specification changed; issue a new "
        "version and frozen SHA-256 before use"
    )


@dataclass(frozen=True)
class ComparisonContext:
    """Preconditions that must be explicit before structural classification.

    The context has no defaults by design.  A caller cannot quietly omit the
    completed-session, KIS latest-session, adjustment, timezone, or timestamp
    convention checks and still receive an equivalence result.
    """

    source_a: str
    source_b: str
    market: str
    session_segment: str
    adjustment_a: str
    adjustment_b: str
    timezone_a: str
    timezone_b: str
    completed_session: bool
    latest_session_rule_applied_first: bool
    bar_label_convention: str
    offset_minutes: int


@dataclass(frozen=True)
class InvariantFailure:
    """An auditable failure instead of an exception that could hide a mismatch."""

    invariant: str
    detail: str

    def as_record(self) -> dict[str, str]:
        return {"invariant": self.invariant, "detail": self.detail}


@dataclass(frozen=True)
class MinuteMismatch:
    """All raw OHLCV fields that differ at one comparable minute."""

    minute: datetime
    fields: tuple[str, ...]

    def as_record(self) -> dict[str, Any]:
        return {"minute_kst": self.minute.isoformat(), "fields": list(self.fields)}


@dataclass(frozen=True)
class PairVerdict:
    """The structural outcome for one disjoint two-minute candidate pair."""

    t: datetime
    t_next: datetime
    equivalent: bool
    failures: tuple[InvariantFailure, ...] = ()

    def as_record(self) -> dict[str, Any]:
        return {
            "t_kst": self.t.isoformat(),
            "t_next_kst": self.t_next.isoformat(),
            "equivalent": self.equivalent,
            "failures": [failure.as_record() for failure in self.failures],
        }


@dataclass
class ClassificationResult:
    """Raw and structural verdicts kept separate for auditability."""

    symbol: str
    session: date
    common_minutes: int = 0
    raw_mismatches: list[MinuteMismatch] = field(default_factory=list)
    one_sided_minutes: list[datetime] = field(default_factory=list)
    accepted_pairs: list[PairVerdict] = field(default_factory=list)
    rejected_pairs: list[PairVerdict] = field(default_factory=list)
    invariant_failures: list[InvariantFailure] = field(default_factory=list)
    noncompliant_mismatch_cells: int = 0

    @property
    def raw_mismatch_cells(self) -> int:
        return sum(len(mismatch.fields) for mismatch in self.raw_mismatches)

    @property
    def raw_mismatch_minutes(self) -> int:
        return len(self.raw_mismatches)

    @property
    def raw_1m_exact(self) -> bool:
        """The raw verdict never changes when a pair is structurally accepted."""

        return (
            self.common_minutes > 0
            and self.raw_mismatch_cells == 0
            and not self.one_sided_minutes
        )

    @property
    def adjacent_window_equivalent(self) -> bool:
        """True only if every raw mismatch belongs to an accepted pair."""

        return not (
            self.invariant_failures
            or self.one_sided_minutes
            or self.rejected_pairs
            or self.noncompliant_mismatch_cells
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "predicate": PREDICATE_NAME,
            "predicate_version": PREDICATE_VERSION,
            "predicate_spec_sha256": PREDICATE_SPEC_SHA256,
            "cause_name": PREDICATE_NAME,
            "symbol": self.symbol,
            "session": self.session.isoformat(),
            "RAW_1M_EXACT": "PASS" if self.raw_1m_exact else "FAIL",
            "common_minutes": self.common_minutes,
            "raw_mismatch_cells": self.raw_mismatch_cells,
            "raw_mismatch_minutes": self.raw_mismatch_minutes,
            "raw_mismatch_locations": [
                mismatch.as_record() for mismatch in self.raw_mismatches
            ],
            "ADJACENT_WINDOW_EQUIVALENCE": (
                "PASS" if self.adjacent_window_equivalent else "FAIL"
            ),
            "exception_pair_count": len(self.accepted_pairs),
            "exception_pairs": [pair.as_record() for pair in self.accepted_pairs],
            "rejected_pairs": [pair.as_record() for pair in self.rejected_pairs],
            "one_sided_minutes": [
                minute.isoformat() for minute in self.one_sided_minutes
            ],
            "noncompliant_mismatch_cells": self.noncompliant_mismatch_cells,
            "invariant_failures": [
                failure.as_record() for failure in self.invariant_failures
            ],
        }


@dataclass(frozen=True)
class PhaseBEvidence:
    """Evidence required before a shard may receive the documented-exception pass.

    Constructing this object does not perform Phase B.  It only makes the
    required evidence explicit so an uncollected holdout cannot become a pass
    through a default value or a match-rate shortcut.
    """

    design_sessions: tuple[date, ...]
    holdout_sessions: tuple[date, ...]
    holdout_revalidation_completed: bool
    holdout_sessions_completed: bool
    higher_timeframe_bucket_exact: Mapping[str, str]


@dataclass(frozen=True)
class ShardGateDecision:
    """A fail-closed shard result; this module never enables two-way operation."""

    status: str
    reasons: tuple[str, ...]
    noncompliant_mismatch_cells: int

    @property
    def two_way_enabled(self) -> bool:
        return False

    def as_record(self) -> dict[str, Any]:
        return {
            "predicate": PREDICATE_NAME,
            "predicate_version": PREDICATE_VERSION,
            "predicate_spec_sha256": PREDICATE_SPEC_SHA256,
            "SHARD_GATE": self.status,
            "reasons": list(self.reasons),
            "noncompliant_mismatch_cells": self.noncompliant_mismatch_cells,
            "TWO_WAY_ENABLED": "NO",
        }


NormalisedBars = dict[datetime, dict[str, int]]


def classify(
    symbol: str,
    session: date,
    bars_a: Mapping[datetime, Mapping[str, Any]],
    bars_b: Mapping[datetime, Mapping[str, Any]],
    *,
    context: ComparisonContext,
) -> ClassificationResult:
    """Classify one symbol and completed KRX session without changing raw facts."""

    result = ClassificationResult(symbol=symbol, session=session)
    result.invariant_failures.extend(_validate_context(context))

    normalised_a, failures_a = _normalise_source_bars(bars_a, session, "A")
    normalised_b, failures_b = _normalise_source_bars(bars_b, session, "B")
    result.invariant_failures.extend(failures_a)
    result.invariant_failures.extend(failures_b)

    only_a = sorted(set(normalised_a) - set(normalised_b))
    only_b = sorted(set(normalised_b) - set(normalised_a))
    result.one_sided_minutes.extend((*only_a, *only_b))
    if result.one_sided_minutes:
        result.invariant_failures.append(
            InvariantFailure(
                "I1",
                "one-sided minute(s) cannot be structurally classified: "
                + ", ".join(minute.isoformat() for minute in result.one_sided_minutes),
            )
        )

    common_minutes = sorted(set(normalised_a) & set(normalised_b))
    result.common_minutes = len(common_minutes)
    if not common_minutes:
        result.invariant_failures.append(
            InvariantFailure("I1", "no comparable minute exists in both sources")
        )

    mismatch_fields_by_minute: dict[datetime, tuple[str, ...]] = {}
    for minute in common_minutes:
        fields = _mismatching_fields(normalised_a[minute], normalised_b[minute])
        if fields:
            mismatch_fields_by_minute[minute] = fields
            result.raw_mismatches.append(MinuteMismatch(minute, fields))

    # A bad semantic/input contract invalidates every raw mismatch; accepting a
    # pair after any such failure would make an unavailable fact look verified.
    if result.invariant_failures:
        result.noncompliant_mismatch_cells = result.raw_mismatch_cells
        return result

    mismatch_minutes = tuple(sorted(mismatch_fields_by_minute))
    if not mismatch_minutes:
        return result

    pairs, partition_failure = _partition_mismatch_minutes(mismatch_minutes)
    if partition_failure is not None:
        result.invariant_failures.append(partition_failure)
        result.invariant_failures.append(
            InvariantFailure(
                "I8",
                "because no complete accepted-pair partition exists, every raw "
                "OHLCV mismatch remains outside the accepted exception set",
            )
        )
        result.noncompliant_mismatch_cells = result.raw_mismatch_cells
        return result

    accepted_minutes: set[datetime] = set()
    for t, t_next in pairs:
        pair = _evaluate_pair(
            normalised_a,
            normalised_b,
            mismatch_fields_by_minute,
            t,
            t_next,
        )
        if pair.equivalent:
            result.accepted_pairs.append(pair)
            accepted_minutes.update((t, t_next))
        else:
            result.rejected_pairs.append(pair)

    noncompliant_minutes = set(mismatch_fields_by_minute) - accepted_minutes
    result.noncompliant_mismatch_cells = sum(
        len(mismatch_fields_by_minute[minute]) for minute in noncompliant_minutes
    )
    if noncompliant_minutes:
        result.invariant_failures.append(
            InvariantFailure(
                "I8",
                "OHLCV mismatch remains outside the accepted exception pair set",
            )
        )
        result.invariant_failures.append(
            InvariantFailure(
                "I9",
                "rejected or unpaired raw mismatch minute(s) remain ordinary "
                "mismatches",
            )
        )
    return result


def evaluate_shard_gate(
    results: Sequence[ClassificationResult],
    *,
    phase_b: PhaseBEvidence,
) -> ShardGateDecision:
    """Issue only the documented-exception verdict, never a two-way enablement.

    Phase A callers should pass their pending evidence.  The result is then
    necessarily ``FAIL``.  A future Phase B caller must supply independent,
    completed holdout sessions and all actual higher-timeframe bucket results.
    """

    reasons: list[str] = []
    if not results:
        reasons.append("no_classification_results")

    noncompliant = sum(result.noncompliant_mismatch_cells for result in results)
    for result in results:
        if not result.adjacent_window_equivalent:
            reasons.append(
                f"classification_failed:{result.symbol}:{result.session.isoformat()}"
            )

    design_sessions = set(phase_b.design_sessions)
    holdout_sessions = set(phase_b.holdout_sessions)
    if not design_sessions:
        reasons.append("design_sessions_not_declared")
    if not holdout_sessions:
        reasons.append("holdout_sessions_not_declared")
    if design_sessions & holdout_sessions:
        reasons.append("holdout_overlaps_design_sessions")
    if not phase_b.holdout_revalidation_completed:
        reasons.append("holdout_revalidation_not_completed")
    if not phase_b.holdout_sessions_completed:
        reasons.append("holdout_includes_uncompleted_session")
    if noncompliant:
        reasons.append("noncompliant_mismatch_cells_present")

    for timeframe in REQUIRED_HIGHER_TIMEFRAMES:
        if phase_b.higher_timeframe_bucket_exact.get(timeframe) != "PASS":
            reasons.append(f"higher_timeframe_bucket_not_exact:{timeframe}")

    status = "FAIL" if reasons else "PASS_WITH_DOCUMENTED_EXCEPTION"
    return ShardGateDecision(
        status=status,
        reasons=tuple(reasons),
        noncompliant_mismatch_cells=noncompliant,
    )


def _validate_context(context: ComparisonContext) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []
    source_a = context.source_a if isinstance(context.source_a, str) else ""
    source_b = context.source_b if isinstance(context.source_b, str) else ""
    if not source_a.strip() or not source_b.strip():
        failures.append(InvariantFailure("I1", "both source identities are required"))
    elif source_a == source_b:
        failures.append(InvariantFailure("I1", "the two source identities must differ"))
    if context.market != "KRX" or context.session_segment != KRX_REGULAR_SEGMENT:
        failures.append(
            InvariantFailure(
                "I1",
                "comparison must be explicitly limited to the KRX regular session",
            )
        )
    if not context.adjustment_a or context.adjustment_a != context.adjustment_b:
        failures.append(
            InvariantFailure("I1", "sources must declare the same adjustment basis")
        )
    if (
        context.timezone_a != KST_TIMEZONE_NAME
        or context.timezone_b != KST_TIMEZONE_NAME
    ):
        failures.append(
            InvariantFailure("I1", "both source timestamps must declare Asia/Seoul")
        )
    if context.completed_session is not True:
        failures.append(InvariantFailure("I1", "session is not declared complete"))
    if context.latest_session_rule_applied_first is not True:
        failures.append(
            InvariantFailure(
                "I1",
                "latest-session exclusion was not applied before this predicate",
            )
        )
    if context.bar_label_convention != FROZEN_BAR_LABEL_CONVENTION:
        failures.append(
            InvariantFailure(
                "I2",
                "bar-label convention differs from the frozen V1 convention",
            )
        )
    if (
        isinstance(context.offset_minutes, bool)
        or not isinstance(context.offset_minutes, int)
        or context.offset_minutes != FROZEN_OFFSET_MINUTES
    ):
        failures.append(
            InvariantFailure(
                "I2",
                "applied timestamp offset differs from the frozen V1 offset",
            )
        )
    return failures


def _normalise_source_bars(
    bars: Mapping[datetime, Mapping[str, Any]],
    session: date,
    source_label: str,
) -> tuple[NormalisedBars, list[InvariantFailure]]:
    normalised: NormalisedBars = {}
    failures: list[InvariantFailure] = []
    if not isinstance(bars, Mapping):
        return {}, [
            InvariantFailure("I1", f"source {source_label} bars are not a mapping")
        ]
    if not bars:
        failures.append(InvariantFailure("I1", f"source {source_label} has no bars"))

    for timestamp, raw_bar in bars.items():
        local_timestamp, timestamp_failure = _normalise_timestamp(timestamp, session)
        if timestamp_failure is not None:
            failures.append(timestamp_failure)
            continue
        if local_timestamp in normalised:
            failures.append(
                InvariantFailure(
                    "I3",
                    "duplicate normalized timestamp in source "
                    f"{source_label}: {local_timestamp.isoformat()}",
                )
            )
            continue
        bar, bar_failures = _normalise_bar(raw_bar, source_label, local_timestamp)
        failures.extend(bar_failures)
        if bar is not None:
            normalised[local_timestamp] = bar
    return normalised, failures


def _normalise_timestamp(
    timestamp: object,
    session: date,
) -> tuple[datetime | None, InvariantFailure | None]:
    if not isinstance(timestamp, datetime):
        return None, InvariantFailure("I1", "bar timestamp is not a datetime")
    if timestamp.tzinfo is None or timestamp.utcoffset() != KST_UTC_OFFSET:
        return None, InvariantFailure(
            "I1",
            "bar timestamp is not timezone-aware Asia/Seoul (+09:00)",
        )
    local = timestamp.astimezone(KST)
    if local.date() != session:
        return None, InvariantFailure(
            "I1",
            "bar timestamp session does not match the declared completed session",
        )
    if not _is_krx_regular_minute(local):
        return None, InvariantFailure(
            "I1",
            "bar timestamp is outside the declared KRX regular session",
        )
    return local, None


def _normalise_bar(
    raw_bar: object,
    source_label: str,
    timestamp: datetime,
) -> tuple[dict[str, int] | None, list[InvariantFailure]]:
    if not isinstance(raw_bar, Mapping):
        return None, [
            InvariantFailure(
                "I1",
                f"source {source_label} bar at {timestamp.isoformat()} is not a mapping",
            )
        ]

    normalised: dict[str, int] = {}
    failures: list[InvariantFailure] = []
    for field_name in COMPARED_FIELDS:
        if field_name not in raw_bar:
            failures.append(
                InvariantFailure(
                    "I1",
                    f"source {source_label} bar at {timestamp.isoformat()} "
                    f"omits {field_name}",
                )
            )
            continue
        try:
            normalised[field_name] = _normalise_value(raw_bar[field_name])
        except (OverflowError, TypeError, ValueError):
            failures.append(
                InvariantFailure(
                    "I1",
                    f"source {source_label} bar at {timestamp.isoformat()} has "
                    f"non-finite or non-numeric {field_name}",
                )
            )
    return (normalised if not failures else None), failures


def _normalise_value(value: object) -> int:
    if isinstance(value, (bool, str, bytes)):
        raise TypeError("not a numeric candle value")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("not finite")
    return int(round(converted))


def _mismatching_fields(a: Mapping[str, int], b: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(
        field_name for field_name in COMPARED_FIELDS if a[field_name] != b[field_name]
    )


def _partition_mismatch_minutes(
    minutes: Sequence[datetime],
) -> tuple[list[tuple[datetime, datetime]], InvariantFailure | None]:
    """Enforce I3 before looking at any candidate pair's aggregate values."""

    if not minutes:
        return [], None
    if len(set(minutes)) != len(minutes):
        return [], InvariantFailure(
            "I3", "duplicate mismatch minute cannot form a pair"
        )
    ordered = sorted(minutes)
    if len(ordered) % 2:
        return [], InvariantFailure(
            "I3", "odd number of mismatch minutes cannot partition into pairs"
        )

    pairs: list[tuple[datetime, datetime]] = []
    used_minutes: set[datetime] = set()
    for index in range(0, len(ordered), 2):
        t, t_next = ordered[index], ordered[index + 1]
        if t_next - t != timedelta(minutes=1):
            return [], InvariantFailure(
                "I3",
                "mismatch minutes are not adjacent: "
                f"{t.isoformat()} and {t_next.isoformat()}",
            )
        if pairs and t - pairs[-1][1] == timedelta(minutes=1):
            return [], InvariantFailure(
                "I3", f"three-or-more-minute mismatch chain begins at {t.isoformat()}"
            )
        if t in used_minutes or t_next in used_minutes:
            return [], InvariantFailure("I3", "duplicate or overlapping pair")
        pairs.append((t, t_next))
        used_minutes.update((t, t_next))
    return pairs, None


def _evaluate_pair(
    bars_a: NormalisedBars,
    bars_b: NormalisedBars,
    mismatch_fields_by_minute: Mapping[datetime, tuple[str, ...]],
    t: datetime,
    t_next: datetime,
) -> PairVerdict:
    failures = _isolation_failures(bars_a, bars_b, mismatch_fields_by_minute, t, t_next)
    a_t, a_next = bars_a[t], bars_a[t_next]
    b_t, b_next = bars_b[t], bars_b[t_next]

    if a_t["open"] != b_t["open"]:
        failures.append(InvariantFailure("I4", "opening price differs at t"))
    if a_next["close"] != b_next["close"]:
        failures.append(InvariantFailure("I4", "closing price differs at t+1"))
    if max(a_t["high"], a_next["high"]) != max(b_t["high"], b_next["high"]):
        failures.append(InvariantFailure("I5", "two-minute high differs"))
    if min(a_t["low"], a_next["low"]) != min(b_t["low"], b_next["low"]):
        failures.append(InvariantFailure("I5", "two-minute low differs"))

    volume_a = a_t["volume"] + a_next["volume"]
    volume_b = b_t["volume"] + b_next["volume"]
    if volume_a != volume_b:
        failures.append(InvariantFailure("I6", "two-minute volume sum differs"))

    delta_t = a_t["volume"] - b_t["volume"]
    delta_next = a_next["volume"] - b_next["volume"]
    if delta_t == 0 or delta_next == 0:
        failures.append(
            InvariantFailure("I7", "volume movement must be non-zero in both minutes")
        )
    if delta_t != -delta_next:
        failures.append(InvariantFailure("I7", "volume deltas do not cancel"))

    return PairVerdict(t, t_next, equivalent=not failures, failures=tuple(failures))


def _isolation_failures(
    bars_a: NormalisedBars,
    bars_b: NormalisedBars,
    mismatch_fields_by_minute: Mapping[datetime, tuple[str, ...]],
    t: datetime,
    t_next: datetime,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []
    for neighbour in (t - timedelta(minutes=1), t_next + timedelta(minutes=1)):
        if not _is_krx_regular_minute(neighbour):
            continue
        if neighbour not in bars_a or neighbour not in bars_b:
            failures.append(
                InvariantFailure(
                    "I3",
                    "cannot prove pair isolation because adjacent regular-session "
                    f"minute is absent: {neighbour.isoformat()}",
                )
            )
        elif neighbour in mismatch_fields_by_minute:
            failures.append(
                InvariantFailure(
                    "I3",
                    "mismatch reaches an adjacent regular-session minute: "
                    f"{neighbour.isoformat()}",
                )
            )
    return failures


def _is_krx_regular_minute(timestamp: datetime) -> bool:
    minute = timestamp.timetz().replace(tzinfo=None)
    return REGULAR_SESSION_START <= minute <= REGULAR_SESSION_END
