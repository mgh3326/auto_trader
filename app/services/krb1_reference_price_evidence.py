"""AC3 — reference-price (기준가) exception evidence with a separated clock model.

The 07-29 gate conflated two different clocks into one ``source_as_of`` field and
then required ``source_as_of >= target_session``. That is structurally
unsatisfiable: a decision taken on 07-29 18:00 KST cannot hold a snapshot stamped
on 07-30. The gate therefore blocked for a reason that no amount of correct
operating could ever clear.

The model here separates the two clocks that actually matter:

* ``effective_session`` — the session the published base price applies to. Must
  equal the selector's ``target_session``. A notice for another session proves
  nothing about the target session.
* ``published_at`` — when the authority published it.
* ``retrieved_at`` — when we fetched it.

Rule:

    effective_session == target_session
    AND published_at <= decision_at
    AND retrieved_at <= decision_at
    AND published_at <= retrieved_at

This is not a relaxation of the gate. The authoritative-source allowlist is
unchanged, raw provenance is still mandatory, per-symbol coverage of the whole
eligible universe is still mandatory, and both clocks are now bounded above by
``decision_at`` — a bound the old single-clock model did not have at all. What
changes is that the requirement is now about *pre-decision publication* instead of
a post-decision snapshot date.

While no authoritative source is wired (see
:mod:`app.services.krb1_reference_exception_adapter`), this gate still fails
closed — now with the honest reason that the source is not wired.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.services.krb1_gate_result import (
    GateResult,
    examples,
    is_aware,
    is_sha256_hex,
    normalize_evidence,
    parse_nonnegative_int_string,
    proven,
    unprovable,
)

SCHEMA_VERSION = "krb1.p0_3.reference_price_evidence.v2"

# A4: the numeric reference price and *how it was determined* are different facts.
# Some corporate actions (capital reduction, relisting after a split, changed
# listing) have their base price produced by the target session's opening call, so
# the number does not exist on the prior evening. Requiring it for every record
# made the gate unsatisfiable for the whole market whenever one such symbol was
# eligible; classifying the method separates "we could not prove it" from "the
# number does not exist yet".
DeterminationMethod = Literal[
    "NORMAL_PRIOR_CLOSE",
    "PRECOMPUTED_THEORETICAL",
    "TARGET_DAY_OPENING_CALL",
    "UNKNOWN",
]

#: Methods whose numeric reference price must already exist before the decision.
NUMERIC_REQUIRED_METHODS: frozenset[str] = frozenset(
    {"NORMAL_PRIOR_CLOSE", "PRECOMPUTED_THEORETICAL"}
)
#: Methods that are positively classified but have no number until the target
#: session's opening call. Excluded from ranking; never estimated, never awaited.
NUMERIC_DEFERRED_METHODS: frozenset[str] = frozenset({"TARGET_DAY_OPENING_CALL"})
#: Anything unresolved fails the whole run.
RUN_FAIL_CLOSE_METHODS: frozenset[str] = frozenset({"UNKNOWN"})
DETERMINATION_METHODS: frozenset[str] = (
    NUMERIC_REQUIRED_METHODS | NUMERIC_DEFERRED_METHODS | RUN_FAIL_CLOSE_METHODS
)

# Only an official KRX base-price publication counts. No screener, quote wrapper,
# derived table, or operator assertion can satisfy this.
AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES = frozenset({"krx_official_base_price"})

OUTCOME_RULE = (
    "effective_session == target_session "
    "AND published_at <= decision_at AND retrieved_at <= decision_at"
)


@dataclass(frozen=True, slots=True)
class ReferencePriceExceptionRecord:
    """One authoritative statement about a symbol's target-session base price."""

    symbol: str
    effective_session: dt.date
    is_exception: bool | None
    source: str
    published_at: dt.datetime
    retrieved_at: dt.datetime
    determination_method: str = "UNKNOWN"
    raw_reference_price: str | None = None
    raw_reason_code: str | None = None
    raw_payload_sha256: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "determination_method": self.determination_method,
                "effective_session": self.effective_session,
                "is_exception": self.is_exception,
                "published_at": self.published_at,
                "raw_payload_sha256": self.raw_payload_sha256,
                "raw_reason_code": self.raw_reason_code,
                "raw_reference_price": self.raw_reference_price,
                "retrieved_at": self.retrieved_at,
                "source": self.source,
                "symbol": self.symbol,
            }
        )


def classify_record(
    record: ReferencePriceExceptionRecord,
    *,
    target_session: dt.date,
    decision_at: dt.datetime,
) -> str:
    """Return ``"proven"`` or a named defect for one record."""
    if record.source not in AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES:
        return "source_not_authoritative"
    if record.effective_session != target_session:
        return "effective_session_not_target_session"
    if record.is_exception is None:
        return "exception_flag_missing"
    if not is_aware(record.published_at) or not is_aware(record.retrieved_at):
        return "clock_not_timezone_aware"
    if not is_aware(decision_at):
        # Never compare against a clock we cannot place on a timeline.
        return "decision_clock_not_timezone_aware"
    if record.published_at > decision_at:
        return "published_after_decision_at"
    if record.retrieved_at > decision_at:
        return "retrieved_after_decision_at"
    if record.published_at > record.retrieved_at:
        return "retrieved_before_published"
    if record.determination_method not in DETERMINATION_METHODS:
        return "determination_method_unrecognized"
    if record.determination_method in RUN_FAIL_CLOSE_METHODS:
        return "determination_method_unknown"
    if not record.raw_reason_code:
        return "raw_reason_code_missing"
    if not is_sha256_hex(record.raw_payload_sha256):
        return "raw_payload_hash_missing"
    numeric = parse_nonnegative_int_string(record.raw_reference_price)
    if record.determination_method in NUMERIC_REQUIRED_METHODS:
        if numeric in (None, 0):
            return "raw_reference_price_missing"
        return "proven"
    # TARGET_DAY_OPENING_CALL: the method is positively classified, so this is not a
    # proof failure. The number is produced by the target session's opening call, so
    # it must be neither awaited nor estimated — the symbol leaves the ranking.
    if numeric not in (None, 0):
        return "numeric_reference_price_before_opening_call_determination"
    return "excluded_pending_opening_call"


