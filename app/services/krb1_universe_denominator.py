"""F-INT-03 — the universe denominator must come from outside our database.

``#1729`` proved "full universe coverage" by comparing ``count(*)`` with the rows
from the same transaction on the same table: a truncated universe shrank both sides
together and still passed. ROB-1172 bound the denominator to the append-only
metadata snapshot's sealed ``symbol_count`` instead, which catches a truncation
that happens *after* a snapshot exists.

🔴 It does not catch the first snapshot. If the database is already short when the
very first snapshot is captured, capture reads those rows, requests exactly those
symbols, hashes that payload, and seals that count. Every number agrees and the
gate passes — the sealed basis is a notarized copy of the same possibly-truncated
read. Sealing establishes *when* we knew a number, never that the number was the
whole market.

Proving it requires an official listed-instrument count for the market and session
from a source that is not us. No such source is wired: KRX official listing counts
are ROB-1175, and the wired Toss ``/api/v1/stocks`` projection is a paginated view
of instruments with no declared total (D1's ``NO_ADMISSIBLE_TOSS_SOURCE`` covers the
same surface). So this module is a typed ``UNPROVABLE`` stub, shaped like the
provider-finality and reference-exception adapters:

* :data:`EXTERNAL_DENOMINATOR_SOURCE_WIRED` is ``False``, so no input proves the
  gate — including one handed in at the value boundary;
* :data:`ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES` is empty, so admissibility cannot
  leak through an unenumerated name; and
* self-referential sources (our own rows, our own sealed count, an operator
  signature) are refused by name at construction.

🔴 This is deliberately a blocking stub and not a new proof path. The selector must
report that full-universe coverage is unprovable, not manufacture a denominator it
does not have.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, runtime_checkable

from app.services.krb1_gate_result import (
    GateResult,
    is_aware,
    is_sha256_hex,
    normalize_evidence,
    proven,
    unprovable,
)

SCHEMA_VERSION = "krb1.p0_3.universe_denominator.v1"

#: No external listed-instrument count is wired. Not configuration: flipping this
#: requires implementing :class:`ExternalUniverseDenominatorSource` against a real
#: official source (ROB-1175), in a separate reviewed change.
EXTERNAL_DENOMINATOR_SOURCE_WIRED: Final[bool] = False

#: Admissible external denominator sources. **EMPTY on purpose** — the load-bearing
#: closure, because an allowlist cannot leak an unenumerated source.
ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES: frozenset[str] = frozenset()

#: 🔴 Named refusals: bases that are our own read wearing an external label, plus
#: the operator attestation that ROB-1172 E1/D1 rejected outright.
REFUSED_SELF_REFERENTIAL_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "db_universe_rows",
        "kr_symbol_universe",
        "metadata_snapshot_symbol_count",
        "operator",
        "operator_attestation",
        "selector_expected_universe_counts",
        "toss_stocks_page_row_count",
    }
)

FAIL_CLOSED_REASON: Final[str] = "external_universe_denominator_source_not_wired"
FAIL_CLOSED_STATUS: Final[str] = "unprovable"
UNPROVEN_REASON: Final[str] = "universe_denominator_external_basis_unproven"
PROVEN_REASON: Final[str] = "universe_denominator_externally_bound"


class ExternalDenominatorNotWired(RuntimeError):
    """Raised when a caller tries to make the unwired stub produce a denominator."""


@dataclass(frozen=True, slots=True)
class ExternalUniverseDenominator:
    """An official count of listed instruments for one market and session.

    Every field is provider-origin: the count, the clock the source published it
    at, when we retrieved it, and the digest of the payload it came from.
    """

    market: str
    session_date: dt.date
    source: str
    listed_count: int
    published_at: dt.datetime
    retrieved_at: dt.datetime
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        if self.source in REFUSED_SELF_REFERENTIAL_SOURCES:
            raise ExternalDenominatorNotWired(
                f"{self.source!r} is our own read or an operator signature; it "
                "cannot be the external basis for its own denominator"
            )
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("source must name provider-origin evidence")
        if type(self.listed_count) is not int or self.listed_count <= 0:
            raise ValueError("listed_count must be a positive integer count")
        if not is_aware(self.published_at) or not is_aware(self.retrieved_at):
            raise ValueError("denominator clocks must be timezone-aware")
        if not is_sha256_hex(self.raw_payload_sha256):
            raise ValueError(
                "raw_payload_sha256 must be a 64-character SHA-256 hex digest of "
                "the raw source payload"
            )

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "listed_count": self.listed_count,
                "market": self.market,
                "published_at": self.published_at,
                "raw_payload_sha256": self.raw_payload_sha256,
                "retrieved_at": self.retrieved_at,
                "session_date": self.session_date,
                "source": self.source,
            }
        )


@runtime_checkable
class ExternalUniverseDenominatorSource(Protocol):
    """Contract a future official listed-count source must satisfy."""

    def fetch(
        self,
        *,
        market: str,
        session_date: dt.date,
        decision_at: dt.datetime,
    ) -> tuple[ExternalUniverseDenominator, ...]: ...


@dataclass(frozen=True, slots=True)
class DenominatorFetchResult:
    """Fail-closed fetch outcome. Structurally incapable of carrying a pass."""

    market: str
    session_date: str
    decision_at: str
    status: Literal["unprovable"] = FAIL_CLOSED_STATUS  # type: ignore[assignment]
    reason: str = FAIL_CLOSED_REASON
    denominators: tuple[ExternalUniverseDenominator, ...] = field(default_factory=tuple)
    source_wired: bool = EXTERNAL_DENOMINATOR_SOURCE_WIRED
    sealed_count_is_not_external_basis: bool = True

    def __post_init__(self) -> None:
        if self.denominators:
            raise ExternalDenominatorNotWired(
                "the external-denominator adapter is a fail-closed stub; it cannot "
                "carry a denominator"
            )
        if self.status != FAIL_CLOSED_STATUS:
            raise ExternalDenominatorNotWired(
                f"status must remain {FAIL_CLOSED_STATUS!r} while no external "
                "denominator source is wired"
            )
        if self.reason != FAIL_CLOSED_REASON:
            raise ExternalDenominatorNotWired(
                f"reason must remain {FAIL_CLOSED_REASON!r} while no external "
                "denominator source is wired"
            )
        if self.source_wired:
            raise ExternalDenominatorNotWired(
                "source_wired cannot be asserted by a caller"
            )

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "decision_at": self.decision_at,
                "denominator_count": len(self.denominators),
                "market": self.market,
                "reason": self.reason,
                "schema_version": SCHEMA_VERSION,
                "sealed_count_is_not_external_basis": (
                    self.sealed_count_is_not_external_basis
                ),
                "session_date": self.session_date,
                "source_wired": self.source_wired,
                "status": self.status,
            }
        )


def fetch_external_universe_denominator(
    *,
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
) -> DenominatorFetchResult:
    """Return the fail-closed outcome. There is no success branch."""
    return DenominatorFetchResult(
        market=market,
        session_date=session_date.isoformat(),
        decision_at=decision_at.isoformat(),
    )


def evaluate_universe_denominator(
    *,
    denominators: Iterable[ExternalUniverseDenominator],
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
    sealed_count: int | None,
    actual_count: int,
    source_unavailable_reason: str | None = None,
) -> GateResult:
    """Gate: is the coverage denominator bound to a count from outside our database?

    ``sealed_count`` (the append-only metadata snapshot's ``symbol_count``) and
    ``actual_count`` (the rows the selector read) are both *our* numbers. They are
    recorded and cross-checked, but agreement between them is not proof: this gate
    is about whether an external source says that number is the whole market.
    """
    required = {
        "required_external_denominator": True,
        "external_denominator_source_wired": EXTERNAL_DENOMINATOR_SOURCE_WIRED,
        "required_admissible_sources": sorted(ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES),
        "refused_self_referential_sources": sorted(REFUSED_SELF_REFERENTIAL_SOURCES),
        "sealed_count_is_not_external_basis": True,
        "sealing_proves_when_not_completeness": True,
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "sealed_count": sealed_count,
        "actual_count": actual_count,
        "session_date": session_date.isoformat(),
        "market": market,
    }
    candidates = tuple(denominators)
    if not is_aware(decision_at):
        return unprovable(
            "universe_denominator_decision_clock_not_timezone_aware", **required
        )
    # 🔴 While no external source is wired, no input proves this gate. A first
    # snapshot of an already-truncated database must stay unprovable rather than
    # notarize its own shortfall.
    if not EXTERNAL_DENOMINATOR_SOURCE_WIRED:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_source_not_wired",
            denominator_count=len(candidates),
            submitted_sources=sorted({item.source for item in candidates}),
            first_snapshot_of_a_truncated_universe_is_self_proving=True,
            **required,
        )
    if source_unavailable_reason is not None:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_source_unavailable",
            source_unavailable_reason=source_unavailable_reason,
            **required,
        )
    if sealed_count is None:
        return unprovable(
            UNPROVEN_REASON,
            defect="no_sealed_denominator_to_bind",
            **required,
        )
    matching = [
        item
        for item in candidates
        if item.market == market and item.session_date == session_date
    ]
    if not matching:
        return unprovable(
            UNPROVEN_REASON,
            defect="no_external_denominator_for_market_session",
            **required,
        )
    if len(matching) > 1:
        return unprovable(
            UNPROVEN_REASON,
            defect="conflicting_external_denominators",
            denominator_count=len(matching),
            **required,
        )
    denominator = matching[0]
    if denominator.source in REFUSED_SELF_REFERENTIAL_SOURCES:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_source_self_referential",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.source not in ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_source_not_admissible",
            denominator=denominator.as_evidence(),
            **required,
        )
    if not is_sha256_hex(denominator.raw_payload_sha256):
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_payload_hash_malformed",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.published_at > decision_at:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_published_after_decision_at",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.retrieved_at > decision_at:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_retrieved_after_decision_at",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.published_at > denominator.retrieved_at:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_denominator_published_after_retrieval_clock",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.listed_count != sealed_count:
        return unprovable(
            UNPROVEN_REASON,
            defect="universe_denominator_disagrees_with_external_basis",
            denominator=denominator.as_evidence(),
            **required,
        )
    if denominator.listed_count != actual_count:
        return unprovable(
            UNPROVEN_REASON,
            defect="external_basis_disagrees_with_selected_rows",
            denominator=denominator.as_evidence(),
            **required,
        )
    return proven(
        PROVEN_REASON,
        denominator=denominator.as_evidence(),
        **required,
    )


def evaluate_with_stub(
    *,
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
    sealed_count: int | None,
    actual_count: int,
) -> tuple[GateResult, DenominatorFetchResult]:
    """Run the fail-closed stub and convert its outcome into the blocking gate."""
    fetched = fetch_external_universe_denominator(
        market=market, session_date=session_date, decision_at=decision_at
    )
    gate = evaluate_universe_denominator(
        denominators=fetched.denominators,
        market=market,
        session_date=session_date,
        decision_at=decision_at,
        sealed_count=sealed_count,
        actual_count=actual_count,
        source_unavailable_reason=fetched.reason,
    )
    return gate, fetched


__all__ = [
    "ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES",
    "EXTERNAL_DENOMINATOR_SOURCE_WIRED",
    "FAIL_CLOSED_REASON",
    "FAIL_CLOSED_STATUS",
    "PROVEN_REASON",
    "REFUSED_SELF_REFERENTIAL_SOURCES",
    "SCHEMA_VERSION",
    "UNPROVEN_REASON",
    "DenominatorFetchResult",
    "ExternalDenominatorNotWired",
    "ExternalUniverseDenominator",
    "ExternalUniverseDenominatorSource",
    "evaluate_universe_denominator",
    "evaluate_with_stub",
    "fetch_external_universe_denominator",
]
