"""ROB-1059 H1 (spec §14.3/§11.2) — UTC calendar-day validity + aggregation.

Boundary cases required by the ROB-1059 acceptance contract (AC9-AC13, AC22):
minute_count 1432/1433/1440; 2-minute vs 3-minute gap; gap inside the last 60
minutes before decision; duplicate/reversed rows as terminal errors; partial
(in-progress) day never emitted; PIT snapshot immutability under future
mutation; exact int/float-zero types.
"""

import math
import unittest.mock

import daily_bars as db
import pytest

MIN_MS = 60_000
DAY_MS = 1440 * MIN_MS
DAY0 = 1_719_878_400_000  # 2024-07-02T00:00:00Z (arbitrary UTC-midnight anchor)


def _row(open_time_ms, close=100.0, o=None, h=None, low=None, v=1.0):
    o = o if o is not None else close
    h = h if h is not None else close
    low = low if low is not None else close
    return db.SpotMinute(open_time_ms, o, h, low, close, v)


def _full_day_rows(day_start=DAY0, skip: set[int] = frozenset(), close_fn=None):
    rows = []
    for m in range(1440):
        if m in skip:
            continue
        ts = day_start + m * MIN_MS
        close = close_fn(m) if close_fn else 100.0 + m * 0.001
        rows.append(_row(ts, close=close))
    return rows


# --------------------------------------------------------------------------- #
# minute_count boundary: 1432 / 1433 / 1440
# --------------------------------------------------------------------------- #
def test_minute_count_1440_full_day_is_valid():
    rows = _full_day_rows()
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.is_valid is True
    assert bar.minute_count_observed == 1440
    assert bar.imputed_minutes == 0


def test_minute_count_1433_boundary_is_valid_with_scattered_small_gaps():
    # 7 missing minutes as scattered isolated single-minute gaps (never a
    # consecutive run > 2, never inside the last 60 minutes) -> valid.
    skip = {10, 200, 400, 600, 800, 1000, 1200}
    assert len(skip) == 7
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.minute_count_observed == 1433
    assert bar.max_gap_minutes == 1
    assert bar.gap_in_last_60min is False
    assert bar.is_valid is True
    assert bar.imputed_minutes == 7


def test_minute_count_1432_boundary_is_invalid():
    # one extra missing minute beyond the 1433 threshold -> invalid, even
    # though every individual gap is still only 1 minute wide.
    skip = {10, 200, 400, 600, 800, 1000, 1200, 1250}
    assert len(skip) == 8
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.minute_count_observed == 1432
    assert bar.is_valid is False


# --------------------------------------------------------------------------- #
# gap-run boundary: 2-minute gap imputed & valid vs 3-minute gap invalid
# --------------------------------------------------------------------------- #
def test_two_minute_gap_is_imputed_and_day_stays_valid():
    skip = {500, 501}
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.max_gap_minutes == 2
    assert bar.is_valid is True
    assert bar.imputed_minutes == 2
    assert bar.minute_count_observed == 1438


def test_three_minute_gap_is_never_imputed_and_day_is_invalid():
    skip = {500, 501, 502}
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.max_gap_minutes == 3
    assert bar.is_valid is False


# --------------------------------------------------------------------------- #
# gap inside the 60 minutes preceding the decision (end of day) -> invalid
# regardless of gap width or overall minute_count
# --------------------------------------------------------------------------- #
def test_gap_in_final_60_minutes_invalidates_day_even_if_otherwise_pristine():
    # minute 1439 (the very last minute of the day) missing: only 1 minute
    # missing overall (minute_count=1439 >= 1433, gap width 1 <= 2) yet the
    # decision-freshness rule must still fail this day closed.
    skip = {1439}
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.minute_count_observed == 1439
    assert bar.max_gap_minutes == 1
    assert bar.gap_in_last_60min is True
    assert bar.is_valid is False


def test_gap_just_before_final_60_minutes_boundary_does_not_invalidate():
    # minute 1379 is the last minute BEFORE the final-60-minute window
    # (1380..1439); missing it alone must not trip gap_in_last_60min.
    skip = {1379}
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert bar.gap_in_last_60min is False
    assert bar.is_valid is True


