import pytest
from rob974_features import MinuteBar
from rob974_h4_runner import (
    ExactMinuteEntry,
    H4Phase,
    build_actual_h1_phase_context,
    candidate_fits_phase,
    phase_horizon_reason,
    recompute_stateless_phase,
    resolve_exact_entry,
)


def test_horizon_accepts_exact_phase_end_and_rejects_one_ms_overrun() -> None:
    assert candidate_fits_phase(signal_ts=100, max_hold_ms=900, phase_end_ms=1_000)
    assert not candidate_fits_phase(signal_ts=101, max_hold_ms=900, phase_end_ms=1_000)


def test_exact_entry_never_scans_a_later_valid_minute() -> None:
    rows = (
        ExactMinuteEntry(1_001, "missing exact tick sentinel"),
        ExactMinuteEntry(1_000, "a later tuple member must not be scanned"),
    )
    assert resolve_exact_entry(decision_close_ms=1_000, minutes=rows) is None
    assert resolve_exact_entry(
        decision_close_ms=1_000, minutes=(ExactMinuteEntry(1_000, "entry"),)
    ) == ExactMinuteEntry(1_000, "entry")


def test_phases_are_half_open_and_context_is_recomputed_without_carry() -> None:
    phase = H4Phase("selected_oos", 3_000, 4_000)
    assert not phase.contains(2_999)
    assert phase.contains(3_000)
    assert not phase.contains(4_000)
    calls: list[tuple[object, H4Phase]] = []

    def actual_h1(raw: object, incoming: H4Phase) -> str:
        calls.append((raw, incoming))
        return "fresh"

    assert (
        recompute_stateless_phase(
            raw_past_context=("completed",), phase=phase, feature_builder=actual_h1
        )
        == "fresh"
    )
    assert calls == [(("completed",), phase)]
    assert phase_horizon_reason("selected_oos") == "insufficient_oos_exit_horizon"
    with pytest.raises(ValueError):
        phase_horizon_reason("wrong")


def _actual_h1_minutes() -> dict[str, tuple[MinuteBar, ...]]:
    values: dict[str, tuple[MinuteBar, ...]] = {}
    for offset, symbol in enumerate(("XRPUSDT", "DOGEUSDT", "SOLUSDT")):
        values[symbol] = tuple(
            MinuteBar(
                ts=index * 60_000,
                open=100.0 + offset + index * 0.001,
                high=100.1 + offset + index * 0.001,
                low=99.9 + offset + index * 0.001,
                close=100.0 + offset + index * 0.001,
                volume=1.0,
            )
            for index in range(8 * 240)
        )
    return values


def test_actual_h1_phase_context_is_fresh_pit_and_preserves_crossing_bar() -> None:
    # The complete [24h,28h) bar is prior context to phase start 27h, while
    # its close/event at 28h belongs to the half-open phase.
    phase = H4Phase("selected_oos", 27 * 3_600_000, 32 * 3_600_000)
    raw = _actual_h1_minutes()
    first = build_actual_h1_phase_context(raw_minutes=raw, phase=phase)
    second = build_actual_h1_phase_context(raw_minutes=raw, phase=phase)
    assert first == second
    assert first.phase_snapshot_count == 1
    assert first.feature_context.snapshot_at(28 * 3_600_000) is not None
    assert any(
        bar.close_ts == 28 * 3_600_000
        for bar in first.feature_context.bars_for("XRPUSDT")
    )
    future = {
        symbol: rows + (MinuteBar(phase.end_ms, 100.0, 100.1, 99.9, 100.0, 1.0),)
        for symbol, rows in raw.items()
    }
    with pytest.raises(ValueError, match="future minute"):
        build_actual_h1_phase_context(raw_minutes=future, phase=phase)


def test_missing_minute_removes_affected_complete_bar_without_forward_fill() -> None:
    phase = H4Phase("selected_oos", 27 * 3_600_000, 32 * 3_600_000)
    raw = _actual_h1_minutes()
    raw["XRPUSDT"] = tuple(row for row in raw["XRPUSDT"] if row.ts != 25 * 3_600_000)
    built = build_actual_h1_phase_context(raw_minutes=raw, phase=phase)
    assert all(
        bar.close_ts != 28 * 3_600_000
        for bar in built.feature_context.bars_for("XRPUSDT")
    )
    assert built.feature_context.snapshot_at(28 * 3_600_000) is None
