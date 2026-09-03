"""AC4 — reference-exception adapter: a stub that *blocks*, not one that passes.

No KRX/KIS surface in this repository publishes target-session base-price
(기준가) exceptions, so nothing can prove the reference-price gate today. The
adapter exists to make that fact machine-enforced rather than a comment:

* it returns ``status="unprovable"`` with a named reason for every input;
* it returns zero records, and the result type *refuses to hold* records — a
  caller that tries to construct a passing result raises;
* there is no override: no environment variable, settings import, flag, keyword,
  or subclass hook that can turn this into a pass;
* the only way to make the gate passable is to implement
  :class:`ReferenceExceptionSource` against a real authoritative publication,
  which is a separate reviewed change.

Anything that would let this stub emit ``is_exception=False`` is a defect, not a
feature. A guard test asserts the module never constructs a record and never
reads configuration.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, runtime_checkable

from app.services.krb1_gate_result import GateResult, normalize_evidence
from app.services.krb1_reference_price_evidence import (
    AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES,
    OUTCOME_RULE,
    ReferencePriceExceptionRecord,
    evaluate_reference_price_exception_coverage,
)

SCHEMA_VERSION = "krb1.p0_3.reference_exception_adapter.v1"

#: No authoritative target-session base-price source is wired. This constant is
#: not configuration; flipping it requires implementing a real source.
SOURCE_WIRED: Final[bool] = False

FAIL_CLOSED_REASON: Final[str] = (
    "authoritative_target_session_reference_exception_source_not_wired"
)
FAIL_CLOSED_STATUS: Final[str] = "unprovable"


class ReferenceExceptionSourceNotWired(RuntimeError):
    """Raised when a caller tries to make the unwired adapter produce a pass."""


@runtime_checkable
class ReferenceExceptionSource(Protocol):
    """Contract a future authoritative source must satisfy.

    An implementation must return records whose ``effective_session`` equals the
    requested target session and whose ``published_at``/``retrieved_at`` precede
    ``decision_at``; see :mod:`app.services.krb1_reference_price_evidence`.
    """

    def fetch(
        self,
        *,
        symbols: Sequence[str],
        target_session: dt.date,
        decision_at: dt.datetime,
    ) -> tuple[ReferencePriceExceptionRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class ReferenceExceptionFetchResult:
    """Fail-closed fetch outcome. Structurally incapable of carrying a pass."""

    requested_symbols: tuple[str, ...]
    target_session: str
    decision_at: str
    status: Literal["unprovable"] = FAIL_CLOSED_STATUS  # type: ignore[assignment]
    reason: str = FAIL_CLOSED_REASON
    records: tuple[ReferencePriceExceptionRecord, ...] = field(default_factory=tuple)
    source_wired: bool = SOURCE_WIRED
    fallback_forbidden: bool = True

    def __post_init__(self) -> None:
        if self.records:
            raise ReferenceExceptionSourceNotWired(
                "the reference-exception adapter is a fail-closed stub; "
                "it cannot carry evidence records"
            )
        if self.status != FAIL_CLOSED_STATUS:
            raise ReferenceExceptionSourceNotWired(
                f"status must remain {FAIL_CLOSED_STATUS!r} while no "
                "authoritative source is wired"
            )
        if self.reason != FAIL_CLOSED_REASON:
            raise ReferenceExceptionSourceNotWired(
                f"reason must remain {FAIL_CLOSED_REASON!r} while no "
                "authoritative source is wired"
            )
        if self.source_wired:
            raise ReferenceExceptionSourceNotWired(
                "source_wired cannot be asserted by a caller"
            )

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "decision_at": self.decision_at,
                "fallback_forbidden": self.fallback_forbidden,
                "outcome_rule": OUTCOME_RULE,
                "reason": self.reason,
                "record_count": len(self.records),
                "requested_symbol_count": len(self.requested_symbols),
                "required_authoritative_sources": sorted(
                    AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES
                ),
                "schema_version": SCHEMA_VERSION,
                "source_wired": self.source_wired,
                "status": self.status,
                "target_session": self.target_session,
            }
        )


def fetch_reference_price_exceptions(
    *,
    symbols: Iterable[str],
    target_session: dt.date,
    decision_at: dt.datetime,
) -> ReferenceExceptionFetchResult:
    """Return the fail-closed outcome. There is no success branch."""
    return ReferenceExceptionFetchResult(
        requested_symbols=tuple(sorted({str(symbol) for symbol in symbols})),
        target_session=target_session.isoformat(),
        decision_at=decision_at.isoformat(),
    )


def evaluate_with_adapter(
    *,
    symbols: Iterable[str],
    target_session: dt.date,
    decision_at: dt.datetime,
) -> tuple[GateResult, ReferenceExceptionFetchResult]:
    """Run the adapter and convert its outcome into the blocking gate result."""
    fetched = fetch_reference_price_exceptions(
        symbols=symbols,
        target_session=target_session,
        decision_at=decision_at,
    )
    gate = evaluate_reference_price_exception_coverage(
        records=fetched.records,
        required_symbols=fetched.requested_symbols,
        target_session=target_session,
        decision_at=decision_at,
        source_unavailable_reason=fetched.reason,
    )
    return gate, fetched


__all__ = [
    "FAIL_CLOSED_REASON",
    "FAIL_CLOSED_STATUS",
    "SCHEMA_VERSION",
    "SOURCE_WIRED",
    "ReferenceExceptionFetchResult",
    "ReferenceExceptionSource",
    "ReferenceExceptionSourceNotWired",
    "evaluate_with_adapter",
    "fetch_reference_price_exceptions",
]
