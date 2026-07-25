"""ROB-945 (H5) -- external OOS signal-capture seam over frozen H4.

Fable Q2=A ruling (2026-07-18, orch-fable-answer-rob945-20260718.md): H5
must NOT touch ``rob944_walkforward.py`` (or any other H4 byte) to obtain
the validated, pre-funding/pre-engine OOS ``SignalEvent`` batch it needs for
signal-concurrency evidence and MDD-in-R signal linkage. The only externally
controlled seam is the ``generate_signals`` callable supplied on each
``ConfigSpec`` -- H4 calls it identically for both its TRAIN and OOS phases
(same three positional args, same ``fold.fold_id``), so this module wraps
that callable, classifies each call as TRAIN or OOS purely from the bars
slice's timestamps against the caller-supplied ``fold_schedule`` (H4's own
test ``test_train_and_oos_signal_generators_only_see_their_own_window_bars``
establishes this classification is a safe invariant of H4's slicing), mirrors
H4's OWN validation functions (reused, never re-implemented) to only record
genuinely valid OOS evidence, and then returns the real generator's raw
result completely UNCHANGED -- by identity, not just equality.

Hazards this module closes (captain adversarial review, 2026-07-18):

1. A generator MAY legitimately return a one-shot iterator (a bare
   generator object) rather than a re-iterable ``tuple``/``list``/
   ``GeneratedSignalBatch``. Calling ``_normalize_generated_batch`` on such
   an object for capture purposes would CONSUME it before H4 itself gets a
   chance to -- silently starving H4 of signals it should have seen. This
   module therefore only ever inspects the three known-safe, already
   re-iterable shapes; anything else is left completely untouched (capture
   marks that call invalid, never inspects the object).
2. A partial/duplicate/forged capture failure must never leave a sink that
   LOOKS complete. Every mutation is staged and committed atomically
   (all-or-nothing per call), any anomaly permanently latches the sink
   invalid via a CLOSED, stable reason code (never a raw exception
   type/message -- this repository's established convention is that no
   hashed/persisted evidence ever carries raw exception text), and
   ``snapshot()`` raises rather than silently returning a partial view.
3. A capture sink must be explicitly finalized against the SET of OOS
   calls H4 was expected to make, keyed by the FULL frozen identity
   ``(strategy, fold_id, selected_config_id, symbol)`` -- an omitted call
   must never be indistinguishable from a call that legitimately produced
   zero signals, and this identity must be as specific as the fold's own
   winning config, not just fold+symbol.
4. The expected-call derivation hard-pins the frozen 4-symbol universe
   (``rob941_frozen_scope.UNIVERSE``) itself -- it never trusts a
   caller-supplied universe, which could silently narrow coverage.

The wrapper's own capture-side work (bars/fold lookup, mirrored validation)
must NEVER be the reason H4's control flow changes: any exception raised
while capturing is caught and turned into a latched-invalid sink state,
since H4 performs its OWN, official validation of the very same raw result
immediately afterward and will crash that attempt through its own normal
path if the result is invalid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import rob941_frozen_scope as frozen
from rob940_engine import SignalEvent
from rob944_folds import Fold
from rob944_walkforward import (
    ConfigSpec,
    GeneratedSignalBatch,
    _assert_no_signal_rejection_ts_collision,
    _normalize_generated_batch,
    _validate_generated_rejections,
    _validate_generated_signals,
)

# Only these shapes are safe to inspect without risking a one-shot consume:
# tuple/list are already fully materialized (re-iterating is harmless), and
# GeneratedSignalBatch already holds materialized tuples internally.
_SAFE_BATCH_SHAPES = (GeneratedSignalBatch, tuple, list)

# Closed, stable capture-invalid reason codes -- never a raw exception
# type/message (matches this repository's established no-raw-exception-text
# convention, e.g. ``rob944_walkforward._stable_terminal_hash``).
REASON_UNSUPPORTED_BATCH_SHAPE = "unsupported_batch_shape"
REASON_INVALID_SIGNAL_TYPE = "invalid_signal_type"
REASON_DUPLICATE_SIGNAL_IDENTITY = "duplicate_signal_identity"
REASON_DUPLICATE_OBSERVED_CALL = "duplicate_observed_call"
REASON_SIGNAL_VALIDATION_FAILED = "signal_validation_failed"
REASON_CAPTURE_INCOMPLETE = "capture_incomplete"

_CLOSED_INVALID_REASON_CODES = frozenset(
    {
        REASON_UNSUPPORTED_BATCH_SHAPE,
        REASON_INVALID_SIGNAL_TYPE,
        REASON_DUPLICATE_SIGNAL_IDENTITY,
        REASON_DUPLICATE_OBSERVED_CALL,
        REASON_SIGNAL_VALIDATION_FAILED,
        REASON_CAPTURE_INCOMPLETE,
    }
)

# Full call identity: an OOS call is scoped to one strategy, one fold, the
# fold's ONE selected winning config, and one symbol.
_CallIdentity = tuple[str, str | None, str, str]


def _canonical_event_key(signal: SignalEvent) -> tuple[str, str | None, str, int]:
    return (signal.strategy, signal.fold_id, signal.symbol, signal.signal_ts)


class CaptureInvalidError(RuntimeError):
    """The capture sink observed an anomaly (duplicate/forged/unsupported
    shape/incomplete coverage) and is now PERMANENTLY invalid -- its
    evidence must never be used as complete H5 input. Raised by
    ``snapshot()``, never swallowed by any caller of this module."""


class OosSignalCaptureSink:
    """Append-only, atomically-staged collector of validated OOS
    ``SignalEvent`` objects, latched invalid on any anomaly.

    Only ever written to from inside a wrapped ``generate_signals`` closure,
    strictly AFTER the real generator's raw result has already been
    computed -- writes here can never affect what H4 itself sees or does.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str | None, str, int], SignalEvent] = {}
        self._observed_calls: set[_CallIdentity] = set()
        self._invalid_reason_code: str | None = None
        self._finalized = False
        self._complete = False

    @property
    def is_invalid(self) -> bool:
        return self._invalid_reason_code is not None

    @property
    def invalid_reason_code(self) -> str | None:
        return self._invalid_reason_code

    def mark_invalid(self, reason_code: str) -> None:
        """Latch the sink permanently invalid with a CLOSED, stable reason
        code. The FIRST reason wins and is never overwritten or cleared --
        once invalid, always invalid."""
        if reason_code not in _CLOSED_INVALID_REASON_CODES:
            reason_code = REASON_SIGNAL_VALIDATION_FAILED
        if self._invalid_reason_code is None:
            self._invalid_reason_code = reason_code

    def record_call_observed(
        self, *, strategy: str, fold_id: str | None, config_id: str, symbol: str
    ) -> None:
        """Marks that an OOS-phase call for this exact
        ``(strategy, fold_id, config_id, symbol)`` identity was actually
        observed, REGARDLESS of whether it produced any signals -- this is
        what lets ``finalize`` tell a legitimate empty batch apart from a
        call H4 never made through this wrapper at all."""
        key: _CallIdentity = (strategy, fold_id, config_id, symbol)
        if key in self._observed_calls:
            self.mark_invalid(REASON_DUPLICATE_OBSERVED_CALL)
            return
        self._observed_calls.add(key)

    def try_extend(self, signals: Sequence[SignalEvent]) -> None:
        """Best-effort, atomic per-call staged commit: builds a full
        candidate copy of the key->signal mapping first and only replaces
        the committed state if EVERY signal in ``signals`` is valid and
        non-duplicate -- a mid-batch failure can never leave a partial
        subset of this call's signals committed. Never raises; any anomaly
        latches invalid state instead."""
        if self._invalid_reason_code is not None:
            return
        staged = dict(self._by_key)
        for signal in signals:
            if not isinstance(signal, SignalEvent):
                self.mark_invalid(REASON_INVALID_SIGNAL_TYPE)
                return
            key = _canonical_event_key(signal)
            if key in staged:
                self.mark_invalid(REASON_DUPLICATE_SIGNAL_IDENTITY)
                return
            staged[key] = signal
        self._by_key = staged

    def finalize(self, expected_calls: set[_CallIdentity]) -> None:
        """Must be called exactly once, after the full walk-forward run, with
        the exact set of ``(strategy, fold_id, config_id, symbol)`` OOS
        calls H4 was expected to make (derive via
        ``expected_oos_calls_from_walkforward_result``, which reads the
        unchanged ``WalkForwardResult.folds`` selection trace). Missing or
        extra coverage latches the sink invalid -- it is NEVER silently
        treated as "zero signals" for that call."""
        self._finalized = True
        if self._invalid_reason_code is not None:
            self._complete = False
            return
        if self._observed_calls != set(expected_calls):
            self.mark_invalid(REASON_CAPTURE_INCOMPLETE)
            self._complete = False
            return
        self._complete = True

    def snapshot(self) -> tuple[SignalEvent, ...]:
        if not self._finalized:
            raise CaptureInvalidError(
                "OosSignalCaptureSink.snapshot() called before finalize(expected_calls) "
                "-- completeness has not been proven"
            )
        if self._invalid_reason_code is not None or not self._complete:
            raise CaptureInvalidError(
                f"OosSignalCaptureSink is invalid/incomplete "
                f"(reason_code={self._invalid_reason_code!r}) -- cannot be used as "
                "complete H5 evidence"
            )
        # canonically sorted -- the evidence's identity must never depend on
        # H4's internal fold/symbol iteration order.
        return tuple(sorted(self._by_key.values(), key=_canonical_event_key))


