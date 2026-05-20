"""ROB-281 Stage 7 — Market-aware dispatch helpers + view-model wiring tests.

Covers the public dispatch surface used by ``screener_service``:

* ``expected_baseline_date(market)`` routes KR/US to session-aware helpers
  and falls back to ``today_trading_date`` for unknown markets.
* ``session_label_for_partition(market, computed_at)`` routes KR/US to the
  per-market label helpers and returns None for unknown markets.

Plus a focused integration test on ``_build_freshness`` proving that
``asOfLabel`` for a screener-snapshot result carries the parenthetical
session token (KRX preliminary / NXT final / US post-close), without
breaking ROB-277 served-time vs data-as-of semantics.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.services.invest_screener_snapshots.freshness import (
    expected_baseline_date,
    session_label_for_partition,
    today_trading_date,
)
from app.services.invest_view_model.screener_service import _build_freshness

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _kst(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_KST)


def _et(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_ET)


# --- expected_baseline_date dispatch ----------------------------------------


def test_expected_baseline_date_kr_in_pre_market_window_returns_prior_day() -> None:
    """The whole point of Stage 7 wiring: KR 07:40 KST → prior trading day."""
    assert expected_baseline_date("kr", now=_kst(2026, 5, 20, 7, 40)) == dt.date(
        2026, 5, 19
    )


def test_expected_baseline_date_kr_after_krx_preliminary_returns_today() -> None:
    assert expected_baseline_date("kr", now=_kst(2026, 5, 20, 16, 20)) == dt.date(
        2026, 5, 20
    )


def test_expected_baseline_date_us_post_close_returns_today() -> None:
    assert expected_baseline_date("us", now=_et(2025, 6, 9, 17, 20)) == dt.date(
        2025, 6, 9
    )


def test_expected_baseline_date_us_before_post_close_returns_prior() -> None:
    assert expected_baseline_date("us", now=_et(2025, 6, 9, 17, 19)) == dt.date(
        2025, 6, 6
    )


def test_expected_baseline_date_unknown_market_falls_back_to_today_trading_date() -> (
    None
):
    """Crypto / future markets must not crash — degrade to today_trading_date."""
    now = dt.datetime(2026, 5, 20, 14, 0, tzinfo=dt.UTC)
    assert expected_baseline_date("crypto", now=now) == today_trading_date(
        "crypto", now=now
    )


# --- session_label_for_partition dispatch -----------------------------------


def test_session_label_for_partition_kr_krx_preliminary() -> None:
    assert (
        session_label_for_partition("kr", _kst(2026, 5, 20, 16, 25))
        == "KRX preliminary"
    )


def test_session_label_for_partition_kr_nxt_final() -> None:
    assert session_label_for_partition("kr", _kst(2026, 5, 20, 20, 30)) == "NXT final"


def test_session_label_for_partition_us_post_close() -> None:
    assert session_label_for_partition("us", _et(2025, 6, 9, 17, 30)) == "US post-close"


def test_session_label_for_partition_none_input() -> None:
    assert session_label_for_partition("kr", None) is None
    assert session_label_for_partition("us", None) is None


def test_session_label_for_partition_unknown_market_returns_none() -> None:
    assert session_label_for_partition("crypto", _kst(2026, 5, 20, 16, 30)) is None


# --- _build_freshness asOfLabel composition ---------------------------------


def _now_factory(value: dt.datetime):
    return lambda: value


def test_build_freshness_kr_krx_preliminary_appends_label_to_as_of() -> None:
    """ROB-281 + ROB-277 contract: asOfLabel ends with `(KRX preliminary)`."""
    served_now = dt.datetime(2026, 5, 20, 16, 30, tzinfo=dt.UTC)
    computed_at = _kst(2026, 5, 20, 16, 20)
    freshness = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="kr",
        now=_now_factory(served_now),
        dataState="fresh",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 20),
        primary_computed_at=computed_at,
        primary_source="invest_screener_snapshots",
    )
    assert freshness.primary is not None
    assert freshness.primary.asOfLabel.endswith("(KRX preliminary)")
    # ROB-277: served vs data-as-of remain distinct.
    assert freshness.servedAt is not None
    assert freshness.primary.asOfLabel != freshness.servedRelativeLabel


def test_build_freshness_kr_nxt_final_appends_label() -> None:
    served_now = dt.datetime(2026, 5, 20, 11, 30, tzinfo=dt.UTC)  # 20:30 KST
    computed_at = _kst(2026, 5, 20, 20, 20)
    freshness = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="kr",
        now=_now_factory(served_now),
        dataState="fresh",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 20),
        primary_computed_at=computed_at,
        primary_source="invest_screener_snapshots",
    )
    assert freshness.primary is not None
    assert freshness.primary.asOfLabel.endswith("(NXT final)")


def test_build_freshness_us_appends_post_close_label() -> None:
    served_now = dt.datetime(2025, 6, 9, 22, 30, tzinfo=dt.UTC)
    computed_at = _et(2025, 6, 9, 17, 20)
    freshness = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="us",
        now=_now_factory(served_now),
        dataState="fresh",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2025, 6, 9),
        primary_computed_at=computed_at,
        primary_source="invest_screener_snapshots",
    )
    assert freshness.primary is not None
    assert freshness.primary.asOfLabel.endswith("(US post-close)")


def test_build_freshness_live_kind_does_not_get_session_token() -> None:
    """Live (non-snapshot) results have no session label — preserves ROB-277."""
    served_now = dt.datetime(2026, 5, 20, 11, 30, tzinfo=dt.UTC)
    freshness = _build_freshness(
        raw_timestamp=None,
        cache_hit=False,
        market="kr",
        now=_now_factory(served_now),
        dataState="fresh",
        primary_kind="live",
        primary_snapshot_date=None,
        primary_computed_at=None,
        primary_source="kr_live_screener",
    )
    assert freshness.primary is not None
    # live kind uses data_basis_kst format, no parenthetical session token.
    assert "KRX" not in freshness.primary.asOfLabel
    assert "NXT" not in freshness.primary.asOfLabel
    assert "post-close" not in freshness.primary.asOfLabel


def test_build_freshness_screener_snapshot_with_no_computed_at_omits_token() -> None:
    """Without computed_at, session classification falls through — no token."""
    served_now = dt.datetime(2026, 5, 20, 11, 30, tzinfo=dt.UTC)
    freshness = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="kr",
        now=_now_factory(served_now),
        dataState="fresh",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 20),
        primary_computed_at=None,  # no computed_at → no session token
        primary_source="invest_screener_snapshots",
    )
    assert freshness.primary is not None
    # asOfLabel falls back to "장마감 기준" (per format_kst_as_of_label) with no
    # parenthetical session token appended.
    assert "(" not in freshness.primary.asOfLabel