def evaluate_reference_price_exception_coverage(
    *,
    records: Iterable[ReferencePriceExceptionRecord],
    required_symbols: Iterable[str],
    target_session: dt.date,
    decision_at: dt.datetime,
    source_unavailable_reason: str | None = None,
) -> GateResult:
    """Gate: is every eligible symbol's target-session base price proven?"""
    required = {
        "required_authoritative_sources": sorted(
            AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES
        ),
        "outcome_rule": OUTCOME_RULE,
        "target_session": target_session.isoformat(),
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "fallback_forbidden": True,
    }
    expected = sorted({str(symbol) for symbol in required_symbols})
    indexed: dict[str, ReferencePriceExceptionRecord] = {}
    duplicates: list[str] = []
    for record in records:
        if record.symbol in indexed:
            duplicates.append(record.symbol)
        else:
            indexed[record.symbol] = record

    if not is_aware(decision_at):
        return unprovable(
            "reference_price_exception_decision_clock_not_timezone_aware", **required
        )
    if source_unavailable_reason is not None:
        return unprovable(
            "target_session_reference_price_exception_unproven",
            defect="authoritative_source_unavailable",
            source_unavailable_reason=source_unavailable_reason,
            expected_count=len(expected),
            supplied_record_count=len(indexed),
            **required,
        )
    if not expected:
        return unprovable(
            "target_session_reference_price_exception_unproven",
            defect="no_symbols_to_prove",
            expected_count=0,
            **required,
        )
    if duplicates:
        return unprovable(
            "target_session_reference_price_exception_unproven",
            defect="duplicate_records",
            duplicate_symbols=examples(sorted(set(duplicates))),
            **required,
        )

    missing = [symbol for symbol in expected if symbol not in indexed]
    defects: list[dict[str, str]] = []
    excluded_pending_opening_call: list[str] = []
    for symbol in expected:
        record = indexed.get(symbol)
        if record is None:
            continue
        verdict = classify_record(
            record, target_session=target_session, decision_at=decision_at
        )
        if verdict == "excluded_pending_opening_call":
            excluded_pending_opening_call.append(symbol)
        elif verdict != "proven":
            defects.append({"symbol": symbol, "defect": verdict})
    extra = sorted(set(indexed) - set(expected))
    if missing or defects or extra:
        return unprovable(
            "target_session_reference_price_exception_unproven",
            expected_count=len(expected),
            missing_count=len(missing),
            missing_examples=examples(missing),
            defect_count=len(defects),
            defect_examples=[normalize_evidence(item) for item in defects[:20]],
            outside_expected_universe_count=len(extra),
            outside_expected_universe_examples=examples(extra),
            excluded_pending_opening_call=examples(
                sorted(excluded_pending_opening_call)
            ),
            **required,
        )
    return proven(
        "target_session_reference_price_exception_coverage_proven",
        checked_count=len(expected),
        exception_symbols=examples(
            sorted(
                symbol
                for symbol in expected
                if indexed[symbol].is_exception
                and symbol not in set(excluded_pending_opening_call)
            )
        ),
        excluded_pending_opening_call=examples(sorted(excluded_pending_opening_call)),
        excluded_pending_opening_call_count=len(excluded_pending_opening_call),
        numeric_price_not_awaited_or_estimated=True,
        **required,
    )


def excluded_pending_opening_call_symbols(
    *,
    records: Iterable[ReferencePriceExceptionRecord],
    target_session: dt.date,
    decision_at: dt.datetime,
) -> tuple[str, ...]:
    """Symbols whose base price is set by the target session's opening call.

    They are positively classified, not unproven — so they leave the ranking
    population instead of failing the run. Whether the run may then select the next
    rank is a separate contract question; see the selector's
    ``ranked_candidate`` gate, which refuses automatic promotion.
    """
    return tuple(
        sorted(
            record.symbol
            for record in records
            if classify_record(
                record, target_session=target_session, decision_at=decision_at
            )
            == "excluded_pending_opening_call"
        )
    )


def is_tradable_reference_price(record: ReferencePriceExceptionRecord) -> bool:
    """True only when the authority says it is not an exception *and* the number exists.

    A ``TARGET_DAY_OPENING_CALL`` symbol is excluded here even if
    ``is_exception is False``: its base price is produced by the target session's
    opening call, so at decision time there is nothing to rank it on.
    """
    if record.is_exception is not False:
        return False
    if record.determination_method not in NUMERIC_REQUIRED_METHODS:
        return False
    return parse_nonnegative_int_string(record.raw_reference_price) not in (None, 0)


__all__ = [
    "AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES",
    "DETERMINATION_METHODS",
    "NUMERIC_DEFERRED_METHODS",
    "NUMERIC_REQUIRED_METHODS",
    "RUN_FAIL_CLOSE_METHODS",
    "DeterminationMethod",
    "excluded_pending_opening_call_symbols",
    "OUTCOME_RULE",
    "SCHEMA_VERSION",
    "ReferencePriceExceptionRecord",
    "classify_record",
    "evaluate_reference_price_exception_coverage",
    "is_tradable_reference_price",
]
