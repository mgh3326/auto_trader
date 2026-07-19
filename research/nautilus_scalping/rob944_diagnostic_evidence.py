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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from research_contracts.canonical_hash import canonical_sha256
from research_contracts.diagnostic_evidence_policy import (
    MAX_DISTINCT_SIGNATURES as MAX_DISTINCT_SIGNATURES,
)

__all__ = [
    "MAX_DISTINCT_SIGNATURES",
    "ChildFailureEvidence",
    "DiagnosticOverflowMetadata",
    "accumulate_diagnostic_evidence",
    "capture_child_failure_evidence",
]

Transport = Literal["in_process"]
Stage = Literal["generator", "funding_gate", "engine"]

_MAX_MESSAGE_CHARS = 500
_MAX_TRACEBACK_CHARS = 4000
_TRUNCATION_MARKER = "...<truncated>..."
# Q3 (Fable-approved orch-fable-answer-rob970-r1-20260719.md): structural
# redaction removes KNOWN patterns first; if the result still LOOKS unsafe
# (a residual fail-closed check), the ENTIRE message is replaced with this
# fixed sentinel rather than left partially exposed. Never novel-encoding
# fail-OPEN.
_SENTINEL_UNSAFE_MESSAGE = "<redacted-unsafe-exception-message>"

# Absolute filesystem path inside a `File "..."` traceback frame line -- keep
# only the bare filename; the worktree/host path never survives sanitization.
_FILE_FRAME_RE = re.compile(r'File "[^"]*[\\/]([^\\/"]+)"')
# The (already directory-stripped, by the time this runs) bare filename
# inside a `File "..."` frame line -- R2 stop-gate audit: a hostile
# FILENAME itself (e.g. a synthetic module named after a secret) is not
# automatically safe merely for living inside a `File "..."` clause, so it
# must still be checked/redacted -- while `, line N, in func_name` (matched
# by this same frame line, outside the quoted span) always survives
# verbatim, since frame identity must remain useful.
_FILE_FRAME_BARE_RE = re.compile(r'File "([^"]+)"')
# Any other absolute-looking POSIX path fragment that isn't inside a `File
# "..."` frame (defense in depth) -- collapse to its bare last component.
_BARE_ABS_PATH_RE = re.compile(r"(?<![\w./])/(?:[\w.\-]+/)+([\w.\-]+)")

# Unquoted `key=value`/`key: value` secret-shaped assignment (mixed-case key
# names via `(?i)`).
_SECRET_KV_RE = re.compile(
    r"(?i)\b(\w*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|dsn|"
    r"credential)\w*)\b(\s*[:=]\s*)(\S+)"
)
# Quoted key/value secret-shaped assignment -- dict-repr/env-dump style, e.g.
# `'OPENAI_API_KEY': 'sk-...'` or `"secret_token": "..."` (R1 Critical-1's
# exact adversarial repro: the OLD unquoted-only regex missed this shape).
_SECRET_QUOTED_KV_RE = re.compile(
    r"(?i)(['\"])(\w*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|"
    r"dsn|credential)\w*)\1(\s*[:=]\s*)(['\"])[^'\"]*\4"
)
_DSN_URL_RE = re.compile(r"(?i)\b\w+://[^\s\"']*:[^\s\"'@]*@[^\s\"']+")
_BEARER_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-.]{10,}\b")
# A `{...}` span containing at least one quoted `'key': 'value'`-shaped pair
# -- a dict/env-dump repr, redacted wholesale regardless of key names (fail-
# closed: we don't know what else the dict carries).
_DICT_LITERAL_RE = re.compile(r"\{(?:[^{}]|\n){0,2000}?\}")
_DICT_LITERAL_HAS_KV_RE = re.compile(r"['\"]\s*:\s*['\"]")
# A `ClassName(field=value, field=value, ...)`-shaped repr (2+ kwargs) --
# generic raw-record redaction (Bar1m/TradeRecord/NoTradeRecord/SignalEvent/
# etc.) without hardcoding any specific class name.
_DATACLASS_REPR_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*\((?:[a-zA-Z_][a-zA-Z0-9_]*=[^(){}]*?,\s*){1,}"
    r"[a-zA-Z_][a-zA-Z0-9_]*=[^(){}]*?\)"
)

