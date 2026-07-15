"""ROB-881 — typed domain core for retrospective action transitions.

This module holds the pure domain vocabulary used by
:mod:`app.services.trade_journal.retrospective_action_transition`. It defines
no I/O and imports nothing from the ORM or HTTP layers, so the transition
service stays a single boundary that HTTP/MCP/triage callers can reuse.

Contracts enforced here (see ROB-878 design ``State Machine`` /
``Evidence envelope``):

* state graph — ``open``/``in_progress`` are active; ``done``/``obsolete``/
  ``expired`` are terminal and immutable.
* optimistic version — active transitions require the current version;
  same-terminal retry is idempotent even with a stale version.
* typed evidence — the manual ``operator_attestation`` envelope is an exact
  six-key schema that rejects extras, secrets, raw payloads, and oversized or
  deeply nested content.
* caller-owned transactions — the service only flushes; commit/rollback
  ownership stays with the caller. Exceptions are typed so ROB-882 can map
  them to 404/409/422 without touching this layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

#: All valid canonical action statuses.
ActionStatus = Literal["open", "in_progress", "done", "obsolete", "expired"]

ACTIVE_STATUSES: frozenset[str] = frozenset({"open", "in_progress"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "obsolete", "expired"})
ALL_STATUSES: frozenset[str] = ACTIVE_STATUSES | TERMINAL_STATUSES

#: Transition ``status_source`` values permitted at this layer.
ALLOWED_TRANSITION_SOURCES: frozenset[str] = frozenset(
    {"web", "mcp", "triage", "reconciler"}
)

#: Reason text upper bound (mirrors the DB CHECK constraint).
REASON_MAX_LENGTH = 2000

#: Evidence envelope canonical UTF-8 JSON size bound.
EVIDENCE_MAX_BYTES = 16 * 1024

#: Maximum nesting depth of any evidence envelope.
EVIDENCE_MAX_DEPTH = 5

#: Maximum object key length in any evidence envelope.
EVIDENCE_KEY_MAX_LENGTH = 64

#: Maximum string value length in any evidence envelope.
EVIDENCE_STRING_MAX_LENGTH = 2000

#: Key fragments (case-insensitive) that may never appear in evidence keys.
SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
)

#: The manual operator-attestation envelope — exactly these keys, in order.
_OPERATOR_ATTESTATION_KEYS: tuple[str, ...] = (
    "schema_version",
    "kind",
    "source",
    "reference",
    "observed_at",
    "summary",
)


# ---------------------------------------------------------------------------
# Transition actor (caller-attested identity, never caller-controlled string)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionActor:
    """A typed transition actor.

    The HTTP/MCP/triage boundaries build this from authenticated identity so a
    caller can never inject an arbitrary ``status_actor`` string. ``source``
    is one of :data:`ALLOWED_TRANSITION_SOURCES`; ``value`` is the derived
    persisted actor string (e.g. ``user:42`` or ``mcp:tradingcodex_execution``).
    """

    source: str
    value: str

    def __post_init__(self) -> None:
        if self.source not in ALLOWED_TRANSITION_SOURCES:
            raise ActionTransitionInvalid(
                f"invalid transition actor source: {self.source!r}"
            )
        if not self.value or not self.value.strip():
            raise ActionTransitionInvalid("transition actor value must be non-empty")

    # -- Factory constructors (preferred over hand-built values) ------------

    @classmethod
    def web(cls, user_id: int | str) -> TransitionActor:
        """Web operator actor — ``user:<stable database user id>``."""
        return cls(source="web", value=f"user:{user_id}")

    @classmethod
    def mcp(cls, profile: str) -> TransitionActor:
        """MCP server actor — ``mcp:<server profile>``."""
        if not profile or not profile.strip():
            raise ActionTransitionInvalid("mcp actor profile must be non-empty")
        return cls(source="mcp", value=f"mcp:{profile.strip()}")

    @classmethod
    def triage(cls, user_id: int | str) -> TransitionActor:
        """Triage CLI actor — the approving stable user id."""
        return cls(source="triage", value=f"user:{user_id}")

    @classmethod
    def reconciler(cls, binding: str) -> TransitionActor:
        """Automated reconciler actor — ``reconciler:<binding>``."""
        if not binding or not binding.strip():
            raise ActionTransitionInvalid("reconciler binding must be non-empty")
        return cls(source="reconciler", value=f"reconciler:{binding.strip()}")


# ---------------------------------------------------------------------------
# Typed domain exceptions (ROB-882 maps these to HTTP status codes)
# ---------------------------------------------------------------------------


class ActionTransitionError(Exception):
    """Base class for all transition-core domain errors."""


class ActionNotFoundError(ActionTransitionError):
    """The target action id does not exist (ROB-882 → 404)."""

    def __init__(self, action_id: Any) -> None:
        self.action_id = action_id
        super().__init__(f"retrospective action {action_id} not found")


class ActionControlModeError(ActionTransitionError):
    """Control mode is not canonical — writes fail closed (ROB-882 → 409)."""

    def __init__(self, mode: str | None) -> None:
        self.mode = mode
        super().__init__(
            "retrospective action control mode is not canonical; "
            f"transitions are disabled (mode={mode!r})"
        )


class ActionTransitionConflict(ActionTransitionError):
    """Optimistic-version or terminal-state conflict (ROB-882 → 409).

    Carries the current ``action_id``/``status``/``version`` so the operator
    surface can echo them in a 409 body without re-reading the row.
    """

    def __init__(
        self,
        *,
        action_id: Any,
        status: str,
        version: int,
        reason: str,
    ) -> None:
        self.action_id = action_id
        self.status = status
        self.version = version
        self.reason = reason
        super().__init__(
            f"transition conflict for action {action_id} "
            f"(status={status}, version={version}): {reason}"
        )


class ActionTransitionInvalid(ActionTransitionError):
    """Invalid transition input — bad graph step, reason, or evidence (→ 422)."""


# ---------------------------------------------------------------------------
# Transition result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionTransitionResult:
    """Evaluated outcome of a transition request.

    ``action`` is a snapshot of the evaluated action row after the transition
    (or the stored row for an idempotent result). It never includes
    ``legacy_payload`` or raw ``status_evidence``; callers that need those read
    them through the repository.
    """

    changed: bool
    idempotent: bool
    dry_run: bool
    action: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Evidence envelope validation
# ---------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    return not isinstance(value, str) or value.strip() == ""


def _max_depth(value: Any, depth: int = 0) -> int:
    if isinstance(value, Mapping):
        if not value:
            return depth + 1
        return max(_max_depth(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        if not value:
            return depth + 1
        return max(_max_depth(v, depth + 1) for v in value)
    return depth


def _scan_for_secrets(value: Any) -> str | None:
    """Return the first forbidden key fragment found, else ``None``.

    Keys are matched case-insensitively at every nesting level so a buried
    ``config.apiKey`` cannot smuggle a credential into the audit record.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                lowered = key.lower()
                for fragment in SECRET_KEY_FRAGMENTS:
                    if fragment in lowered:
                        return fragment
            found = _scan_for_secrets(child)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _scan_for_secrets(child)
            if found is not None:
                return found
    return None


