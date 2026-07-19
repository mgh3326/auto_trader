"""ROB-970 (Q2/Q3, Fable-approved ``orch-fable-answer-rob970-20260719.md``) --
typed, sanitized, bounded child-failure diagnostic evidence, captured at the
FIRST catch (generator/funding-gate/engine) and carried SEPARATE from H4/H5/H6
semantic identity (fixed reason code, scenario/fold artifact hash,
``fold_evidence_hash``, ``run_identity``, H6's ``terminal_evidence_fingerprint``)
into H6's persisted ``raw_payload`` diagnostic area.

Never reconstructed later from ``str(exc)`` -- capture happens at the ORIGINAL
catch site via ``traceback.TracebackException.from_exception(exc,
capture_locals=False)`` (Q2 condition: no locals, ever).

Q3 (Fable-approved): the only transport this module supports today is
``"in_process"`` -- every current child (signal-generator/funding-gate/engine
callback) runs in the SAME OS process, never a subprocess, so there is no real
child stderr stream. ``stderr`` is therefore always ``None`` for this
transport; a future subprocess transport would carry its own captured stderr
bytes/text, but this module never fabricates one by duplicating the traceback
into it -- that would misrepresent an observed fact.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from typing import Literal

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "ChildFailureEvidence",
    "capture_child_failure_evidence",
    "merge_child_failure_evidence",
]

Transport = Literal["in_process"]
Stage = Literal["generator", "funding_gate", "engine"]

_MAX_MESSAGE_CHARS = 500
_MAX_TRACEBACK_CHARS = 4000
_TRUNCATION_MARKER = "...<truncated>..."

# Absolute filesystem path inside a `File "..."` traceback frame line -- keep
# only the bare filename; the worktree/host path never survives sanitization.
_FILE_FRAME_RE = re.compile(r'File "[^"]*[\\/]([^\\/"]+)"')
# Any other absolute-looking POSIX path fragment that isn't inside a `File
# "..."` frame (defense in depth) -- collapse to its bare last component.
_BARE_ABS_PATH_RE = re.compile(r"(?<![\w./])/(?:[\w.\-]+/)+([\w.\-]+)")

_SECRET_KV_RE = re.compile(
    r"(?i)\b(secret|token|password|passwd|api[_-]?key|access[_-]?key|dsn)\b"
    r"(\s*[:=]\s*)(\S+)"
)
_DSN_URL_RE = re.compile(r"(?i)\b\w+://[^\s\"']*:[^\s\"'@]*@[^\s\"']+")
_BEARER_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-.]{10,}\b")


def _redact_dsn_urls(text: str) -> str:
    return _DSN_URL_RE.sub("<redacted-dsn>", text)


def _redact_secret_kv(text: str) -> str:
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


def _redact_bearer_jwt(text: str) -> str:
    return _BEARER_JWT_RE.sub("<redacted-token>", text)


def _redact_paths(text: str) -> str:
    text = _FILE_FRAME_RE.sub(lambda m: f'File "{m.group(1)}"', text)
    return _BARE_ABS_PATH_RE.sub(lambda m: m.group(1), text)


def _sanitize(text: str) -> str:
    """Order matters: redact DSNs/secrets/tokens BEFORE paths, so a secret
    embedded inside a path-shaped string (e.g. a DSN with a leading `/`) is
    still caught by the more specific patterns first."""
    text = _redact_dsn_urls(text)
    text = _redact_secret_kv(text)
    text = _redact_bearer_jwt(text)
    return _redact_paths(text)


def _bound_head(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _bound_tail(text: str, limit: int) -> tuple[str, bool]:
    """Keep the END of the text (innermost/failing frames + the final
    exception line), never the start -- a truncated Python traceback's most
    diagnostic content is always at the bottom."""
    if len(text) <= limit:
        return text, False
    return _TRUNCATION_MARKER + text[-limit:], True


@dataclass(frozen=True)
class ChildFailureEvidence:
    """One deduped, sanitized, bounded child-failure diagnostic record.
    Deliberately carries NO hash/identity role of its own -- it is additive
    persistence-only evidence, never an input to any fixed reason code,
    artifact hash, ``fold_evidence_hash``, ``run_identity``, or H6 semantic
    fingerprint (Fable Q2 condition)."""

    transport: Transport
    stage: Stage
    exception_type: str
    message: str
    traceback_text: str
    stderr: str | None
    strategy: str
    config_id: str
    symbol: str | None
    fold_id: str | None
    scenario_name: str | None
    signature: str
    occurrence_count: int
    truncated: bool


def _compute_signature(*, exception_type: str, message: str, stage: str) -> str:
    """Deterministic dedupe key -- deliberately EXCLUDES symbol/fold_id/
    scenario_name (those are expected to vary across independent recurrences
    of the SAME underlying failure) so repeated hits of one root cause across
    many folds/symbols collapse into one evidence entry with an incrementing
    ``occurrence_count``, never N near-duplicate rows. No wall-clock/UUID."""
    return canonical_sha256(
        {
            "exception_type": exception_type,
            "message": message,
            "stage": stage,
        }
    )


def capture_child_failure_evidence(
    exc: BaseException,
    *,
    transport: Transport,
    stage: Stage,
    strategy: str,
    config_id: str,
    symbol: str | None = None,
    fold_id: str | None = None,
    scenario_name: str | None = None,
) -> ChildFailureEvidence:
    """Capture ONE child failure at its original catch site. ``exc`` must be
    the live exception object (accessed from an ``except ... as exc:`` block)
    -- this function reads its traceback directly, never from a
    re-stringified/re-raised copy."""
    if transport != "in_process":
        raise ValueError(
            f"unsupported transport {transport!r} -- only 'in_process' has a "
            "real capture path today; refusing to fabricate evidence for an "
            "unimplemented transport"
        )

    te = traceback.TracebackException.from_exception(exc, capture_locals=False)
    exception_type = type(exc).__name__
    raw_message = str(exc)
    raw_traceback = "".join(te.format())

    sanitized_message = _sanitize(raw_message)
    sanitized_traceback = _sanitize(raw_traceback)

    message, message_truncated = _bound_head(sanitized_message, _MAX_MESSAGE_CHARS)
    traceback_text, traceback_truncated = _bound_tail(
        sanitized_traceback, _MAX_TRACEBACK_CHARS
    )

    signature = _compute_signature(
        exception_type=exception_type, message=message, stage=stage
    )

    return ChildFailureEvidence(
        transport=transport,
        stage=stage,
        exception_type=exception_type,
        message=message,
        traceback_text=traceback_text,
        stderr=None,  # Q3: no real child stderr stream exists for in_process
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        fold_id=fold_id,
        scenario_name=scenario_name,
        signature=signature,
        occurrence_count=1,
        truncated=message_truncated or traceback_truncated,
    )


def merge_child_failure_evidence(
    existing: tuple[ChildFailureEvidence, ...],
    new: ChildFailureEvidence,
) -> tuple[ChildFailureEvidence, ...]:
    """Fold ``new`` into ``existing`` by stable signature. A recurrence of an
    already-seen signature only bumps ``occurrence_count`` -- the FIRST-seen
    deterministic context (symbol/fold_id/scenario_name/representative
    message+traceback) is preserved, never overwritten by a later duplicate.
    A new signature is appended as its own entry."""
    for idx, current in enumerate(existing):
        if current.signature == new.signature:
            bumped = ChildFailureEvidence(
                transport=current.transport,
                stage=current.stage,
                exception_type=current.exception_type,
                message=current.message,
                traceback_text=current.traceback_text,
                stderr=current.stderr,
                strategy=current.strategy,
                config_id=current.config_id,
                symbol=current.symbol,
                fold_id=current.fold_id,
                scenario_name=current.scenario_name,
                signature=current.signature,
                occurrence_count=current.occurrence_count + 1,
                truncated=current.truncated,
            )
            return existing[:idx] + (bumped,) + existing[idx + 1 :]
    return existing + (new,)
