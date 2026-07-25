"""ROB-942 (H2, ROB-940) — complete-only 1m -> 5m/15m aggregation RED fixtures.

Pins AC2: only fully-covered UTC buckets are emitted (open=first open,
high/low=max/min, close=last close, volume=sum); any gap in the source 1m grid
must NOT bridge a bucket, and must reset warm-up (``is_segment_start``) on the
next bucket that completes after the gap.
"""

from rob940_bars_agg import Bar1m, aggregate_complete

MIN = 60_000


def _bars(start_ts, closes, *, opens=None, highs=None, lows=None, volumes=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    volumes = volumes or [10.0] * n
    return [
        Bar1m(
            ts=start_ts + i * MIN,
            open=opens[i],
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=volumes[i],
        )
        for i in range(n)
    ]


def test_aggregate_5m_complete_bucket_ohlcv():
    bars = _bars(
        0,
        [10, 11, 12, 9, 13],
        opens=[10, 11, 12, 9, 13],
        highs=[10.5, 11.5, 12.5, 9.5, 13.5],
        lows=[9.9, 10.9, 8.0, 8.9, 12.9],
        volumes=[1.0, 2.0, 3.0, 4.0, 5.0],
    )
    out = aggregate_complete(bars, bucket_minutes=5)
    assert len(out) == 1
    b = out[0]
    assert b.ts == 0
    assert b.close_ts == 5 * MIN
    assert b.open == 10  # first 1m open
    assert b.high == 13.5  # max of highs
    assert b.low == 8.0  # min of lows
    assert b.close == 13  # last 1m close
    assert b.volume == 15.0  # sum of volumes
    assert b.is_segment_start is True


def test_aggregate_15m_complete_bucket_ohlcv():
    bars = _bars(0, list(range(15)))
    out = aggregate_complete(bars, bucket_minutes=15)
    assert len(out) == 1
    b = out[0]
    assert b.ts == 0
    assert b.close_ts == 15 * MIN
    assert b.open == bars[0].open
    assert b.close == bars[-1].close
    assert b.volume == sum(x.volume for x in bars)


def test_incomplete_bucket_not_emitted_missing_middle_bar():
    bars = _bars(0, [1, 2, 3, 4, 5])
    del bars[2]  # drop the middle 1m bar -> bucket can never complete
    out = aggregate_complete(bars, bucket_minutes=5)
    assert out == []


def test_leading_partial_bucket_at_series_start_not_emitted():
    # data starts at minute offset 2 of a 5m bucket [0,5) -> that bucket can
    # never see offsets 0/1 and must never be emitted.
    bars = _bars(2 * MIN, [1, 2, 3])
    out = aggregate_complete(bars, bucket_minutes=5)
    assert out == []


def test_trailing_partial_bucket_at_series_end_not_emitted():
    bars = _bars(0, [1, 2, 3, 4, 5, 6, 7])  # 7 bars: one full 5m bucket + 2 leftover
    out = aggregate_complete(bars, bucket_minutes=5)
    assert len(out) == 1
    assert out[0].ts == 0


def test_gap_between_two_5m_buckets_does_not_bridge_and_resets_segment():
    first = _bars(0, [1, 2, 3, 4, 5])  # bucket [0, 5*MIN)
    # gap: skip minutes 5..9, resume at minute 10 with a complete bucket [10*MIN,15*MIN)
    second = _bars(10 * MIN, [10, 11, 12, 13, 14])
    out = aggregate_complete(first + second, bucket_minutes=5)
    assert [b.ts for b in out] == [0, 10 * MIN]
    assert out[0].is_segment_start is True
    assert out[1].is_segment_start is True  # first bucket after the gap resets warm-up


def test_contiguous_second_bucket_in_same_segment_is_not_a_segment_start():
    bars = _bars(0, list(range(10)))  # two back-to-back complete 5m buckets, no gap
    out = aggregate_complete(bars, bucket_minutes=5)
    assert [b.ts for b in out] == [0, 5 * MIN]
    assert out[0].is_segment_start is True
    assert out[1].is_segment_start is False


def test_unsorted_or_duplicate_timestamps_raise():
    bars = _bars(0, [1, 2, 3])
    bad = [bars[0], bars[0], bars[2]]
    try:
        aggregate_complete(bad, bucket_minutes=5)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_empty_input_returns_empty():
    assert aggregate_complete([], bucket_minutes=5) == []


def test_non_positive_bucket_minutes_raises():
    bars = _bars(0, [1, 2, 3])
    try:
        aggregate_complete(bars, bucket_minutes=0)
        raised = False
    except ValueError:
        raised = True
    assert raised
