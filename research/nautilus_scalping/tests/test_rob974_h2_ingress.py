"""ROB-979 CP1 -- narrow H1 semantic ingress + exact-tick entry resolver (RED first).

The ingress normalizer accepts DUCK-TYPED H1-shaped records (plain namedtuples
here, standing in for the real ROB-978 output post-merge) via attribute
access only -- never ``isinstance`` against a concrete H1 class -- and returns
the H2-owned immutable DTOs from ``rob974_h2_dtos``. Covers ROB-979 AC3
(exact contiguous next-minute entry / ``next_tick_unavailable``) and the
"H2 core is not coupled to H1 concrete class identity" CP6 requirement.
"""

from __future__ import annotations

from collections import namedtuple

import pytest
from rob974_h2_dtos import MinuteBar, S3CloseFeature, S4PairLegClose
from rob974_h2_ingress import (
    build_minute_index,
    normalize_minute_bar,
    normalize_s3_close_feature,
    normalize_s4_pair_leg_close,
    resolve_entry_minute,
)

_RawMinute = namedtuple("_RawMinute", "symbol open_time open high low close")
_RawS3Close = namedtuple("_RawS3Close", "symbol close_ts close VWAP24 M")
_RawPairClose = namedtuple("_RawPairClose", "symbol close_ts close")


class TestNormalizeMinuteBar:
    def test_duck_typed_attribute_access_only(self):
        raw = _RawMinute("XRPUSDT", 1_000_000, 1.0, 1.1, 0.9, 1.05)
        bar = normalize_minute_bar(raw)
        assert isinstance(bar, MinuteBar)
        assert bar.open_time == 1_000_000

    def test_does_not_isinstance_check_a_concrete_h1_class(self):
        # A completely unrelated object exposing the same attributes must
        # normalize identically -- ingress must never do `isinstance(raw, H1Bar)`.
        class _ArbitraryShape:
            symbol = "DOGEUSDT"
            open_time = 2_000_000
            open = 0.5
            high = 0.55
            low = 0.45
            close = 0.52

        bar = normalize_minute_bar(_ArbitraryShape())
        assert bar.symbol == "DOGEUSDT"


class TestNormalizeS3CloseFeature:
    def test_maps_uppercase_vwap24_and_m(self):
        raw = _RawS3Close("SOLUSDT", 3_000_000, 100.0, 99.0, 0.0081)
        feat = normalize_s3_close_feature(raw)
        assert isinstance(feat, S3CloseFeature)
        assert feat.vwap24 == 99.0
        assert feat.m == 0.0081


class TestNormalizeS4PairLegClose:
    def test_basic(self):
        raw = _RawPairClose("XRPUSDT", 3_000_000, 1.2)
        leg = normalize_s4_pair_leg_close(raw)
        assert isinstance(leg, S4PairLegClose)
        assert leg.close == 1.2


class TestResolveEntryMinute:
    def test_exact_contiguous_tick_resolves(self):
        bars = [
            MinuteBar("XRPUSDT", 1_000_000, 1.0, 1.0, 1.0, 1.0),
            MinuteBar("XRPUSDT", 1_000_060_000, 1.01, 1.01, 1.01, 1.01),
        ]
        index = build_minute_index(bars)
        resolved = resolve_entry_minute(index, "XRPUSDT", 1_000_060_000)
        assert resolved is not None
        assert resolved.open == 1.01

    def test_missing_exact_tick_returns_none_never_scans_ahead(self):
        bars = [
            MinuteBar("XRPUSDT", 1_000_000, 1.0, 1.0, 1.0, 1.0),
            # gap at 1_000_060_000
            MinuteBar("XRPUSDT", 1_000_120_000, 1.02, 1.02, 1.02, 1.02),
        ]
        index = build_minute_index(bars)
        resolved = resolve_entry_minute(index, "XRPUSDT", 1_000_060_000)
        assert resolved is None  # must NOT fall through to the 1_000_120_000 bar

    def test_missing_symbol_returns_none(self):
        bars = [MinuteBar("XRPUSDT", 1_000_000, 1.0, 1.0, 1.0, 1.0)]
        index = build_minute_index(bars)
        assert resolve_entry_minute(index, "DOGEUSDT", 1_000_000) is None

    def test_index_is_keyed_by_symbol_then_open_time_collision_free(self):
        bars = [
            MinuteBar("XRPUSDT", 1_000_000, 1.0, 1.0, 1.0, 1.0),
            MinuteBar("DOGEUSDT", 1_000_000, 0.5, 0.5, 0.5, 0.5),
        ]
        index = build_minute_index(bars)
        assert resolve_entry_minute(index, "XRPUSDT", 1_000_000).close == 1.0
        assert resolve_entry_minute(index, "DOGEUSDT", 1_000_000).close == 0.5

    def test_duplicate_open_time_same_symbol_is_rejected(self):
        bars = [
            MinuteBar("XRPUSDT", 1_000_000, 1.0, 1.0, 1.0, 1.0),
            MinuteBar("XRPUSDT", 1_000_000, 1.01, 1.01, 1.01, 1.01),
        ]
        with pytest.raises(ValueError):
            build_minute_index(bars)