def _is_oos_window(fold: Fold, bars_slice: Sequence) -> bool:
    if not bars_slice:
        return False
    first_ts = bars_slice[0].ts
    last_ts = bars_slice[-1].ts
    return fold.oos_start_ms <= first_ts and last_ts < fold.oos_end_ms


def wrap_config_spec_for_oos_capture(
    config_spec: ConfigSpec,
    *,
    strategy: str,
    fold_by_id: Mapping[str, Fold],
    sink: OosSignalCaptureSink,
) -> ConfigSpec:
    """Return a NEW ``ConfigSpec`` (same ``config_id``) whose
    ``generate_signals`` records a copy of the validated OOS signal batch
    into ``sink`` as a side effect, then returns the real generator's raw
    result unchanged (same object, by identity)."""

    real_generate_signals = config_spec.generate_signals

    def _wrapped(symbol, bars_slice, fold_id):
        raw_result = real_generate_signals(symbol, bars_slice, fold_id)
        try:
            fold = fold_by_id[fold_id]
            if _is_oos_window(fold, bars_slice):
                sink.record_call_observed(
                    strategy=strategy,
                    fold_id=fold_id,
                    config_id=config_spec.config_id,
                    symbol=symbol,
                )
                if not isinstance(raw_result, _SAFE_BATCH_SHAPES):
                    sink.mark_invalid(REASON_UNSUPPORTED_BATCH_SHAPE)
                else:
                    batch = _normalize_generated_batch(raw_result)
                    signals = batch.signals
                    _validate_generated_signals(
                        signals,
                        strategy=strategy,
                        config_id=config_spec.config_id,
                        symbol=symbol,
                        fold_id=fold_id,
                        window_start_ms=fold.oos_start_ms,
                        window_end_ms=fold.oos_end_ms,
                    )
                    rejections = _validate_generated_rejections(
                        batch.rejections,
                        strategy=strategy,
                        config_id=config_spec.config_id,
                        symbol=symbol,
                        fold_id=fold_id,
                        window_start_ms=fold.oos_start_ms,
                        window_end_ms=fold.oos_end_ms,
                    )
                    _assert_no_signal_rejection_ts_collision(
                        signals,
                        rejections,
                        strategy=strategy,
                        config_id=config_spec.config_id,
                        symbol=symbol,
                        fold_id=fold_id,
                    )
                    sink.try_extend(signals)
        except Exception:  # noqa: BLE001 -- capture-side failure must never affect H4's own control flow, and never persists raw exception text; H4 independently validates/crashes on this same raw_result via its own path.
            sink.mark_invalid(REASON_SIGNAL_VALIDATION_FAILED)
        return raw_result

    return ConfigSpec(config_id=config_spec.config_id, generate_signals=_wrapped)