# --------------------------------------------------------------------------- #
# duplicate / reversed rows: terminal, never deduped/resorted
# --------------------------------------------------------------------------- #
def test_duplicate_open_time_is_terminal():
    rows = [_row(DAY0), _row(DAY0)]
    with pytest.raises(db.DuplicateOpenTimeError):
        db.ingest_minute_sequence(rows)


def test_byte_identical_duplicate_open_time_is_still_terminal():
    # even a content-identical duplicate must NOT be silently deduped (unlike
    # rob941_kline_schema.parse_kline_csv's dedupe semantics) -- ROB-1059's
    # contract is stricter: any duplicate open_time is terminal.
    row = _row(DAY0)
    with pytest.raises(db.DuplicateOpenTimeError):
        db.ingest_minute_sequence([row, row])


def test_reversed_row_is_terminal():
    rows = [_row(DAY0 + MIN_MS), _row(DAY0)]
    with pytest.raises(db.ReversedRowError):
        db.ingest_minute_sequence(rows)


def test_ascending_rows_pass_through_unchanged():
    rows = [_row(DAY0), _row(DAY0 + MIN_MS), _row(DAY0 + 2 * MIN_MS)]
    out = db.ingest_minute_sequence(rows)
    assert out == tuple(rows)


# --------------------------------------------------------------------------- #
# leading-gap-with-no-baseline: fail closed to invalid, not a crash
# --------------------------------------------------------------------------- #
def test_row_exactly_at_day_end_ms_is_rejected_half_open_exclusive():
    # AC13 remediation: build_utc_day's window check is
    # `day_start_ms <= open_time_ms < day_end_ms` -- a row exactly AT
    # day_end_ms belongs to the NEXT day and must be rejected (half-open
    # exclusive upper bound). A mutation of `<` to `<=` would silently admit
    # it. This isolates EXACTLY one extra row at the boundary (no other
    # out-of-range rows) so a `<=` mutation is NOT masked by some other row
    # still tripping the check for an unrelated reason.
    day0_rows = _full_day_rows(DAY0)
    boundary_row = _row(DAY0 + DAY_MS)  # exactly day_end_ms
    with pytest.raises(ValueError, match="outside declared UTC day window"):
        db.build_utc_day(
            DAY0, day0_rows + [boundary_row], prior_close=99.0, is_segment_start=True
        )


def test_leading_gap_with_no_prior_close_is_invalid_not_a_crash():
    skip = {0}
    rows = _full_day_rows(skip=skip)
    bar = db.build_utc_day(DAY0, rows, prior_close=None, is_segment_start=True)
    assert bar.is_valid is False


# --------------------------------------------------------------------------- #
# OHLCV aggregation: first open / max high / min low / last close / fsum(vol)
# --------------------------------------------------------------------------- #
def test_ohlcv_aggregation_uses_first_open_max_high_min_low_last_close_fsum_volume():
    rows = [
        db.SpotMinute(DAY0, 10.0, 12.0, 9.0, 11.0, 1.0),
        db.SpotMinute(DAY0 + MIN_MS, 11.0, 15.0, 10.5, 14.0, 2.0),
        db.SpotMinute(DAY0 + 2 * MIN_MS, 14.0, 14.5, 8.0, 9.0, 3.0),
    ]
    # pad the rest of the day so the day is valid (skip everything else -> way
    # too many gaps) -- use a VARYING-close full day and only replace the
    # first 3 rows. A constant close_fn (e.g. lambda m: 11.0) would make
    # `bar.close == full[-1].close` vacuously true regardless of whether the
    # aggregation actually picks the LAST close vs. any other row's close --
    # every row would share the same value. Varying closes make "last close"
    # a genuine, falsifiable claim.
    full = _full_day_rows(close_fn=lambda m: 12.0 + m * 0.0001)  # stays inside (8, 15)
    full[0:3] = rows
    bar = db.build_utc_day(DAY0, full, prior_close=9.5, is_segment_start=True)
    assert bar.open == 10.0
    assert bar.high == 15.0
    assert bar.low == 8.0
    assert bar.close == full[-1].close
    assert bar.close == pytest.approx(12.0 + 1439 * 0.0001)  # explicit expected value
    assert bar.is_valid is True
    # non-zero, non-trivial total volume: rows[0:3] contribute 1.0+2.0+3.0=6.0,
    # the remaining 1437 padding rows each contribute 1.0 (see `_row`'s
    # default v=1.0) -- a "volume always constant 0.0" mutation would be
    # caught here (this assertion was previously absent from this test).
    assert bar.volume == pytest.approx(6.0 + 1437 * 1.0)


