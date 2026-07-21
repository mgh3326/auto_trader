"""ROB-981 (ROB-974 R2 H6-A) CP6 -- ROB-970 diagnostic carrier and replay
observer isolation.

Reuses the existing ROB-970 sanitizer contract UNCHANGED
(``rob944_diagnostic_evidence.capture_child_failure_evidence``/
``accumulate_diagnostic_evidence``/``ChildFailureEvidence``/
``DiagnosticOverflowMetadata``/``MAX_DISTINCT_SIGNATURES`` -- cap500
message/cap4000 traceback/32 distinct signatures, ``capture_locals=False``,
structural + residual fail-closed redaction) -- this module never
reimplements sanitization, it only adds the ROB-981-specific CARRIER type
and the pure canonical-byte comparison authority for replay-divergence
detection.

Diagnostic fields are excluded from every semantic hash by CONSTRUCTION:
``DiagnosticCarrier`` is never an input to any ``rob974_h6a_evidence``/
``rob974_h6a_accounting``/``rob974_h6a_payload`` hash function, and
``rob974_h6a_bridge.H6AAttemptBatchItem.fingerprint()`` deliberately never
reads its ``diagnostic_evidence``/``diagnostic_overflow`` fields either.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
shared ``research_contracts.canonical_hash`` authority and the sibling
``rob944_diagnostic_evidence`` module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rob944_diagnostic_evidence import (
    MAX_DISTINCT_SIGNATURES as MAX_DISTINCT_SIGNATURES,
)
from rob944_diagnostic_evidence import (
    ChildFailureEvidence as ChildFailureEvidence,
)
from rob944_diagnostic_evidence import (
    DiagnosticOverflowMetadata as DiagnosticOverflowMetadata,
)
from rob944_diagnostic_evidence import (
    accumulate_diagnostic_evidence as accumulate_diagnostic_evidence,
)
from rob944_diagnostic_evidence import (
    capture_child_failure_evidence as capture_child_failure_evidence,
)

from research_contracts.canonical_hash import canonical_json

__all__ = [
    "MAX_DISTINCT_SIGNATURES",
    "MISSING",
    "ChildFailureEvidence",
    "DiagnosticCarrier",
    "DiagnosticCarrierError",
    "DiagnosticOverflowMetadata",
    "DiagnosticReplayObservation",
    "accumulate_diagnostic_evidence",
    "build_replay_observation",
    "canonical_diagnostic_bytes",
    "capture_child_failure_evidence",
    "diverges",
]

# A sentinel distinct from any real value (including ``None``) -- lets a
# caller (CP5's persistence boundary) tell a GENUINELY ABSENT stored
# diagnostic (a pre-ROB-981 row) apart from one that is PRESENT with an
# explicit (possibly malformed) value. ``value or MISSING`` / ``.get(key) or
# default`` are exactly the bug class this exists to prevent -- always
# compare identity (``is MISSING``), never truthiness.
MISSING = object()


class DiagnosticCarrierError(ValueError):
    """A diagnostic carrier failed an exact-type/cap check -- a forged/
    subclass/``model_construct``-equivalent leaf can never pass."""


@dataclass(frozen=True)
class DiagnosticCarrier:
    """Bounded (evidence, overflow) pair -- the one persistence-boundary
    unit CP5 attaches to an attempt. Exact-type gated: an outer/leaf
    subclass, a non-tuple evidence collection, or a non-authoritative
    overflow carrier are all refused at construction, never merely
    ``isinstance``-checked (which a ``__class__``-forged or hand-built
    subclass instance would still pass)."""

    evidence: tuple[ChildFailureEvidence, ...]
    overflow: DiagnosticOverflowMetadata

    def __post_init__(self) -> None:
        if type(self.evidence) is not tuple:
            raise DiagnosticCarrierError("evidence must be an exact tuple")
        if len(self.evidence) > MAX_DISTINCT_SIGNATURES:
            raise DiagnosticCarrierError(
                f"evidence must have at most {MAX_DISTINCT_SIGNATURES} entries"
            )
        if any(type(item) is not ChildFailureEvidence for item in self.evidence):
            raise DiagnosticCarrierError(
                "every evidence entry must be an exact ChildFailureEvidence instance"
            )
        if type(self.overflow) is not DiagnosticOverflowMetadata:
            raise DiagnosticCarrierError(
                "overflow must be an exact DiagnosticOverflowMetadata instance"
            )
        signatures = [item.signature for item in self.evidence]
        if len(set(signatures)) != len(signatures):
            raise DiagnosticCarrierError(
                "evidence must not contain a duplicate signature"
            )


def _evidence_payload(evidence: tuple[ChildFailureEvidence, ...]) -> list[dict]:
    return [
        {
            "transport": item.transport,
            "stage": item.stage,
            "exception_type": item.exception_type,
            "message": item.message,
            "traceback_text": item.traceback_text,
            "stderr": item.stderr,
            "strategy": item.strategy,
            "config_id": item.config_id,
            "symbol": item.symbol,
            "fold_id": item.fold_id,
            "scenario_name": item.scenario_name,
            "signature": item.signature,
            "occurrence_count": item.occurrence_count,
            "truncated": item.truncated,
        }
        for item in evidence
    ]


def _overflow_payload(overflow: DiagnosticOverflowMetadata) -> dict:
    return {
        "truncated": overflow.truncated,
        "omitted_distinct_signatures": overflow.omitted_distinct_signatures,
        "omitted_occurrences": overflow.omitted_occurrences,
    }


def canonical_diagnostic_bytes(carrier: DiagnosticCarrier) -> bytes:
    """The SOLE comparison authority for replay-divergence detection --
    canonical bytes via the shared canonical-JSON authority, reconstructed
    fresh every time (mirrors ``research_campaign_bridge.
    _canonical_diagnostic_bytes``'s discipline: never ad-hoc ``json.dumps``,
    never a persisted fingerprint string read back as ground truth)."""
    return canonical_json(
        {
            "diagnostic_evidence": _evidence_payload(carrier.evidence),
            "diagnostic_overflow": _overflow_payload(carrier.overflow),
        }
    ).encode("utf-8")


def diverges(stored_bytes: bytes, incoming_bytes: bytes) -> bool:
    """Pure byte comparison -- byte-identical (including any legacy/absent-
    field normalization the CALLER already applied before building
    ``stored_bytes``) is a no-op; any divergence is loudly observable."""
    return stored_bytes != incoming_bytes


@dataclass(frozen=True)
class DiagnosticReplayObservation:
    """Digest-only, bounded, sanitized observation payload -- never the raw
    idempotency_key, never raw stored/incoming diagnostic content. This is
    the ONLY shape an observer may emit."""

    idempotency_key_digest: str
    stored_diagnostic_digest: str
    incoming_diagnostic_digest: str
    stored_distinct_signature_count: int
    new_distinct_signature_count: int


def build_replay_observation(
    *,
    idempotency_key: str,
    stored_bytes: bytes,
    incoming_bytes: bytes,
    stored_distinct_signature_count: int,
    new_distinct_signature_count: int,
) -> DiagnosticReplayObservation:
    """Pure construction of the bounded/sanitized observation record -- the
    caller (CP5's persistence boundary) is responsible for actually
    emitting it (e.g. to stderr) inside its own non-fail-stop try/except;
    this function performs no I/O of its own."""
    return DiagnosticReplayObservation(
        idempotency_key_digest=hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest(),
        stored_diagnostic_digest=hashlib.sha256(stored_bytes).hexdigest(),
        incoming_diagnostic_digest=hashlib.sha256(incoming_bytes).hexdigest(),
        stored_distinct_signature_count=stored_distinct_signature_count,
        new_distinct_signature_count=new_distinct_signature_count,
    )