# Residual fail-closed detectors (Q3): run AFTER structural redaction. Each
# pattern's trigger requires an actual non-placeholder value immediately
# following, so an ALREADY-safely-redacted `key: <redacted>` never
# re-triggers a full-message wipe.
_RESIDUAL_KV_SECRET_RE = re.compile(
    r"(?i)\b\w*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|dsn|"
    r"credential)\w*\b['\"]?\s*[:=]\s*(?!<redacted)['\"]?\S"
)
_RESIDUAL_DICT_LITERAL_RE = re.compile(
    r"\{(?:[^{}]|\n){0,2000}?['\"]\s*:\s*['\"][^{}]*?\}"
)
_RESIDUAL_DATACLASS_REPR_RE = _DATACLASS_REPR_RE
# A domain-agnostic backstop for genuinely novel/unrecognized encodings: a
# SHOUTY (all-caps, 4+ letters) secret-vocabulary word. Ordinary messages in
# this codebase's vocabulary never shout these words in caps -- a hit here
# means the structural patterns above didn't recognize the surrounding
# shape, not that the content is actually safe.
_RESIDUAL_SHOUTY_SECRET_RE = re.compile(
    r"\b(?:SECRET|CREDENTIAL|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|PASSWORD|"
    r"API[_-]?KEY|ACCESS[_-]?KEY)[A-Z_-]*\b"
)


def _redact_dsn_urls(text: str) -> str:
    return _DSN_URL_RE.sub("<redacted-dsn>", text)


def _redact_secret_kv(text: str) -> str:
    text = _SECRET_QUOTED_KV_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}<redacted>", text
    )
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


def _redact_bearer_jwt(text: str) -> str:
    return _BEARER_JWT_RE.sub("<redacted-token>", text)


def _redact_dict_literals(text: str) -> str:
    def _replace(m: re.Match) -> str:
        chunk = m.group(0)
        if _DICT_LITERAL_HAS_KV_RE.search(chunk):
            return "<redacted-dict>"
        return chunk

    return _DICT_LITERAL_RE.sub(_replace, text)


def _redact_dataclass_reprs(text: str) -> str:
    return _DATACLASS_REPR_RE.sub("<redacted-record>", text)


def _redact_paths(text: str) -> str:
    text = _FILE_FRAME_RE.sub(lambda m: f'File "{m.group(1)}"', text)
    return _BARE_ABS_PATH_RE.sub(lambda m: m.group(1), text)


def _sanitize(text: str) -> str:
    """Order matters: redact DSNs/secrets/tokens/dict-literals/records BEFORE
    paths, so a secret embedded inside a path-shaped string (e.g. a DSN with
    a leading `/`) is still caught by the more specific patterns first."""
    text = _redact_dsn_urls(text)
    text = _redact_secret_kv(text)
    text = _redact_bearer_jwt(text)
    text = _redact_dict_literals(text)
    text = _redact_dataclass_reprs(text)
    return _redact_paths(text)


def _looks_unsafe_residual(text: str) -> bool:
    """Q3 fail-closed residual check: after structural redaction, does the
    text STILL look like it carries a secret/env-dump/raw-record shape? A
    real hit here means structural redaction missed a genuinely
    novel/unrecognized encoding -- never fail open."""
    return bool(
        _RESIDUAL_KV_SECRET_RE.search(text)
        or _RESIDUAL_DICT_LITERAL_RE.search(text)
        or _RESIDUAL_DATACLASS_REPR_RE.search(text)
        or _RESIDUAL_SHOUTY_SECRET_RE.search(text)
    )


def _finalize_message(raw_message: str) -> str:
    """Structural redaction, then the fail-closed residual check -- on a
    residual hit, the message is replaced WHOLESALE (never left partially
    exposed)."""
    sanitized = _sanitize(raw_message)
    if _looks_unsafe_residual(sanitized):
        return _SENTINEL_UNSAFE_MESSAGE
    return sanitized


_FRAME_LIKE_LINE_RE = re.compile(
    r'^(\s*File "|Traceback \(most recent call last\)|\s*\^|\s*~|\s*$|'
    r"The above exception was the direct cause|"
    r"During handling of the above exception|\[Previous line repeated)"
)

