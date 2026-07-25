"""ROB-945 (H5, Task 1E) -- pure H6-summary normalizer/validator contract.

Task 1 final re-review (strategy-audit-rob945-task1-final-20260718-050428.md,
I2/I3) found that ``rob945_accounting_seal`` hashed/cross-bound a caller/
producer-supplied ``rob944_walkforward.ConfigAttemptEvidenceSummary`` without
ever applying the real H6-build boundary's own nested normalization/
validation -- a structurally malformed summary (e.g. one config missing from
one fold's candidate roster, leaving 7 fold-selection rows instead of the
canonical 8) sealed successfully because only the OUTER 12-config roster was
checked, never the nested ``fold_selection_trace``/``scenario_summaries``
shape.

``normalize_and_validate_h6_summary`` is a pure H5 mirror of the real H6-build
boundary's own normalizer (``run_rob944_campaign.
_normalize_config_attempt_evidence_summary`` +
``_normalized_summary_to_attempt_evidence``'s pre-hash validation,
2026-07-17 captain corrections) -- it does NOT import that CLI module or
``app.*`` (H4/H6 bytes cannot be changed to extract a shared public helper in
this issue); it is a hand-verified literal duplicate of that boundary's
invariants, pinned by a test-only parity oracle
(``tests/test_rob945_h6_summary_contract.py``, which imports the real
``run_rob944_campaign`` helper ONLY as a comparison oracle, never from H5
production code).

One-pass normalization discipline (mirrors the real boundary's own TOCTOU
closure): every caller-owned field is read EXACTLY ONCE, type-checked with
``type(x) is T`` (never ``isinstance``, which would accept a subclass whose
overridden dunder methods could return different values on repeated reads),
and copied into a BRAND-NEW ``ConfigAttemptEvidenceSummary``/
``ScenarioEvidenceSummary``/``FoldSelectionEvidenceSummary``. Hashing/
cross-binding downstream must consume ONLY the returned snapshot, never the
caller's original object again.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
existing research-local H4 sibling modules, deterministic given its input.
"""

from __future__ import annotations

import re
from typing import Any

import rob941_frozen_scope as frozen
import rob944_folds as foldmod
from rob944_diagnostic_evidence import (
    MAX_DISTINCT_SIGNATURES,
    ChildFailureEvidence,
    DiagnosticOverflowMetadata,
)
from rob944_gap_funding import (
    REASON_DATA_GAP_IN_POSITION,
    REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
    REASON_FUNDING_EVIDENCE_UNAVAILABLE,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
)
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_GLOBAL_CORPUS_LOAD_FAILED,
    REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    REASON_NEVER_SELECTED_IN_ANY_FOLD,
    ConfigAttemptEvidenceSummary,
    FoldSelectionEvidenceSummary,
    ScenarioEvidenceSummary,
)

__all__ = [
    "H6SummaryContractError",
    "normalize_and_validate_h6_summary",
]

_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_KNOWN_SCENARIO_NAMES = frozenset({"base", "primary_stress", "upward_stress"})

_REAL_FOLDS = foldmod.generate_frozen_fold_schedule(
    frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
)
_CANONICAL_FOLD_IDS = frozenset(f.fold_id for f in _REAL_FOLDS)
_CANONICAL_CONFIG_IDS_BY_STRATEGY: dict[str, frozenset[str]] = {
    "S1": frozenset(f"S1-{i:02d}" for i in range(12)),
    "S2": frozenset(f"S2-{i:02d}" for i in range(12)),
}
_UNIVERSE_SET = frozenset(frozen.UNIVERSE)

# The closed set of no_trade_reason_counts KEYS this system can ever
# legitimately produce -- literal hand-verified duplicate of
# ``run_rob944_campaign._known_no_trade_reasons()`` (H2's bare-string
# no-fill reasons, H4's funding-gate reasons, H3 S2's 6-code rejection
# set) -- an arbitrary caller-injected key must never be hashed/persisted.
_KNOWN_NO_TRADE_REASONS: frozenset[str] = frozenset(
    {
        "next_bar_unavailable",
        "daily_stop_active",
        "daily_entry_cap",
        "cooldown_active",
        "tp_below_min_distance",
        REASON_FUNDING_EVIDENCE_UNAVAILABLE,
        REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
        "confirmation_failed",
        "target_direction_invalid",
        "tp_above_max",
        "tp_below_r_min_sl",
        "tp_below_abs_floor",
    }
)