def test_volume_aggregation_actually_calls_math_fsum():
    # AC11 remediation: on this Python version (3.12+), CPython's builtin
    # sum() is ITSELF precision-compensated for float sequences, so a
    # `math.fsum(...) -> sum(...)` mutation is no longer reliably detectable
    # via numeric precision differences alone (a classic large/small-value
    # cancellation input that distinguished the two on older Pythons produces
    # an IDENTICAL result here). Assert the WIRING directly instead: spy on
    # daily_bars.math.fsum (wrapping the real implementation, so behavior is
    # unchanged) and confirm it is actually invoked -- a mutation swapping in
    # `sum()` would never touch this patched name, so `spy.called` would be
    # False.
    rows = _full_day_rows(close_fn=lambda m: 100.0)
    with unittest.mock.patch("daily_bars.math.fsum", wraps=math.fsum) as fsum_spy:
        bar = db.build_utc_day(DAY0, rows, prior_close=99.0, is_segment_start=True)
    assert fsum_spy.called
    assert bar.volume == pytest.approx(1440.0)


# --------------------------------------------------------------------------- #
# S6 remediation: DailyBar economics (OHLC invariant, non-negative volume) --
# type/finiteness checks alone previously let DailyBar(high=1.0, low=99.0,
# volume=-5.0) construct fine.
# --------------------------------------------------------------------------- #
def _valid_daily_bar_kwargs() -> dict:
    bar = db.build_utc_day(
        DAY0, _full_day_rows(), prior_close=99.0, is_segment_start=True
    )
    return {
        "day_start_ms": bar.day_start_ms,
        "day_end_ms": bar.day_end_ms,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "minute_count_observed": bar.minute_count_observed,
        "imputed_minutes": bar.imputed_minutes,
        "max_gap_minutes": bar.max_gap_minutes,
        "gap_in_last_60min": bar.gap_in_last_60min,
        "is_valid": bar.is_valid,
        "is_segment_start": bar.is_segment_start,
    }


def test_daily_bar_rejects_negative_volume():
    kwargs = _valid_daily_bar_kwargs()
    kwargs["volume"] = -5.0
    with pytest.raises(ValueError):
        db.DailyBar(**kwargs)


def test_daily_bar_rejects_non_positive_ohlc():
    kwargs = _valid_daily_bar_kwargs()
    kwargs["low"] = -1.0
    with pytest.raises(ValueError):
        db.DailyBar(**kwargs)


def test_daily_bar_rejects_high_below_low():
    kwargs = _valid_daily_bar_kwargs()
    kwargs["high"] = 1.0
    kwargs["low"] = 99.0
    with pytest.raises(ValueError):
        db.DailyBar(**kwargs)


def test_daily_bar_rejects_high_below_open_or_close():
    kwargs = _valid_daily_bar_kwargs()
    kwargs["open"] = 1000.0  # higher than `high`
    with pytest.raises(ValueError):
        db.DailyBar(**kwargs)


# --------------------------------------------------------------------------- #
# partial (in-progress) day is never emitted from a daily series
# --------------------------------------------------------------------------- #
def test_partial_day_is_never_emitted_in_series():
    # window ends mid-second-day: the second day's rows only cover the first
    # 100 minutes -- build_daily_series must emit exactly one DailyBar (day 0),
    # never a partial day 1.
    day0_rows = _full_day_rows(DAY0)
    day1_partial = [
        _row(DAY0 + DAY_MS + m * MIN_MS, close=200.0 + m) for m in range(100)
    ]
    series = db.build_daily_series(
        day0_rows + day1_partial,
        window_start_ms=DAY0,
        window_end_ms=DAY0 + DAY_MS + 100 * MIN_MS,
    )
    assert len(series) == 1
    assert series[0].day_start_ms == DAY0


