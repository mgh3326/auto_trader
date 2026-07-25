"""ROB-945 (H5) -- OOS signal capture seam RED tests.

Fable Q2=A ruling (2026-07-18, orch-fable-answer-rob945-20260718.md): H5
must capture the validated, pre-funding/pre-engine OOS ``SignalEvent`` batch
via an EXTERNAL wrapper around ``ConfigSpec.generate_signals`` -- H4's own
bytes (``rob944_walkforward.py``) are never touched. The required condition
is a false-coupling guard: capture on vs off must produce a BYTE-IDENTICAL
``WalkForwardResult`` (dataclass equality AND canonical artifact-hash
equality) -- proof the wrapper is a pure observer with zero effect on H4.

Captain adversarial review (2026-07-18) added two hard requirements this
file also covers: (1) a generator returning a one-shot iterator must never
be consumed by the capture side before H4 consumes it, and (2) the sink
must be a staged, atomically-committed, permanently-latched-invalid state
machine that also proves COMPLETE coverage (via ``finalize`` against the
exact expected OOS-call set) -- a missing/omitted call must never be
indistinguishable from a legitimate zero-signal call.
"""

from __future__ import annotations

import pytest
from rob940_engine import Bar1m, SignalEvent
from rob941_funding_sidecar import FundingSidecar
from rob944_folds import Fold
from rob944_walkforward import ConfigSpec, run_walkforward
from rob945_canonical_payload import to_canonical_payload
from rob945_capture import (
    CaptureInvalidError,
    OosSignalCaptureSink,
    expected_oos_calls_from_walkforward_result,
    wrap_config_specs_for_oos_capture,
)

from research_contracts.canonical_hash import canonical_json, canonical_sha256

_SYMBOLS = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000

_FOLD_0 = Fold(
    fold_id="fold-00",
    fold_index=0,
    train_start_ms=0,
    train_end_ms=3 * _DAY_MS,
    embargo_start_ms=3 * _DAY_MS,
    embargo_end_ms=3 * _DAY_MS + 3 * _HOUR_MS,
    oos_start_ms=3 * _DAY_MS + 3 * _HOUR_MS,
    oos_end_ms=3 * _DAY_MS + 3 * _HOUR_MS + 2 * _DAY_MS,
)
_ROLL_MS = 2 * _DAY_MS
_FOLD_1 = Fold(
    fold_id="fold-01",
    fold_index=1,
    train_start_ms=_ROLL_MS,
    train_end_ms=_ROLL_MS + 3 * _DAY_MS,
    embargo_start_ms=_ROLL_MS + 3 * _DAY_MS,
    embargo_end_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS,
    oos_start_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS,
    oos_end_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS + 2 * _DAY_MS,
)
_WINDOW_END = _FOLD_1.oos_end_ms
_FOLD_SCHEDULE = (_FOLD_0, _FOLD_1)


def _bar(ts, price=100.0):
    return Bar1m(ts=ts, open=price, high=price, low=price, close=price, volume=1.0)


def _flat_bars(start_ms, end_ms, overrides=None):
    overrides = overrides or {}
    out = []
    ts = start_ms
    while ts < end_ms:
        out.append(_bar(ts, overrides.get(ts, 100.0)))
        ts += 60_000
    return tuple(out)


def _permissive_funding_sidecars():
    from funding_oi_archive import FundingRow

    return {
        s: FundingSidecar.from_rows(
            s,
            [
                FundingRow(
                    calc_time=-10_000_000,
                    funding_interval_hours=8,
                    last_funding_rate=0.0,
                )
            ],
        )
        for s in _SYMBOLS
    }


def _no_gaps():
    return dict.fromkeys(_SYMBOLS, ())