# R2 audit fix: any SINGLE traceback line (almost always a source-context
# line under a frame header) is bounded BEFORE frame-aware reconstruction,
# so one pathological line (e.g. an oversized inline comment) can never by
# itself consume the whole traceback budget and push the innermost failing
# frame's OWN header line out of the retained window via blind character-
# count slicing.
_MAX_LINE_CHARS = 300


def _bound_line(line: str) -> str:
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[: _MAX_LINE_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _bound_lines(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    bounded = [_bound_line(line) for line in lines]
    return "\n".join(bounded), bounded != lines


_FILE_FRAME_LINE_START_RE = re.compile(r'^\s*File "')


def _neutralize_frame_line_filename(line: str) -> str:
    """R2 stop-gate audit: a ``File "..."`` frame HEADER line's
    ``, line N, in func_name`` suffix must survive verbatim (frame identity
    -- this is the whole point of exempting frame lines from the blanket
    residual sweep below), but the FILENAME itself living inside the quoted
    span is NOT automatically safe merely for being there -- a synthetic or
    hostile module name (e.g. shaped like a shouty secret label) is checked
    and, if unsafe, ONLY the filename portion is replaced with the
    sentinel; the rest of the line (line number, function name) is
    untouched."""

    def _replace(m: re.Match) -> str:
        filename = m.group(1)
        if _looks_unsafe_residual(filename):
            filename = _SENTINEL_UNSAFE_MESSAGE
        return f'File "{filename}"'

    return _FILE_FRAME_BARE_RE.sub(_replace, line, count=1)


def _neutralize_unsafe_lines(text: str) -> str:
    """R2 audit fix: a frame HEADER line (``File "...", line N, in name``)
    and pure traceback furniture are always preserved verbatim (frame
    identity must survive) -- EXCEPT the filename inside a ``File "..."``
    clause, which gets its own targeted check (see
    ``_neutralize_frame_line_filename``). Every OTHER line -- including an
    indented SOURCE-CONTEXT line -- is NOT automatically safe:
    ``capture_locals=False`` only guarantees no runtime local VALUES
    appear, but the literal source TEXT (e.g. a careless comment) can
    itself carry secret-shaped content. Each such line is checked
    independently and replaced with the sentinel if it still looks unsafe,
    never blanket-trusted merely for being indented."""
    out_lines = []
    for line in text.split("\n"):
        if _FILE_FRAME_LINE_START_RE.match(line):
            out_lines.append(_neutralize_frame_line_filename(line))
        elif _FRAME_LIKE_LINE_RE.match(line):
            out_lines.append(line)
        elif _looks_unsafe_residual(line):
            out_lines.append(_SENTINEL_UNSAFE_MESSAGE)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _finalize_traceback(
    raw_traceback: str, raw_message: str, safe_message: str
) -> tuple[str, bool]:
    """The raw traceback's own trailing exception line duplicates
    ``str(exc)`` -- replace that occurrence with the ALREADY-finalized safe
    message first (so a message that needed full sentinel replacement
    doesn't leave its raw text sitting in the traceback too). Structural
    redaction runs next, then the per-line residual-fail-closed sweep runs
    on the FULL, UNTRUNCATED lines (R2 stop-gate audit: running this sweep
    AFTER length-bounding let a shouty-secret word straddling the per-line
    cut survive as an undetectable partial fragment -- neither the
    truncated remainder nor the discarded tail alone matched the residual
    pattern, even though the untruncated line would have) -- length
    bounding runs LAST, only on content already proven safe (either
    genuinely safe original text, or the fixed-length sentinel)."""
    text = raw_traceback
    if raw_message and raw_message in text:
        text = text.replace(raw_message, safe_message)
    text = _sanitize(text)
    text = _neutralize_unsafe_lines(text)
    text, line_truncated = _bound_lines(text)
    return text, line_truncated


def _bound_head(text: str, limit: int) -> tuple[str, bool]:
    """Head-bound with an EXPLICIT, visible truncation marker -- the total
    returned length never exceeds ``limit``."""
    if len(text) <= limit:
        return text, False
    keep = max(limit - len(_TRUNCATION_MARKER), 0)
    return text[:keep] + _TRUNCATION_MARKER, True


def _bound_tail_by_line(text: str, limit: int) -> tuple[str, bool]:
    """Frame-aware tail-bound: accumulate WHOLE lines from the end (never a
    mid-line character cut, which could otherwise slice straight through a
    frame header line if a PRECEDING line happens to be huge) until the
    budget is exhausted. The innermost (last) frame header and the final
    exception line are the most recently accumulated and therefore always
    survive; only whole EARLIER lines are dropped."""
    if len(text) <= limit:
        return text, False
    lines = text.split("\n")
    budget = max(limit - len(_TRUNCATION_MARKER), 0)
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        addition = len(line) + 1  # +1 for the newline that joins it back in
        if kept and total + addition > budget:
            break
        kept.append(line)
        total += addition
    kept.reverse()
    return _TRUNCATION_MARKER + "\n".join(kept), True


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

    safe_message = _finalize_message(raw_message)
    safe_traceback, line_truncated = _finalize_traceback(
        raw_traceback, raw_message, safe_message
    )

    message, message_truncated = _bound_head(safe_message, _MAX_MESSAGE_CHARS)
    traceback_text, tail_truncated = _bound_tail_by_line(
        safe_traceback, _MAX_TRACEBACK_CHARS
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
        truncated=message_truncated or line_truncated or tail_truncated,
    )


@dataclass(frozen=True)
class DiagnosticOverflowMetadata:
    """ROB-970 R1 (Q1=A, cap=32, Fable-approved
    ``orch-fable-answer-rob970-r1-20260719.md``): closed-shape,
    diagnostics-only overflow accounting for anything beyond the first 32
    DISTINCT signatures (in canonical/first-seen execution order).
    Deliberately carries NO hash/identity role -- excluded from every
    semantic hash/fingerprint/verdict, exactly like ``ChildFailureEvidence``
    itself. Exactly these three fields, nothing more."""

    truncated: bool
    omitted_distinct_signatures: int
    omitted_occurrences: int


def accumulate_diagnostic_evidence(
    events: Sequence[ChildFailureEvidence],
) -> tuple[tuple[ChildFailureEvidence, ...], DiagnosticOverflowMetadata]:
    """Fold ``events`` (in the order captured -- canonical execution order)
    into a BOUNDED tuple of at most ``MAX_DISTINCT_SIGNATURES`` (=32,
    Fable-approved, R2-confirmed the ONLY production cap policy -- no
    caller-selectable override) distinct signatures, plus honest overflow
    metadata for anything beyond the cap.

    A recurrence of an already-RETAINED signature only bumps its
    ``occurrence_count`` -- the FIRST-seen deterministic context is
    preserved, never overwritten. A recurrence of an already-OMITTED
    signature (the cap was already full when it first appeared) only bumps
    ``omitted_occurrences`` -- it does NOT count again toward
    ``omitted_distinct_signatures``. A genuinely NEW signature arriving
    after the cap is full is never silently dropped without a trace: it
    bumps both ``omitted_distinct_signatures`` and ``omitted_occurrences``
    and sets ``truncated=True``.
    """
    evidence: tuple[ChildFailureEvidence, ...] = ()
    omitted_signatures: set[str] = set()
    truncated = False
    omitted_distinct_signatures = 0
    omitted_occurrences = 0

    for new in events:
        matched_idx = next(
            (i for i, cur in enumerate(evidence) if cur.signature == new.signature),
            None,
        )
        if matched_idx is not None:
            current = evidence[matched_idx]
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
            evidence = evidence[:matched_idx] + (bumped,) + evidence[matched_idx + 1 :]
            continue
        if new.signature in omitted_signatures:
            omitted_occurrences += 1
            truncated = True
            continue
        if len(evidence) < MAX_DISTINCT_SIGNATURES:
            evidence = evidence + (new,)
            continue
        omitted_signatures.add(new.signature)
        omitted_distinct_signatures += 1
        omitted_occurrences += 1
        truncated = True

    overflow = DiagnosticOverflowMetadata(
        truncated=truncated,
        omitted_distinct_signatures=omitted_distinct_signatures,
        omitted_occurrences=omitted_occurrences,
    )
    return evidence, overflow