def wrap_config_specs_for_oos_capture(
    config_specs: Sequence[ConfigSpec],
    *,
    strategy: str,
    fold_schedule: Sequence[Fold],
    sink: OosSignalCaptureSink,
) -> tuple[ConfigSpec, ...]:
    fold_by_id = {fold.fold_id: fold for fold in fold_schedule}
    return tuple(
        wrap_config_spec_for_oos_capture(
            config_spec, strategy=strategy, fold_by_id=fold_by_id, sink=sink
        )
        for config_spec in config_specs
    )


def expected_oos_calls_from_walkforward_result(result) -> set[_CallIdentity]:
    """Derive the exact set of ``(strategy, fold_id, config_id, symbol)``
    OOS calls H4 was expected to make from the UNCHANGED
    ``WalkForwardResult`` -- ``strategy`` is read directly off ``result``
    itself (``WalkForwardResult.strategy``), never a redundant caller-
    supplied value that could silently drift from what the result actually
    is. A fold only triggers an OOS evaluation when it actually selected a
    winning config (``selection_trace.selected_config_id is not None``);
    every fold that did so triggers exactly one call per symbol in the
    FROZEN 4-symbol universe (never a caller-supplied universe, which could
    silently narrow expected coverage)."""
    strategy = result.strategy
    expected: set[_CallIdentity] = set()
    for fold_result in result.folds:
        selected_config_id = fold_result.selection_trace.selected_config_id
        if selected_config_id is not None:
            for symbol in frozen.UNIVERSE:
                expected.add(
                    (strategy, fold_result.fold.fold_id, selected_config_id, symbol)
                )
    return expected