def _entries_in_zone(zone_start_ms, zone_end_ms):
    entries = []
    day_start = (zone_start_ms // _DAY_MS) * _DAY_MS
    while day_start < zone_end_ms:
        day_end = day_start + _DAY_MS
        iter_start = max(day_start, zone_start_ms)
        window_end = min(day_end, zone_end_ms)
        for offset in (0, 120_000):
            entry_ts = iter_start + offset
            deadline_ts = entry_ts + 60_000
            if entry_ts < window_end and deadline_ts <= window_end:
                entries.append(entry_ts)
        day_start += _DAY_MS
    return entries


def _make_signal(symbol, signal_ts, *, config_id, fold_id, strategy="S1"):
    return SignalEvent(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side="long",
        sl_distance_bps=200.0,
        tp_distance_bps=300.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id=fold_id,
    )


def _is_oos_call(fold_id, bars_slice):
    """Mirrors the capture wrapper's own phase classification -- needed here
    because H4's rolling folds legitimately overlap in absolute time (e.g.
    fold-01's TRAIN window spans the same calendar days as fold-00's OOS
    window), so a synthetic generator that emits fixed absolute
    timestamps must itself be phase-aware to keep train/oos evidence
    genuinely distinct for this test's own assertions."""
    fold = {"fold-00": _FOLD_0, "fold-01": _FOLD_1}[fold_id]
    if not bars_slice:
        return False
    first_ts, last_ts = bars_slice[0].ts, bars_slice[-1].ts
    return fold.oos_start_ms <= first_ts and last_ts < fold.oos_end_ms


def _train_and_oos_entries():
    train_entries: dict[str, set[int]] = {}
    oos_entries: dict[str, set[int]] = {}
    for fold in (_FOLD_0, _FOLD_1):
        train_entries[fold.fold_id] = set(
            _entries_in_zone(fold.train_start_ms, fold.train_end_ms)
        )
        oos_entries[fold.fold_id] = set(
            _entries_in_zone(fold.oos_start_ms, fold.oos_end_ms)
        )
    return train_entries, oos_entries


_TRAIN_ENTRIES_BY_FOLD, _OOS_ENTRIES_BY_FOLD = _train_and_oos_entries()
_TRAIN_ENTRIES = set().union(*_TRAIN_ENTRIES_BY_FOLD.values())
_OOS_ENTRIES = set().union(*_OOS_ENTRIES_BY_FOLD.values())
_ALL_ENTRIES = _TRAIN_ENTRIES | _OOS_ENTRIES
_BARS_1M = {
    symbol: _flat_bars(0, _WINDOW_END, {ts + 60_000: 101.0 for ts in _ALL_ENTRIES})
    for symbol in _SYMBOLS
}


def _gen_factory(config_id):
    def _gen(symbol, bars_slice, fold_id):
        present = {b.ts for b in bars_slice}
        phase_entries = (
            _OOS_ENTRIES_BY_FOLD[fold_id]
            if _is_oos_call(fold_id, bars_slice)
            else _TRAIN_ENTRIES_BY_FOLD[fold_id]
        )
        return tuple(
            _make_signal(symbol, ts, config_id=config_id, fold_id=fold_id)
            for ts in sorted(phase_entries)
            if ts in present
        )

    return _gen


def _one_shot_gen_factory(config_id):
    """OOS-phase calls return a genuine one-shot generator object (NOT a
    re-iterable tuple/list) -- proves the capture wrapper never consumes it
    before H4 does."""

    def _gen(symbol, bars_slice, fold_id):
        present = {b.ts for b in bars_slice}
        phase_entries = (
            _OOS_ENTRIES_BY_FOLD[fold_id]
            if _is_oos_call(fold_id, bars_slice)
            else _TRAIN_ENTRIES_BY_FOLD[fold_id]
        )
        matching = [
            _make_signal(symbol, ts, config_id=config_id, fold_id=fold_id)
            for ts in sorted(phase_entries)
            if ts in present
        ]
        if _is_oos_call(fold_id, bars_slice):
            return (s for s in matching)  # one-shot generator, OOS phase only
        return tuple(matching)  # train phase stays a safe, re-iterable shape

    return _gen


def _twelve_configs(gen_factory=_gen_factory):
    return tuple(
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=gen_factory(f"S1-{i:02d}"))
        for i in range(12)
    )


def _run_walkforward_kwargs(configs):
    return {
        "strategy": "S1",
        "configs": configs,
        "bars_1m": _BARS_1M,
        "funding_sidecars": _permissive_funding_sidecars(),
        "gap_ranges": _no_gaps(),
        "fold_schedule": _FOLD_SCHEDULE,
    }


def _canonical_hash_of_result(result):
    return canonical_sha256(to_canonical_payload(result))


def _canonical_json_bytes_of_result(result):
    return canonical_json(to_canonical_payload(result)).encode("utf-8")


def test_capture_only_records_oos_phase_signals_never_train():
    sink = OosSignalCaptureSink()
    configs = _twelve_configs()
    wrapped = wrap_config_specs_for_oos_capture(
        configs, strategy="S1", fold_schedule=_FOLD_SCHEDULE, sink=sink
    )
    result = run_walkforward(**_run_walkforward_kwargs(wrapped))
    sink.finalize(expected_oos_calls_from_walkforward_result(result))
    captured = sink.snapshot()
    assert captured, "expected at least one captured OOS signal"
    # Per-fold membership (NOT a global-set check): fold-01's TRAIN window
    # legitimately overlaps fold-00's OOS window in absolute time (normal
    # rolling walk-forward), so the same absolute timestamp can be a
    # genuine OOS entry for one fold and a genuine TRAIN entry for another
    # -- what must never happen is a signal captured AS fold X's OOS
    # evidence actually being one of fold X's own TRAIN-zone timestamps.
    for sig in captured:
        assert sig.signal_ts in _OOS_ENTRIES_BY_FOLD[sig.fold_id]
        assert sig.signal_ts not in _TRAIN_ENTRIES_BY_FOLD[sig.fold_id]