def test_full_two_day_window_emits_two_bars_with_segment_continuity():
    day0_rows = _full_day_rows(DAY0)
    day1_rows = _full_day_rows(DAY0 + DAY_MS, close_fn=lambda m: 200.0 + m * 0.001)
    series = db.build_daily_series(
        day0_rows + day1_rows,
        window_start_ms=DAY0,
        window_end_ms=DAY0 + 2 * DAY_MS,
    )
    assert len(series) == 2
    assert series[0].is_segment_start is True
    assert series[1].is_segment_start is False  # contiguous valid predecessor


def test_build_daily_series_rejects_reversed_rows_terminal_not_silently_sorted():
    # S3 remediation: build_daily_series used to call ``day_rows.sort(...)``
    # per day AFTER splitting by day, which silently repaired a reversed
    # input instead of raising -- exactly what ingest_minute_sequence's
    # ReversedRowError exists to make terminal (AC2). Feeding a fully
    # descending sequence must raise, never quietly produce a valid series.
    day0_rows = _full_day_rows(DAY0)
    reversed_rows = list(reversed(day0_rows))
    with pytest.raises(db.ReversedRowError):
        db.build_daily_series(
            reversed_rows, window_start_ms=DAY0, window_end_ms=DAY0 + DAY_MS
        )


def test_build_daily_series_rejects_duplicate_open_time_terminal():
    day0_rows = _full_day_rows(DAY0)
    # duplicate the LAST row (adjacent-equal, not reversed) so the duplicate
    # check -- not the reversed-row check -- is the one that fires.
    duped_rows = day0_rows + [day0_rows[-1]]
    with pytest.raises(db.DuplicateOpenTimeError):
        db.build_daily_series(
            duped_rows, window_start_ms=DAY0, window_end_ms=DAY0 + DAY_MS
        )


def test_absent_day_resets_prior_close_not_leaked_two_days_stale():
    # S7 remediation: an entirely absent day must reset prior_close to None,
    # not just prior_day_present. Otherwise the NEXT present day's leading
    # gap gets imputed from a close that is two (or more) full UTC days stale.
    day0_rows = _full_day_rows(DAY0, close_fn=lambda m: 100.0)
    # day1 entirely absent (no rows at all)
    day2_rows = _full_day_rows(
        DAY0 + 2 * DAY_MS, skip={0}, close_fn=lambda m: 300.0 + m * 0.001
    )
    series = db.build_daily_series(
        day0_rows + day2_rows,
        window_start_ms=DAY0,
        window_end_ms=DAY0 + 3 * DAY_MS,
    )
    assert [bar.day_start_ms for bar in series] == [DAY0, DAY0 + 2 * DAY_MS]
    day0_bar, day2_bar = series
    assert day0_bar.is_valid is True
    # day2's leading minute is missing and day1 (its would-be baseline) was
    # entirely absent -- prior_close must NOT have leaked across the absent
    # day from day0's close two days earlier; day2 must fail closed instead.
    assert day2_bar.is_valid is False


def test_invalid_predecessor_day_does_not_leak_its_close_as_next_days_baseline():
    # S7 remediation: an INVALID day's aggregated close (partial-coverage
    # aggregate) must also not be forward-filled into the next day's leading
    # gap imputation.
    skip = {500, 501, 502}  # 3-minute gap, not touching the final 60 minutes
    day0_rows = _full_day_rows(DAY0, skip=skip, close_fn=lambda m: 100.0 + m * 0.001)
    day1_rows = _full_day_rows(
        DAY0 + DAY_MS, skip={0}, close_fn=lambda m: 200.0 + m * 0.001
    )
    series = db.build_daily_series(
        day0_rows + day1_rows,
        window_start_ms=DAY0,
        window_end_ms=DAY0 + 2 * DAY_MS,
    )
    day0_bar, day1_bar = series
    assert day0_bar.is_valid is False
    # day1's leading gap has no trustworthy baseline (day0 was invalid) --
    # must fail closed, not silently impute from day0's aggregate close.
    assert day1_bar.is_valid is False