class H6SummaryContractError(ValueError):
    """A caller/producer-supplied ``ConfigAttemptEvidenceSummary`` failed the
    pure H6-parity normalizer/validator -- always fail-closed, never echoes
    the offending (possibly attacker-controlled) value."""


def _fail(message: str) -> None:
    raise H6SummaryContractError(message)


def _assert_exact_str(value: Any, *, context: str) -> str:
    if type(value) is not str:
        _fail(f"{context} must be an exact str")
    return value


def _assert_exact_str_or_none(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    return _assert_exact_str(value, context=context)


def _assert_exact_bool(value: Any, *, context: str) -> bool:
    if type(value) is not bool:
        _fail(f"{context} must be an exact bool")
    return value


def _assert_exact_int(value: Any, *, context: str) -> int:
    if type(value) is not int:
        _fail(f"{context} must be an exact int")
    return value


def _assert_exact_float(value: Any, *, context: str) -> float:
    if type(value) is not float:
        _fail(f"{context} must be an exact float")
    return value


def _assert_hex64(value: Any, *, context: str) -> str:
    if type(value) is not str or not _HEX64_RE.match(value):
        _fail(f"{context} must be a lowercase 64-hex digest")
    return value


_KNOWN_DIAGNOSTIC_TRANSPORTS = frozenset({"in_process"})
_KNOWN_DIAGNOSTIC_STAGES = frozenset({"generator", "funding_gate", "engine"})


def _normalize_child_failure_evidence(
    row: Any, *, context: str
) -> ChildFailureEvidence:
    """ROB-970 (Q2, Fable-approved): additive, persistence-only diagnostic
    evidence -- exact-type gated the same way every other H6 row is, but
    deliberately carries NO hash/identity role (never touched by
    ``_recompute_fold_evidence_hash_and_run_identity``)."""
    if type(row) is not ChildFailureEvidence:
        _fail(f"{context} must be an exact ChildFailureEvidence")
    transport = _assert_exact_str(row.transport, context=f"{context} transport")
    if transport not in _KNOWN_DIAGNOSTIC_TRANSPORTS:
        _fail(f"{context} transport is outside the closed known-transport set")
    stage = _assert_exact_str(row.stage, context=f"{context} stage")
    if stage not in _KNOWN_DIAGNOSTIC_STAGES:
        _fail(f"{context} stage is outside the closed known-stage set")
    _assert_exact_str(row.exception_type, context=f"{context} exception_type")
    _assert_exact_str(row.message, context=f"{context} message")
    _assert_exact_str(row.traceback_text, context=f"{context} traceback_text")
    _assert_exact_str_or_none(row.stderr, context=f"{context} stderr")
    if transport == "in_process" and row.stderr is not None:
        _fail(f"{context} in_process transport must never fabricate a stderr value")
    _assert_exact_str(row.strategy, context=f"{context} strategy")
    _assert_exact_str(row.config_id, context=f"{context} config_id")
    _assert_exact_str_or_none(row.symbol, context=f"{context} symbol")
    _assert_exact_str_or_none(row.fold_id, context=f"{context} fold_id")
    _assert_exact_str_or_none(row.scenario_name, context=f"{context} scenario_name")
    _assert_hex64(row.signature, context=f"{context} signature")
    occurrence_count = _assert_exact_int(
        row.occurrence_count, context=f"{context} occurrence_count"
    )
    if occurrence_count < 1:
        _fail(f"{context} occurrence_count must be >= 1")
    _assert_exact_bool(row.truncated, context=f"{context} truncated")
    return row


def _normalize_diagnostic_evidence(
    raw_value: Any, *, context: str
) -> tuple[ChildFailureEvidence, ...]:
    if type(raw_value) is not tuple:
        _fail(f"{context} must be an exact tuple")
    # R2 audit (one cap policy, MAX_DISTINCT_SIGNATURES=32, at every trust
    # boundary -- never only the producer helper).
    if len(raw_value) > MAX_DISTINCT_SIGNATURES:
        _fail(f"{context} must have at most {MAX_DISTINCT_SIGNATURES} entries")
    return tuple(
        _normalize_child_failure_evidence(row, context=f"{context}#{idx}")
        for idx, row in enumerate(raw_value)
    )


def _normalize_diagnostic_overflow(
    raw_value: Any, *, context: str
) -> DiagnosticOverflowMetadata:
    """ROB-970 R1 (Q1=A, cap=32): the honest overflow accounting is equally
    additive/persistence-only -- exact-type gated, never touched by any
    semantic hash."""
    if type(raw_value) is not DiagnosticOverflowMetadata:
        _fail(f"{context} must be an exact DiagnosticOverflowMetadata")
    truncated = _assert_exact_bool(raw_value.truncated, context=f"{context} truncated")
    omitted_distinct_signatures = _assert_exact_int(
        raw_value.omitted_distinct_signatures,
        context=f"{context} omitted_distinct_signatures",
    )
    if omitted_distinct_signatures < 0:
        _fail(f"{context} omitted_distinct_signatures must be >= 0")
    omitted_occurrences = _assert_exact_int(
        raw_value.omitted_occurrences, context=f"{context} omitted_occurrences"
    )
    if omitted_occurrences < 0:
        _fail(f"{context} omitted_occurrences must be >= 0")
    if omitted_distinct_signatures > omitted_occurrences:
        _fail(
            f"{context} omitted_distinct_signatures cannot exceed omitted_occurrences"
        )
    # R2 audit: truncated is a DERIVED fact, never an independent caller
    # assertion -- truncated is False iff both omitted counts are zero;
    # truncated is True iff omitted_occurrences > 0 (equivalent given the
    # distinct<=occurrences invariant just enforced above).
    if truncated != (omitted_occurrences > 0):
        _fail(f"{context} truncated must be exactly (omitted_occurrences > 0)")
    return DiagnosticOverflowMetadata(
        truncated=truncated,
        omitted_distinct_signatures=omitted_distinct_signatures,
        omitted_occurrences=omitted_occurrences,
    )


def _attempt_allowed_reasons_by_status() -> dict[str, frozenset]:
    return {
        "completed": frozenset(),
        "rejected": frozenset(
            {REASON_DATA_GAP_IN_POSITION, REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS}
        ),
        "crashed": frozenset(
            {REASON_CHILD_EXECUTION_CRASHED, REASON_GLOBAL_CORPUS_LOAD_FAILED}
        ),
        "timeout": frozenset({REASON_CHILD_EXECUTION_TIMEOUT}),
    }


def _scenario_allowed_reasons_by_status() -> dict[str, frozenset]:
    allowed = dict(_attempt_allowed_reasons_by_status())
    allowed["rejected"] = frozenset({REASON_DATA_GAP_IN_POSITION})
    allowed["never_selected"] = frozenset({REASON_NEVER_SELECTED_IN_ANY_FOLD})
    return allowed


def _assert_status_reason_contract(
    status: str, reason_code: str | None, *, allowed_by_status: dict, context: str
) -> None:
    allowed = allowed_by_status.get(status)
    if allowed is None:
        _fail(f"{context} has an unknown status")
        return
    if status == "completed":
        if reason_code is not None:
            _fail(f"{context} has status='completed' but a non-null reason_code")
        return
    if reason_code not in allowed:
        _fail(f"{context} has a reason_code not permitted for its status")


def _normalize_no_trade_reason_counts(value: Any, *, context: str) -> dict[str, int]:
    if type(value) is not dict:
        _fail(f"{context} no_trade_reason_counts must be an exact dict")
    normalized: dict[str, int] = {}
    for idx, (key, val) in enumerate(value.items()):
        key = _assert_exact_str(
            key, context=f"{context} no_trade_reason_counts key#{idx}"
        )
        if key not in _KNOWN_NO_TRADE_REASONS:
            _fail(f"{context} no_trade_reason_counts has an unknown key")
        val = _assert_exact_int(
            val, context=f"{context} no_trade_reason_counts value#{idx}"
        )
        if val < 0:
            _fail(f"{context} no_trade_reason_counts has a negative count")
        normalized[key] = val
    return normalized


def _normalize_scenario_row(
    row: Any, *, context: str, allowed_by_status: dict
) -> ScenarioEvidenceSummary:
    if type(row) is not ScenarioEvidenceSummary:
        _fail(f"{context} must be an exact ScenarioEvidenceSummary")
    scenario_name = _assert_exact_str(
        row.scenario_name, context=f"{context} scenario_name"
    )
    status = _assert_exact_str(row.status, context=f"{context} status")
    reason_code = _assert_exact_str_or_none(
        row.reason_code, context=f"{context} reason_code"
    )
    trade_count = _assert_exact_int(row.trade_count, context=f"{context} trade_count")
    if trade_count < 0:
        _fail(f"{context} trade_count must be nonnegative")
    artifact_hash = _assert_hex64(row.artifact_hash, context=f"{context} artifact_hash")
    no_trade_reason_counts = _normalize_no_trade_reason_counts(
        row.no_trade_reason_counts, context=context
    )
    _assert_status_reason_contract(
        status, reason_code, allowed_by_status=allowed_by_status, context=context
    )
    return ScenarioEvidenceSummary(
        scenario_name=scenario_name,
        status=status,
        reason_code=reason_code,
        trade_count=trade_count,
        artifact_hash=artifact_hash,
        no_trade_reason_counts=no_trade_reason_counts,
    )


def _normalize_fold_row(
    row: Any, *, strategy: str, context: str
) -> FoldSelectionEvidenceSummary:
    if type(row) is not FoldSelectionEvidenceSummary:
        _fail(f"{context} must be an exact FoldSelectionEvidenceSummary")
    fold_id = _assert_exact_str(row.fold_id, context=f"{context} fold_id")
    if fold_id not in _CANONICAL_FOLD_IDS:
        _fail(f"{context} fold_id is outside the closed canonical set")
    fold_selected_config_id = _assert_exact_str_or_none(
        row.fold_selected_config_id, context=f"{context} fold_selected_config_id"
    )
    if (
        fold_selected_config_id is not None
        and fold_selected_config_id
        not in _CANONICAL_CONFIG_IDS_BY_STRATEGY.get(strategy, frozenset())
    ):
        _fail(
            f"{context} fold_selected_config_id is outside this strategy's exact "
            "frozen 12-config set"
        )
    raw_eligible = row.eligible_symbols
    if type(raw_eligible) is not tuple:
        _fail(f"{context} eligible_symbols must be an exact tuple")
    eligible_symbols = tuple(
        _assert_exact_str(s, context=f"{context} eligible_symbols#{idx}")
        for idx, s in enumerate(raw_eligible)
    )
    raw_excluded = row.excluded_symbols
    if type(raw_excluded) is not tuple:
        _fail(f"{context} excluded_symbols must be an exact tuple")
    excluded_symbols: list[tuple[str, str]] = []
    for idx, entry in enumerate(raw_excluded):
        if type(entry) is not tuple or len(entry) != 2:
            _fail(f"{context} excluded_symbols#{idx} must be an exact 2-tuple")
        raw_symbol, raw_reason = entry
        symbol = _assert_exact_str(
            raw_symbol, context=f"{context} excluded_symbols#{idx} symbol"
        )
        reason = _assert_exact_str(
            raw_reason, context=f"{context} excluded_symbols#{idx} reason"
        )
        if reason != INSUFFICIENT_SYMBOL_EVIDENCE_REASON:
            _fail(
                f"{context} excluded_symbols#{idx} has a reason outside the closed allowlist"
            )
        excluded_symbols.append((symbol, reason))

    eligible_set = set(eligible_symbols)
    if len(eligible_set) != len(eligible_symbols):
        _fail(f"{context} has a duplicate eligible_symbols entry")
    excluded_symbol_names = [s for s, _r in excluded_symbols]
    excluded_set = set(excluded_symbol_names)
    if len(excluded_set) != len(excluded_symbol_names):
        _fail(f"{context} has a duplicate excluded_symbols entry")
    if not eligible_set.issubset(_UNIVERSE_SET) or not excluded_set.issubset(
        _UNIVERSE_SET
    ):
        _fail(f"{context} has a symbol outside the frozen 4-symbol universe")
    if eligible_set & excluded_set:
        _fail(f"{context} has a symbol in BOTH eligible_symbols and excluded_symbols")
    if eligible_set | excluded_set != _UNIVERSE_SET:
        _fail(
            f"{context} eligible_symbols/excluded_symbols do not exactly cover the "
            "frozen 4-symbol universe"
        )
    expected_eligible_order = tuple(s for s in frozen.UNIVERSE if s in eligible_set)
    if eligible_symbols != expected_eligible_order:
        _fail(f"{context} eligible_symbols does not preserve the frozen universe order")
    expected_excluded_order = tuple(s for s in frozen.UNIVERSE if s in excluded_set)
    if tuple(excluded_symbol_names) != expected_excluded_order:
        _fail(f"{context} excluded_symbols does not preserve the frozen universe order")

    rejected = _assert_exact_bool(row.rejected, context=f"{context} rejected")
    rejection_reason = _assert_exact_str_or_none(
        row.rejection_reason, context=f"{context} rejection_reason"
    )
    if rejected:
        if rejection_reason != INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON:
            _fail(
                f"{context} is rejected but rejection_reason is not the expected sentinel"
            )
        if row.equal_weight_expectancy_bps is not None:
            _fail(f"{context} is rejected but equal_weight_expectancy_bps is not None")
        if row.pooled_expectancy_bps is not None:
            _fail(f"{context} is rejected but pooled_expectancy_bps is not None")
        equal_weight_expectancy_bps = None
        pooled_expectancy_bps = None
    else:
        if rejection_reason is not None:
            _fail(f"{context} is not rejected but carries a non-null rejection_reason")
        equal_weight_expectancy_bps = _assert_exact_float(
            row.equal_weight_expectancy_bps,
            context=f"{context} equal_weight_expectancy_bps",
        )
        pooled_expectancy_bps = _assert_exact_float(
            row.pooled_expectancy_bps, context=f"{context} pooled_expectancy_bps"
        )
    profit_factor = _assert_exact_float(
        row.profit_factor, context=f"{context} profit_factor"
    )
    train_input_hash = _assert_hex64(
        row.train_input_hash, context=f"{context} train_input_hash"
    )
    no_trade_reason_counts = _normalize_no_trade_reason_counts(
        row.no_trade_reason_counts, context=context
    )
    return FoldSelectionEvidenceSummary(
        fold_id=fold_id,
        fold_selected_config_id=fold_selected_config_id,
        eligible_symbols=eligible_symbols,
        excluded_symbols=tuple(excluded_symbols),
        equal_weight_expectancy_bps=equal_weight_expectancy_bps,
        pooled_expectancy_bps=pooled_expectancy_bps,
        profit_factor=profit_factor,
        rejected=rejected,
        rejection_reason=rejection_reason,
        train_input_hash=train_input_hash,
        no_trade_reason_counts=no_trade_reason_counts,
    )


def _validate_fold_trace_cardinality(
    fold_ids: tuple[str, ...], *, status: str, reason_code: str | None
) -> None:
    if not fold_ids:
        if status == "crashed" and reason_code == REASON_GLOBAL_CORPUS_LOAD_FAILED:
            return
        _fail(
            "empty fold_selection_trace is only valid for the exact "
            "global-corpus-load-failure signature"
        )
        return
    if len(set(fold_ids)) != len(fold_ids) or set(fold_ids) != _CANONICAL_FOLD_IDS:
        _fail(
            "fold_selection_trace does not have exactly the 8 canonical unique fold IDs"
        )


def normalize_and_validate_h6_summary(
    raw: Any, *, expected_strategy: str, expected_config_id: str
) -> ConfigAttemptEvidenceSummary:
    """One-pass normalize + validate a caller/producer-supplied
    ``ConfigAttemptEvidenceSummary`` against the real H6-build boundary's own
    nested invariants (mirrored, never imported from ``run_rob944_campaign``).

    Returns a BRAND-NEW ``ConfigAttemptEvidenceSummary`` with both
    ``scenario_summaries``/``fold_selection_trace`` canonically ordered
    (by ``scenario_name``/``fold_id``) -- callers must hash/cross-bind ONLY
    this returned snapshot, never the original ``raw`` object again.
    """
    # Independent audit correction (Task 1E, I1): the lineage arguments
    # THEMSELVES must be exact-`str`-gated BEFORE any dict/set membership
    # check -- an unhashable value (list/dict) would otherwise raise a raw,
    # uncontrolled ``TypeError`` here (never a stable ``H6SummaryContractError``),
    # and a ``str`` subclass overriding ``__eq__``/``__hash__`` could pass
    # membership while its actual buffer content differs. Snapshotting via
    # ``_assert_exact_str`` closes both before either value is ever used in
    # a membership check, equality comparison, or context string.
    expected_strategy = _assert_exact_str(
        expected_strategy, context="expected_strategy"
    )
    expected_config_id = _assert_exact_str(
        expected_config_id, context="expected_config_id"
    )
    if expected_strategy not in _CANONICAL_CONFIG_IDS_BY_STRATEGY:
        _fail("expected_strategy is outside the closed {S1, S2} set")
    if expected_config_id not in _CANONICAL_CONFIG_IDS_BY_STRATEGY[expected_strategy]:
        _fail("expected_config_id is outside its strategy's exact frozen 12-config set")
    if type(raw) is not ConfigAttemptEvidenceSummary:
        _fail("raw must be an exact ConfigAttemptEvidenceSummary")

    strategy = _assert_exact_str(raw.strategy, context="strategy")
    config_id = _assert_exact_str(raw.config_id, context="config_id")
    if strategy != expected_strategy:
        _fail("summary.strategy does not match the expected strategy slot")
    if config_id != expected_config_id:
        _fail("summary.config_id does not match the expected config slot")

    status = _assert_exact_str(raw.status, context="status")
    reason_code = _assert_exact_str_or_none(raw.reason_code, context="reason_code")
    _assert_status_reason_contract(
        status,
        reason_code,
        allowed_by_status=_attempt_allowed_reasons_by_status(),
        context="attempt",
    )

    raw_scenario_summaries = raw.scenario_summaries
    if type(raw_scenario_summaries) is not tuple:
        _fail("scenario_summaries must be an exact tuple")
    scenario_allowed = _scenario_allowed_reasons_by_status()
    normalized_scenario_rows = tuple(
        _normalize_scenario_row(
            row, context=f"scenario#{idx}", allowed_by_status=scenario_allowed
        )
        for idx, row in enumerate(raw_scenario_summaries)
    )
    scenario_names = tuple(row.scenario_name for row in normalized_scenario_rows)
    if len(scenario_names) != 3 or set(scenario_names) != _KNOWN_SCENARIO_NAMES:
        _fail(
            "scenario_summaries does not have exactly the 3 unique canonical scenarios"
        )

    raw_fold_trace = raw.fold_selection_trace
    if type(raw_fold_trace) is not tuple:
        _fail("fold_selection_trace must be an exact tuple")
    normalized_fold_rows = tuple(
        _normalize_fold_row(row, strategy=strategy, context=f"fold#{idx}")
        for idx, row in enumerate(raw_fold_trace)
    )
    _validate_fold_trace_cardinality(
        tuple(row.fold_id for row in normalized_fold_rows),
        status=status,
        reason_code=reason_code,
    )

    normalized_diagnostic_evidence = _normalize_diagnostic_evidence(
        raw.diagnostic_evidence, context="diagnostic_evidence"
    )
    normalized_diagnostic_overflow = _normalize_diagnostic_overflow(
        raw.diagnostic_overflow, context="diagnostic_overflow"
    )

    return ConfigAttemptEvidenceSummary(
        strategy=strategy,
        config_id=config_id,
        status=status,
        reason_code=reason_code,
        scenario_summaries=tuple(
            sorted(normalized_scenario_rows, key=lambda row: row.scenario_name)
        ),
        fold_selection_trace=tuple(
            sorted(normalized_fold_rows, key=lambda row: row.fold_id)
        ),
        diagnostic_evidence=normalized_diagnostic_evidence,
        diagnostic_overflow=normalized_diagnostic_overflow,
    )