def test_capture_on_vs_off_produces_byte_identical_walkforward_result():
    plain_result = run_walkforward(**_run_walkforward_kwargs(_twelve_configs()))

    sink = OosSignalCaptureSink()
    wrapped = wrap_config_specs_for_oos_capture(
        _twelve_configs(), strategy="S1", fold_schedule=_FOLD_SCHEDULE, sink=sink
    )
    captured_result = run_walkforward(**_run_walkforward_kwargs(wrapped))

    assert plain_result == captured_result
    assert _canonical_hash_of_result(plain_result) == _canonical_hash_of_result(
        captured_result
    )
    # explicit canonical payload BYTES equality -- not just the SHA-256
    # digest -- so a hash COLLISION could never mask a byte divergence.
    assert _canonical_json_bytes_of_result(
        plain_result
    ) == _canonical_json_bytes_of_result(captured_result)
    # non-vacuous: the guard must actually have observed something.
    sink.finalize(expected_oos_calls_from_walkforward_result(captured_result))
    assert sink.snapshot()


def test_one_shot_generator_capture_on_vs_off_still_byte_identical():
    """A generator returning a bare (one-shot) generator object for its OOS
    output must never be consumed by the capture wrapper -- if it were, H4
    would see an already-exhausted iterator and silently lose signals."""
    plain_result = run_walkforward(
        **_run_walkforward_kwargs(_twelve_configs(gen_factory=_one_shot_gen_factory))
    )

    sink = OosSignalCaptureSink()
    wrapped = wrap_config_specs_for_oos_capture(
        _twelve_configs(gen_factory=_one_shot_gen_factory),
        strategy="S1",
        fold_schedule=_FOLD_SCHEDULE,
        sink=sink,
    )
    captured_result = run_walkforward(**_run_walkforward_kwargs(wrapped))

    assert plain_result == captured_result
    assert _canonical_hash_of_result(plain_result) == _canonical_hash_of_result(
        captured_result
    )
    assert _canonical_json_bytes_of_result(
        plain_result
    ) == _canonical_json_bytes_of_result(captured_result)
    # the sink must have marked itself invalid (unsupported one-shot shape),
    # never silently consumed the generator to "successfully" capture it.
    assert sink.is_invalid
    assert sink.invalid_reason_code == "unsupported_batch_shape"
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()


def test_wrapped_generator_returns_the_exact_same_object_the_real_generator_returned():
    sentinel_batch = (
        _make_signal(
            "BTCUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
        ),
    )

    def _real_gen(symbol, bars_slice, fold_id):
        return sentinel_batch

    sink = OosSignalCaptureSink()
    config = ConfigSpec(config_id="S1-00", generate_signals=_real_gen)
    (wrapped,) = wrap_config_specs_for_oos_capture(
        (config,), strategy="S1", fold_schedule=_FOLD_SCHEDULE, sink=sink
    )
    oos_bars = tuple(
        b
        for b in _BARS_1M["BTCUSDT"]
        if _FOLD_0.oos_start_ms <= b.ts < _FOLD_0.oos_end_ms
    )
    result = wrapped.generate_signals("BTCUSDT", oos_bars, "fold-00")
    assert result is sentinel_batch


def test_capture_side_failure_never_raises_out_of_the_wrapper_but_latches_invalid():
    """A forged signal (wrong symbol) would make H4's OWN validation raise --
    but the wrapper's OWN internal (mirrored) validation attempt must never
    be the thing that raises out of the wrapper; it must latch the sink
    invalid and still return the real generator's raw result unchanged,
    letting H4 crash the attempt in its own, single, official control-flow
    path. The sink itself must then refuse to be used as evidence."""
    forged = (
        _make_signal(
            "XRPUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
        ),
    )

    def _forging_gen(symbol, bars_slice, fold_id):
        return forged  # symbol requested is "BTCUSDT", forged claims XRPUSDT

    sink = OosSignalCaptureSink()
    config = ConfigSpec(config_id="S1-00", generate_signals=_forging_gen)
    (wrapped,) = wrap_config_specs_for_oos_capture(
        (config,), strategy="S1", fold_schedule=_FOLD_SCHEDULE, sink=sink
    )
    oos_bars = tuple(
        b
        for b in _BARS_1M["BTCUSDT"]
        if _FOLD_0.oos_start_ms <= b.ts < _FOLD_0.oos_end_ms
    )
    result = wrapped.generate_signals("BTCUSDT", oos_bars, "fold-00")
    assert result is forged  # unchanged despite the forged identity
    assert sink.is_invalid
    assert sink.invalid_reason_code == "signal_validation_failed"
    sink.finalize(set())
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()