def validate_operator_attestation(evidence: Any) -> dict[str, Any]:
    """Validate and canonicalize a manual ``operator_attestation`` envelope.

    Returns the canonical envelope (exactly the six keys, ordered). Raises
    :class:`ActionTransitionInvalid` on any contract violation. Raw provider
    payloads and credentials are never accepted: the envelope holds references
    and a bounded summary only.
    """
    if evidence is None:
        raise ActionTransitionInvalid("operator_attestation evidence is required")
    if not isinstance(evidence, Mapping):
        raise ActionTransitionInvalid(
            "operator_attestation evidence must be a JSON object"
        )

    keys = set(evidence.keys())
    expected = set(_OPERATOR_ATTESTATION_KEYS)
    extra = keys - expected
    if extra:
        # Report the shape, never a value — extras may be a credential.
        raise ActionTransitionInvalid(
            f"operator_attestation evidence rejects unknown keys: {sorted(extra)}"
        )
    missing = expected - keys
    if missing:
        raise ActionTransitionInvalid(
            f"operator_attestation evidence is missing required keys: {sorted(missing)}"
        )

    schema_version = evidence["schema_version"]
    if schema_version != 1:
        raise ActionTransitionInvalid(
            f"unsupported operator_attestation schema_version: {schema_version!r}"
        )

    kind = evidence["kind"]
    if kind != "operator_attestation":
        raise ActionTransitionInvalid(
            f"operator_attestation evidence kind must be 'operator_attestation', "
            f"got {kind!r}"
        )

    source = evidence["source"]
    reference = evidence["reference"]
    summary = evidence["summary"]
    if _is_blank(source):
        raise ActionTransitionInvalid("operator_attestation source must be non-blank")
    if _is_blank(reference):
        raise ActionTransitionInvalid(
            "operator_attestation reference must be non-blank"
        )
    if _is_blank(summary):
        raise ActionTransitionInvalid("operator_attestation summary must be non-blank")

    observed_at_raw = evidence["observed_at"]
    if not isinstance(observed_at_raw, str):
        raise ActionTransitionInvalid(
            "operator_attestation observed_at must be an RFC3339 string"
        )
    observed_at_str = observed_at_raw.strip()
    if not observed_at_str:
        raise ActionTransitionInvalid(
            "operator_attestation observed_at must be non-blank"
        )
    try:
        observed_at = datetime.fromisoformat(observed_at_str)
    except ValueError as exc:
        raise ActionTransitionInvalid(
            "operator_attestation observed_at must be RFC3339"
        ) from exc
    # Reject naive timestamps — an offset (or ``Z``) is mandatory.
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ActionTransitionInvalid(
            "operator_attestation observed_at must include a timezone offset"
        )

    canonical: dict[str, Any] = {
        "schema_version": 1,
        "kind": "operator_attestation",
        "source": source.strip(),
        "reference": reference.strip(),
        "observed_at": observed_at_str,
        "summary": summary.strip(),
    }

    # Structural bounds (depth, key length, string length, secret scan).
    depth = _max_depth(canonical)
    if depth > EVIDENCE_MAX_DEPTH:
        raise ActionTransitionInvalid(
            f"operator_attestation evidence nesting depth {depth} exceeds limit "
            f"{EVIDENCE_MAX_DEPTH}"
        )
    _enforce_string_bounds(canonical)
    secret = _scan_for_secrets(canonical)
    if secret is not None:
        raise ActionTransitionInvalid(
            "operator_attestation evidence rejects secret-like key "
            f"(fragment={secret!r})"
        )

    encoded = json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    if len(encoded) > EVIDENCE_MAX_BYTES:
        raise ActionTransitionInvalid(
            f"operator_attestation evidence exceeds {EVIDENCE_MAX_BYTES} bytes"
        )

    return canonical


def _enforce_string_bounds(value: Any) -> None:
    """Recursively enforce key/string length limits, raising on violation."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and len(key) > EVIDENCE_KEY_MAX_LENGTH:
                raise ActionTransitionInvalid(
                    f"operator_attestation evidence key exceeds "
                    f"{EVIDENCE_KEY_MAX_LENGTH} characters"
                )
            _enforce_string_bounds(child)
    elif isinstance(value, str):
        if len(value) > EVIDENCE_STRING_MAX_LENGTH:
            raise ActionTransitionInvalid(
                f"operator_attestation evidence string exceeds "
                f"{EVIDENCE_STRING_MAX_LENGTH} characters"
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _enforce_string_bounds(child)
