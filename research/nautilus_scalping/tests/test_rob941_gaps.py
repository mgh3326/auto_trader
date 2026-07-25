"""ROB-941 (AC7) — gap tracking: never delete/forward-fill/synthesize, just record.

``detect_gap_ranges`` returns the missing 60s buckets as explicit ranges (never
silently dropped). ``incomplete_buckets`` marks any 5m/15m aggregate that a gap
touches as incomplete (never synthesized from partial coverage). ``position_touches_gap``
is the pure predicate a trial-rejection rule (``rejected:data_gap_in_position``)
consumes — this module does not itself reject trials, only exposes the predicate.
"""

import rob941_gaps as gaps

GRID_MS = 60_000
T0 = 1751328000000  # 2025-07-01T00:00:00Z


def _times(*minute_offsets):
    return [T0 + m * GRID_MS for m in minute_offsets]


def test_detect_gap_ranges_no_gap_when_fully_contiguous():
    observed = _times(0, 1, 2, 3, 4)
    out = gaps.detect_gap_ranges(observed, T0, T0 + 5 * GRID_MS)
    assert out == []


def test_detect_gap_ranges_single_missing_minute():
    observed = _times(0, 1, 3, 4)  # minute 2 missing
    out = gaps.detect_gap_ranges(observed, T0, T0 + 5 * GRID_MS)
    assert out == [(T0 + 2 * GRID_MS, T0 + 3 * GRID_MS)]


def test_detect_gap_ranges_merges_contiguous_missing_minutes():
    observed = _times(0, 4)  # minutes 1,2,3 missing -> one merged range
    out = gaps.detect_gap_ranges(observed, T0, T0 + 5 * GRID_MS)
    assert out == [(T0 + 1 * GRID_MS, T0 + 4 * GRID_MS)]


def test_detect_gap_ranges_covers_entire_window_when_no_data():
    out = gaps.detect_gap_ranges([], T0, T0 + 3 * GRID_MS)
    assert out == [(T0, T0 + 3 * GRID_MS)]


def test_detect_gap_ranges_never_synthesizes_a_row_just_reports_the_hole():
    # regression: the function must not attempt to interpolate/ffill anything —
    # its only output is the (start, end) range, never a fabricated bar value.
    observed = _times(0, 2)
    out = gaps.detect_gap_ranges(observed, T0, T0 + 3 * GRID_MS)
    assert out == [(T0 + GRID_MS, T0 + 2 * GRID_MS)]
    assert all(isinstance(r, tuple) and len(r) == 2 for r in out)


# --------------------------------------------------------------------------- #
# 5m / 15m bucket completeness (a gap anywhere in the bucket -> incomplete)
# --------------------------------------------------------------------------- #
def test_incomplete_buckets_5m_all_present_is_complete():
    observed = _times(0, 1, 2, 3, 4)
    out = gaps.incomplete_buckets(observed, T0, T0 + 5 * GRID_MS, bucket_seconds=300)
    assert out == []


def test_incomplete_buckets_5m_missing_one_minute_marks_whole_bucket_incomplete():
    observed = _times(0, 1, 2, 3)  # minute 4 missing
    out = gaps.incomplete_buckets(observed, T0, T0 + 5 * GRID_MS, bucket_seconds=300)
    assert out == [T0]


def test_incomplete_buckets_15m_boundary_crossing_gap():
    # 15 one-minute bars expected; minute 7 missing -> the whole 15m bucket incomplete
    offsets = [m for m in range(15) if m != 7]
    observed = _times(*offsets)
    out = gaps.incomplete_buckets(observed, T0, T0 + 15 * GRID_MS, bucket_seconds=900)
    assert out == [T0]


def test_incomplete_buckets_only_flags_the_bucket_touching_the_gap():
    # two 5m buckets; first complete, second missing one minute
    offsets = [0, 1, 2, 3, 4, 5, 6, 7, 8]  # minute 9 (bucket 2) missing
    observed = _times(*offsets)
    out = gaps.incomplete_buckets(observed, T0, T0 + 10 * GRID_MS, bucket_seconds=300)
    assert out == [T0 + 5 * GRID_MS]


# --------------------------------------------------------------------------- #
# position-vs-gap predicate (AC7: trial touching a gap is rejected upstream)
# --------------------------------------------------------------------------- #
def test_position_touches_gap_true_when_overlapping():
    gap_ranges = [(T0 + 2 * GRID_MS, T0 + 4 * GRID_MS)]
    assert gaps.position_touches_gap(T0, T0 + 3 * GRID_MS, gap_ranges) is True


def test_position_touches_gap_false_when_disjoint():
    gap_ranges = [(T0 + 2 * GRID_MS, T0 + 4 * GRID_MS)]
    assert gaps.position_touches_gap(T0, T0 + 2 * GRID_MS, gap_ranges) is False
    assert (
        gaps.position_touches_gap(T0 + 4 * GRID_MS, T0 + 5 * GRID_MS, gap_ranges)
        is False
    )


def test_position_touches_gap_false_when_no_gaps():
    assert gaps.position_touches_gap(T0, T0 + GRID_MS, []) is False
