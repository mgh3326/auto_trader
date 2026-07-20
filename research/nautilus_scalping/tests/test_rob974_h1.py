import math

import pytest

from rob974_features import Bar4h, MinuteBar, build_complete_4h, compute_common_features, symbol_features, vwap12, vwap24


MIN = 60_000


def rows(n, start=0):
    return [MinuteBar(start + i * MIN, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 1.0) for i in range(n)]


def test_cp1_complete_only_utc_ohlcv_and_terminal_invalidity():
    out = build_complete_4h(rows(241))
    assert len(out) == 1
    bar = out[0]
    assert (bar.ts, bar.close_ts, bar.open, bar.high, bar.low, bar.close, bar.volume) == (0, 240 * MIN, 1.0, 241.0, 0.5, 240.5, 240.0)
    assert build_complete_4h(rows(239)) == ()
    with pytest.raises(ValueError):
        build_complete_4h([rows(1)[0], rows(1)[0]])
    with pytest.raises(TypeError):
        MinuteBar(True, 1.0, 1.0, 1.0, 1.0, 1.0)


def test_cp2_exact_vwap_windows_and_future_isolation():
    source = rows(1441)
    before = vwap12(source, 1440 * MIN)
    assert before == vwap12(source[:-1], 1440 * MIN)
    assert vwap12(source, 719 * MIN) is None
    assert vwap24(source, 1440 * MIN) is not None
    assert vwap24(rows(1439), 1440 * MIN) is None
    zeros = [MinuteBar(x.ts, x.open, x.high, x.low, x.close, 0.0) for x in source]
    assert vwap12(zeros, 1440 * MIN) is None


def test_cp3_atr_seed_wilder_and_synchronized_common_order():
    bars = tuple(Bar4h(i * 240 * MIN, (i + 1) * 240 * MIN, 10.0 + i, 12.0 + i, 9.0 + i, 11.0 + i, 1.0, i == 0) for i in range(21))
    features = symbol_features("XRPUSDT", (), bars)
    assert features[20].tr == 3.0
    assert features[20].atr20 == 3.0
    assert features[20].a == 3.0 / 31.0
    minute_rows = {symbol: rows(7 * 240) for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")}
    snapshots = compute_common_features(minute_rows)
    assert snapshots[-1].features[0].symbol == "XRPUSDT"
    assert snapshots[-1].m == math.log(1680.5 / 1440.5)
