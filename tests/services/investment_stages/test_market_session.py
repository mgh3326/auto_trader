"""ROB-374 B6 — deterministic intraday market-session derivation."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.investment_stages.market_session import derive_market_session

UTC = dt.UTC


def _utc(y, mo, d, h, mi=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.mark.unit
class TestDeriveMarketSession:
    # The ROB-374 live bundle: as_of 2026-05-29 19:39 UTC (ET Fri 15:39) -> regular.
    def test_us_regular_live_bundle_instant(self) -> None:
        assert derive_market_session("us", _utc(2026, 5, 29, 19, 39)) == "regular"

    def test_us_regular_at_open_boundary_inclusive(self) -> None:
        # 13:30 UTC == 09:30 ET open (inclusive).
        assert derive_market_session("us", _utc(2026, 5, 29, 13, 30)) == "regular"

    def test_us_pre_market(self) -> None:
        # 12:00 UTC == 08:00 ET, within [04:00, 09:30) ET.
        assert derive_market_session("us", _utc(2026, 5, 29, 12, 0)) == "pre"

    def test_us_pre_at_early_boundary(self) -> None:
        # 08:00 UTC == 04:00 ET pre-open (inclusive).
        assert derive_market_session("us", _utc(2026, 5, 29, 8, 0)) == "pre"

    def test_us_post_market_at_close_boundary(self) -> None:
        # 20:00 UTC == 16:00 ET regular close -> post begins (exclusive regular end).
        assert derive_market_session("us", _utc(2026, 5, 29, 20, 0)) == "post"

    def test_us_post_market_late(self) -> None:
        # 23:59 UTC == 19:59 ET, within [16:00, 20:00) ET.
        assert derive_market_session("us", _utc(2026, 5, 29, 23, 59)) == "post"

    def test_us_closed_overnight(self) -> None:
        # 04:00 UTC == 00:00 ET, before the 04:00 ET pre-open -> closed.
        assert derive_market_session("us", _utc(2026, 5, 29, 4, 0)) is None

    def test_us_closed_after_post(self) -> None:
        # 2026-05-30 00:00 UTC == 2026-05-29 20:00 ET, the exclusive post end -> closed.
        assert derive_market_session("us", _utc(2026, 5, 30, 0, 0)) is None

    def test_us_weekend_is_none(self) -> None:
        assert derive_market_session("us", _utc(2026, 5, 30, 17, 0)) is None

    def test_us_holiday_is_none(self) -> None:
        # 2026-05-25 is Memorial Day (XNYS closed).
        assert derive_market_session("us", _utc(2026, 5, 25, 17, 0)) is None

    def test_us_half_day_early_close_is_post_not_regular(self) -> None:
        # 2025-11-28 (day after Thanksgiving) closes early at 13:00 ET (18:00 UTC).
        # 18:30 UTC == 13:30 ET must be post, NOT regular — proves half-days honored.
        assert derive_market_session("us", _utc(2025, 11, 28, 18, 30)) == "post"
        # 15:00 UTC == 10:00 ET is still regular on the half-day.
        assert derive_market_session("us", _utc(2025, 11, 28, 15, 0)) == "regular"

    def test_kr_regular(self) -> None:
        # 02:00 UTC == 11:00 KST, within KST 09:00-15:30.
        assert derive_market_session("kr", _utc(2026, 5, 29, 2, 0)) == "regular"

    def test_kr_outside_regular_is_none_not_fabricated(self) -> None:
        # 08:00 UTC == 17:00 KST (after 15:30 close). KR extended/NXT is not
        # fabricated here -> None.
        assert derive_market_session("kr", _utc(2026, 5, 29, 8, 0)) is None

    def test_kr_weekend_is_none(self) -> None:
        assert derive_market_session("kr", _utc(2026, 5, 30, 2, 0)) is None

    def test_crypto_is_always_24x7(self) -> None:
        assert derive_market_session("crypto", _utc(2026, 5, 30, 3, 0)) == "24x7"

    def test_crypto_24x7_even_without_timestamp(self) -> None:
        assert derive_market_session("crypto", None) == "24x7"

    def test_none_timestamp_is_none(self) -> None:
        assert derive_market_session("us", None) is None

    def test_unknown_market_is_none(self) -> None:
        assert derive_market_session("jp", _utc(2026, 5, 29, 19, 39)) is None

    def test_naive_datetime_treated_as_utc(self) -> None:
        naive = dt.datetime(2026, 5, 29, 19, 39)
        assert derive_market_session("us", naive) == "regular"

    def test_market_case_and_whitespace_insensitive(self) -> None:
        assert derive_market_session("  US ", _utc(2026, 5, 29, 19, 39)) == "regular"