_CALL = ("S1", "fold-00", "S1-00", "BTCUSDT")


def test_duplicate_canonical_event_key_latches_the_whole_sink_invalid():
    """A duplicate is never partially accepted: the sink must reject the
    ENTIRE offending call atomically and latch invalid, never leaving the
    earlier, valid signal quietly usable while the conflict is ignored."""
    sink = OosSignalCaptureSink()
    sig = _make_signal(
        "BTCUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
    )
    sink.try_extend((sig,))
    assert not sink.is_invalid
    sink.try_extend((sig,))
    assert sink.is_invalid
    assert sink.invalid_reason_code == "duplicate_signal_identity"
    sink.finalize({_CALL})
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()


def test_a_new_signal_in_the_same_batch_as_a_pre_existing_duplicate_is_not_retained():
    """True mid-batch atomicity: a batch containing [brand-new signal,
    signal that duplicates an ALREADY-committed one] must reject the WHOLE
    batch -- the new signal must never sneak into the committed state even
    though it, by itself, was perfectly valid."""
    sink = OosSignalCaptureSink()
    existing = _make_signal(
        "BTCUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
    )
    sink.try_extend((existing,))
    assert not sink.is_invalid

    brand_new = _make_signal(
        "XRPUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
    )
    duplicate_of_existing = _make_signal(
        "BTCUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
    )
    sink.try_extend((brand_new, duplicate_of_existing))
    assert sink.is_invalid

    sink.finalize({_CALL, ("S1", "fold-00", "S1-00", "XRPUSDT")})
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()
    # even bypassing the public snapshot() gate, the internal committed
    # state must never contain the brand-new signal from the rejected batch.
    assert brand_new not in sink._by_key.values()  # noqa: SLF001 -- white-box atomicity proof


def test_duplicate_observed_call_for_the_same_full_identity_latches_invalid():
    sink = OosSignalCaptureSink()
    sink.record_call_observed(
        strategy="S1", fold_id="fold-00", config_id="S1-00", symbol="BTCUSDT"
    )
    assert not sink.is_invalid
    sink.record_call_observed(
        strategy="S1", fold_id="fold-00", config_id="S1-00", symbol="BTCUSDT"
    )
    assert sink.is_invalid
    assert sink.invalid_reason_code == "duplicate_observed_call"


def test_all_expected_calls_observed_with_empty_batches_finalizes_complete_and_empty():
    sink = OosSignalCaptureSink()
    expected = {("S1", "fold-00", "S1-00", s) for s in _SYMBOLS}
    for symbol in _SYMBOLS:
        sink.record_call_observed(
            strategy="S1", fold_id="fold-00", config_id="S1-00", symbol=symbol
        )
        sink.try_extend(())  # legitimately zero signals this call
    sink.finalize(expected)
    assert sink.snapshot() == ()  # complete AND empty -- not invalid


def test_omitting_one_expected_call_is_incomplete_never_silently_zero():
    sink = OosSignalCaptureSink()
    expected = {("S1", "fold-00", "S1-00", s) for s in _SYMBOLS}
    for symbol in _SYMBOLS[:-1]:  # deliberately omit the last symbol's call
        sink.record_call_observed(
            strategy="S1", fold_id="fold-00", config_id="S1-00", symbol=symbol
        )
        sink.try_extend(())
    sink.finalize(expected)
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()


def test_snapshot_before_finalize_fails_closed():
    sink = OosSignalCaptureSink()
    with pytest.raises(CaptureInvalidError):
        sink.snapshot()


def test_snapshot_is_a_deep_immutable_view_mutating_the_input_after_the_call_is_inert():
    sink = OosSignalCaptureSink()
    sig = _make_signal(
        "BTCUSDT", _FOLD_0.oos_start_ms, config_id="S1-00", fold_id="fold-00"
    )
    mutable_batch = [sig]
    sink.record_call_observed(
        strategy="S1", fold_id="fold-00", config_id="S1-00", symbol="BTCUSDT"
    )
    sink.try_extend(mutable_batch)
    mutable_batch.append(
        _make_signal(
            "BTCUSDT",
            _FOLD_0.oos_start_ms + 60_000,
            config_id="S1-00",
            fold_id="fold-00",
        )
    )
    mutable_batch.clear()
    sink.finalize({_CALL})
    snapshot = sink.snapshot()
    assert snapshot == (
        sig,
    )  # unaffected by the caller mutating its own list afterward
