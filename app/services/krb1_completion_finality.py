"""A3 — provider daily-OHLCV finality, split away from local reconciliation.

Completion was one axis and it conflated two different claims:

* **local_reconcile** — the stored row and the raw daily response agree, the whole
  universe is covered, and the observation window is bounded. That is what
  :mod:`app.services.krb1_completion_manifest` proves.
* **provider_finality** — the provider guarantees that this daily OHLCV/value
  revision is *final*: a declared complete-session marker, a revision identity,
  and correction/rewrite semantics.

An exact local comparison cannot produce the second claim. Both sides of the
comparison are our own reads of a surface that never said "this is final", so
agreement between them proves consistency, not finality. Promoting it would mean
the selector's evidence asserts a provider guarantee that no provider gave.

No wired surface carries such an attestation: ``RawDailyBar`` has no revision
field, ``rt_cd`` is a transport status, and the KIS daily TR exposes no
end-of-session marker. So this module is a typed ``UNPROVABLE`` stub with the same
shape as the reference-exception adapter — it cannot be talked into producing a
pass, and the selector fails closed.

🔴 ROB-1172 F-INT-01 (2026-07-30): the first version of this split only made the
*fetch* path fail closed. The value boundary still accepted a hand-built
attestation, so an ``operator_attestation`` with ``raw_payload_sha256`` set to
``"not-a-sha256"`` reached ``proven`` while ``FINALITY_SOURCE_WIRED`` was ``False``.
That is the rejected fallback wearing evidence clothing. The gate now refuses it
three independent ways — unwired source, inadmissible/self-asserted source name,
malformed payload digest — and the first two of those also refuse *every* other
input, which is what an unwired stub has to mean.

🔴 Toss ``investor-trading.updatedAt`` is **not** a candidate here. It carries real
provisional/final semantics, but only for the KRX investor-trading-amount domain;
using it for daily OHLCV would be a cross-domain substitution. Wiring a KRX
official operational source is a separate source-contract issue.
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

SCHEMA_VERSION = "krb1.p0_3.completion_finality.v1"

#: No provider surface attests daily OHLCV finality. Not configuration: flipping
#: this requires implementing :class:`ProviderFinalitySource` against a real
#: provider guarantee, in a separate reviewed change.
FINALITY_SOURCE_WIRED: Final[bool] = False

FAIL_CLOSED_REASON: Final[str] = "provider_daily_ohlcv_finality_source_not_wired"
FAIL_CLOSED_STATUS: Final[str] = "unprovable"

#: Sources admissible as daily-OHLCV finality evidence. **EMPTY on purpose.** This
#: is the load-bearing closure: admissibility is an allowlist, so an unenumerated
#: source cannot leak through — anything not listed here is refused, and nothing is
#: listed. Populating it requires a verified provider finality contract in a
#: separate reviewed change. It is not configuration.
ADMISSIBLE_FINALITY_SOURCES: frozenset[str] = frozenset()

#: 🔴 Named refusals for sources that were *explicitly rejected*, so the rejection
#: is attributable rather than merely implied by the empty allowlist above.
#: ROB-1172 E1/D1 (2026-07-30): "operator attestation is not adopted — an operator
#: signature cannot create the external fact of a provider publication/effective
#: clock. Not usable as a fallback unless re-tabled by a separate study."
#: An operator-signed row is a statement about us, not about the provider.
REFUSED_SELF_ASSERTED_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "analyst_attestation",
        "db_reconcile",
        "human_attestation",
        "local_reconcile",
        "manual_attestation",
        "operator",
        "operator_attestation",
        "operator_signature",
        "self_attestation",
    }
)

#: Domains whose finality semantics must never be reused for daily OHLCV.
FORBIDDEN_CROSS_DOMAIN_SOURCES: Final[frozenset[str]] = frozenset(
    {"toss_investor_trading_updated_at", "toss_exchange_rate_valid_window"}
)


class ProviderFinalityNotWired(RuntimeError):
    """Raised when a caller tries to make the unwired stub produce an attestation."""


@dataclass(frozen=True, slots=True)
class ProviderFinalityAttestation:
    """A provider statement that one session's daily bars are the final revision.

    Every field is provider-origin. ``revision`` identifies which revision is being
    declared final, and ``correction_policy`` records what the provider says
    happens if it is later corrected — without that, "final" has no meaning.
    """

    market: str
    session_date: dt.date
    source: str
    revision: str
    declared_final_at: dt.datetime
    retrieved_at: dt.datetime
    correction_policy: str
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        if self.source in FORBIDDEN_CROSS_DOMAIN_SOURCES:
            raise ProviderFinalityNotWired(
                f"{self.source!r} carries finality semantics for a different domain "
                "and cannot attest daily OHLCV finality"
            )
        # 🔴 The rejected fallback. An operator signature is evidence about the
        # operator, not about the provider clock, so it cannot be built at all.
        if self.source in REFUSED_SELF_ASSERTED_SOURCES:
            raise ProviderFinalityNotWired(
                f"{self.source!r} is self-asserted: it cannot create the external "
                "fact of a provider finality declaration (ROB-1172 E1/D1)"
            )
        if not is_aware(self.declared_final_at) or not is_aware(self.retrieved_at):
            raise ValueError("finality clocks must be timezone-aware")
        for name, value in (
            ("source", self.source),
            ("revision", self.revision),
            ("correction_policy", self.correction_policy),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be provider-origin, non-empty evidence")
        # A hash that is not a hash binds the attestation to nothing.
        if not is_sha256_hex(self.raw_payload_sha256):
            raise ValueError(
                "raw_payload_sha256 must be a 64-character SHA-256 hex digest of "
                "the raw provider payload"
            )

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "correction_policy": self.correction_policy,
                "declared_final_at": self.declared_final_at,
                "market": self.market,
                "raw_payload_sha256": self.raw_payload_sha256,
                "retrieved_at": self.retrieved_at,
                "revision": self.revision,
                "session_date": self.session_date,
                "source": self.source,
            }
        )


@runtime_checkable
class ProviderFinalitySource(Protocol):
    """Contract a future authoritative finality source must satisfy."""

    def fetch(
        self,
        *,
        market: str,
        session_date: dt.date,
        decision_at: dt.datetime,
    ) -> tuple[ProviderFinalityAttestation, ...]: ...


@dataclass(frozen=True, slots=True)
class FinalityFetchResult:
    """Fail-closed fetch outcome. Structurally incapable of carrying a pass."""

    market: str
    session_date: str
    decision_at: str
    status: Literal["unprovable"] = FAIL_CLOSED_STATUS  # type: ignore[assignment]
    reason: str = FAIL_CLOSED_REASON
    attestations: tuple[ProviderFinalityAttestation, ...] = field(default_factory=tuple)
    source_wired: bool = FINALITY_SOURCE_WIRED
    local_reconcile_is_not_finality: bool = True

    def __post_init__(self) -> None:
        if self.attestations:
            raise ProviderFinalityNotWired(
                "the provider-finality adapter is a fail-closed stub; it cannot "
                "carry attestations"
            )
        if self.status != FAIL_CLOSED_STATUS:
            raise ProviderFinalityNotWired(
                f"status must remain {FAIL_CLOSED_STATUS!r} while no provider "
                "finality source is wired"
            )
        if self.reason != FAIL_CLOSED_REASON:
            raise ProviderFinalityNotWired(
                f"reason must remain {FAIL_CLOSED_REASON!r} while no provider "
                "finality source is wired"
            )
        if self.source_wired:
            raise ProviderFinalityNotWired(
                "source_wired cannot be asserted by a caller"
            )

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "attestation_count": len(self.attestations),
                "decision_at": self.decision_at,
                "local_reconcile_is_not_finality": (
                    self.local_reconcile_is_not_finality
                ),
                "market": self.market,
                "reason": self.reason,
                "schema_version": SCHEMA_VERSION,
                "session_date": self.session_date,
                "source_wired": self.source_wired,
                "status": self.status,
            }
        )


def fetch_provider_finality(
    *,
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
) -> FinalityFetchResult:
    """Return the fail-closed outcome. There is no success branch."""
    return FinalityFetchResult(
        market=market,
        session_date=session_date.isoformat(),
        decision_at=decision_at.isoformat(),
    )


def evaluate_provider_finality(
    *,
    attestations: Iterable[ProviderFinalityAttestation],
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
    local_reconcile_proven: bool,
    source_unavailable_reason: str | None = None,
) -> GateResult:
    """Gate: did the provider declare this session's daily bars final, pre-decision?

    ``local_reconcile_proven`` is accepted only to be *recorded*. It can never
    substitute for an attestation — that substitution is the defect this split
    exists to remove — so it is reported in the evidence and ignored by the verdict.

    🔴 Three independent layers block a fabricated attestation, because a single
    layer is one mutation away from a false pass (ROB-1172 F-INT-01):

    1. ``FINALITY_SOURCE_WIRED`` is ``False``, so *no* input can be proven. That is
       what the unwired stub means; a value-boundary caller must not be able to
       reach a pass the wired adapter cannot reach.
    2. the source must be in :data:`ADMISSIBLE_FINALITY_SOURCES`, which is empty,
       and must not be a named self-asserted/cross-domain refusal.
    3. ``raw_payload_sha256`` must be a real digest, so the attestation is bound to
       a payload rather than to free text.
    """
    candidates = tuple(attestations)
    required = {
        "required_provider_attestation": True,
        "local_reconcile_is_not_provider_finality": True,
        "local_reconcile_proven": bool(local_reconcile_proven),
        "provider_finality_source_wired": FINALITY_SOURCE_WIRED,
        "required_admissible_sources": sorted(ADMISSIBLE_FINALITY_SOURCES),
        "refused_self_asserted_sources": sorted(REFUSED_SELF_ASSERTED_SOURCES),
        "forbidden_cross_domain_sources": sorted(FORBIDDEN_CROSS_DOMAIN_SOURCES),
        "operator_attestation_is_not_provider_evidence": True,
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "session_date": session_date.isoformat(),
        "market": market,
    }
    if not is_aware(decision_at):
        return unprovable(
            "provider_finality_decision_clock_not_timezone_aware", **required
        )
    # 🔴 Layer 1. While no provider finality source is wired there is no input that
    # can prove this gate — including an attestation handed in at the value
    # boundary. The stub exists to make the claim unreachable, not merely unfetched.
    if not FINALITY_SOURCE_WIRED:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="provider_finality_source_not_wired",
            attestation_count=len(candidates),
            submitted_sources=sorted({item.source for item in candidates}),
            **required,
        )
    if source_unavailable_reason is not None:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="provider_finality_source_unavailable",
            source_unavailable_reason=source_unavailable_reason,
            **required,
        )
    matching = [
        attestation
        for attestation in candidates
        if attestation.market == market and attestation.session_date == session_date
    ]
    if not matching:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="no_attestation_for_market_session",
            **required,
        )
    if len(matching) > 1:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="conflicting_attestations",
            attestation_count=len(matching),
            **required,
        )
    attestation = matching[0]
    # 🔴 Layer 2. Admissibility is an allowlist, so an unenumerated source cannot
    # leak through; the named refusals keep the rejected fallbacks attributable.
    if (
        attestation.source in REFUSED_SELF_ASSERTED_SOURCES
        or attestation.source in FORBIDDEN_CROSS_DOMAIN_SOURCES
    ):
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="finality_source_self_asserted_or_cross_domain",
            attestation=attestation.as_evidence(),
            **required,
        )
    if attestation.source not in ADMISSIBLE_FINALITY_SOURCES:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="finality_source_not_admissible",
            attestation=attestation.as_evidence(),
            **required,
        )
    # 🔴 Layer 3. Unbound text is not a payload hash.
    if not is_sha256_hex(attestation.raw_payload_sha256):
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="raw_payload_sha256_malformed",
            attestation=attestation.as_evidence(),
            **required,
        )
    if attestation.declared_final_at > decision_at:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="declared_final_after_decision_at",
            attestation=attestation.as_evidence(),
            **required,
        )
    if attestation.retrieved_at > decision_at:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="retrieved_after_decision_at",
            attestation=attestation.as_evidence(),
            **required,
        )
    if attestation.declared_final_at > attestation.retrieved_at:
        return unprovable(
            "completed_session_provider_finality_unproven",
            defect="declared_final_after_retrieval_clock",
            attestation=attestation.as_evidence(),
            **required,
        )
    return proven(
        "completed_session_provider_finality_proven",
        attestation=attestation.as_evidence(),
        **required,
    )


def evaluate_with_stub(
    *,
    market: str,
    session_date: dt.date,
    decision_at: dt.datetime,
    local_reconcile_proven: bool,
) -> tuple[GateResult, FinalityFetchResult]:
    """Run the fail-closed stub and convert its outcome into the blocking gate."""
    fetched = fetch_provider_finality(
        market=market, session_date=session_date, decision_at=decision_at
    )
    gate = evaluate_provider_finality(
        attestations=fetched.attestations,
        market=market,
        session_date=session_date,
        decision_at=decision_at,
        local_reconcile_proven=local_reconcile_proven,
        source_unavailable_reason=fetched.reason,
    )
    return gate, fetched


__all__ = [
    "ADMISSIBLE_FINALITY_SOURCES",
    "FAIL_CLOSED_REASON",
    "FAIL_CLOSED_STATUS",
    "FINALITY_SOURCE_WIRED",
    "FORBIDDEN_CROSS_DOMAIN_SOURCES",
    "REFUSED_SELF_ASSERTED_SOURCES",
    "SCHEMA_VERSION",
    "FinalityFetchResult",
    "ProviderFinalityAttestation",
    "ProviderFinalityNotWired",
    "ProviderFinalitySource",
    "evaluate_provider_finality",
    "evaluate_with_stub",
    "fetch_provider_finality",
]