def test_missing_day_starts_a_new_segment():
    day0_rows = _full_day_rows(DAY0)
    # day1 entirely missing (no rows at all); day2 present.
    day2_rows = _full_day_rows(DAY0 + 2 * DAY_MS, close_fn=lambda m: 300.0 + m * 0.001)
    series = db.build_daily_series(
        day0_rows + day2_rows,
        window_start_ms=DAY0,
        window_end_ms=DAY0 + 3 * DAY_MS,
    )
    assert [bar.day_start_ms for bar in series] == [DAY0, DAY0 + 2 * DAY_MS]
    assert series[0].is_segment_start is True
    assert series[1].is_segment_start is True  # day1 gap breaks the segment


# --------------------------------------------------------------------------- #
# PIT immutability: mutating rows at/after decision time t must not change the
# already-closed day's bytes/hash.
# --------------------------------------------------------------------------- #
def test_snapshot_at_t_is_unaffected_by_mutating_rows_at_or_after_t():
    day0_rows = _full_day_rows(DAY0)
    t = DAY0 + DAY_MS  # decision time immediately after day0 closes
    future_rows_v1 = [_row(t + m * MIN_MS, close=500.0) for m in range(10)]
    future_rows_v2 = [_row(t + m * MIN_MS, close=999.0) for m in range(10)]

    bar_v1 = db.build_utc_day(DAY0, day0_rows, prior_close=None, is_segment_start=True)
    bar_v2 = db.build_utc_day(DAY0, day0_rows, prior_close=None, is_segment_start=True)
    assert bar_v1 == bar_v2  # rebuilding from the identical PIT slice is stable

    # constructing the future rows at all must not be reachable from build_utc_day
    # for day0 -- prove it structurally: build_utc_day only ever consumes rows
    # inside [DAY0, DAY0+DAY_MS), so passing the mutated future rows in addition
    # would raise (out-of-window), never silently blend into day0's bar.
    # match= narrows this to the SPECIFIC window-check failure -- the future
    # rows are otherwise well-ordered/non-duplicate, so a bare
    # `pytest.raises(ValueError)` could also silently accept a totally
    # different bug that happened to raise DuplicateOpenTimeError/
    # ReversedRowError (both ValueError subclasses) instead.
    with pytest.raises(ValueError, match="outside declared UTC day window"):
        db.build_utc_day(
            DAY0, day0_rows + future_rows_v1, prior_close=None, is_segment_start=True
        )
    with pytest.raises(ValueError, match="outside declared UTC day window"):
        db.build_utc_day(
            DAY0, day0_rows + future_rows_v2, prior_close=None, is_segment_start=True
        )


# --------------------------------------------------------------------------- #
# exact int / float type discipline
# --------------------------------------------------------------------------- #
def test_spot_minute_rejects_bool_for_open_time_ms():
    with pytest.raises(TypeError):
        db.SpotMinute(True, 1.0, 1.0, 1.0, 1.0, 1.0)


def test_spot_minute_rejects_non_finite_float():
    with pytest.raises(ValueError):
        db.SpotMinute(DAY0, math.nan, 1.0, 1.0, 1.0, 1.0)


def test_spot_minute_rejects_negative_volume():
    with pytest.raises(ValueError):
        db.SpotMinute(DAY0, 1.0, 1.0, 1.0, 1.0, -1.0)


def test_spot_minute_rejects_invalid_ohlc_invariant():
    with pytest.raises(ValueError):
        db.SpotMinute(DAY0, 10.0, 5.0, 1.0, 8.0, 1.0)  # high < open


def test_empty_volume_sum_is_exact_float_zero_when_day_is_all_imputed_but_still_invalid():
    # a day with zero observed rows (all gaps) is invalid and its volume must
    # still be an exact built-in float 0.0, never int 0 or non-finite.
    bar = db.build_utc_day(DAY0, [], prior_close=None, is_segment_start=True)
    assert bar.is_valid is False
    assert type(bar.volume) is float
    assert bar.volume == 0.0
