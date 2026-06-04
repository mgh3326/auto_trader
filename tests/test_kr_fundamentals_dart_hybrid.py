"""ROB-433: DART-first growth/streak in the tvscreener KR fundamentals loader.

`_passes_thresholds` evaluates the 3년평균 증감률 + 순이익 연속증가 conditions from the
DART derivation when present (growth_source/streak_source = "dart"), falling back to
the tvscreener 1yr-YoY proxy (growth) / SKIP (streak) when DART is absent.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.derive import MetricResult
from app.services.invest_view_model.fundamentals_screener import (
    STABLE_GROWTH_SPEC,
    UNDERVALUED_GROWTH_SPEC,
)
from app.services.invest_view_model.kr_fundamentals_tv_screener import (
    _passes_thresholds,
)

_PD = dt.date(2026, 6, 4)


def _snap(**kw) -> InvestKrFundamentalsSnapshot:
    base = {"symbol": "000001", "snapshot_date": _PD}
    base.update(kw)
    return InvestKrFundamentalsSnapshot(**base)


def _ok(value) -> MetricResult:
    return MetricResult(value=value, state="ok")


def _dart(*, rev=None, eps=None, streak=None) -> SimpleNamespace:
    return SimpleNamespace(
        revenue_growth_3y_avg=_ok(rev)
        if rev is not None
        else MetricResult(None, "unavailable"),
        earnings_growth_3y_avg=_ok(eps)
        if eps is not None
        else MetricResult(None, "unavailable"),
        earnings_increase_streak_years=_ok(streak)
        if streak is not None
        else MetricResult(None, "unavailable"),
    )


# --- T2: 3년평균 growth, DART-first ---------------------------------------------


def test_growth_uses_dart_over_proxy_when_present() -> None:
    # proxy YoY would FAIL (0.05 < 0.10), but DART 3y-avg passes → DART wins.
    snap = _snap(per=10.0, revenue_yoy=0.05, eps_yoy=0.05)
    dart = _dart(rev=0.30, eps=0.40)
    ok, reason, prov = _passes_thresholds(
        snap, UNDERVALUED_GROWTH_SPEC, partition_date=_PD, dart=dart
    )
    assert ok, reason
    assert prov["growth_source"] == "dart"


def test_growth_falls_back_to_proxy_when_dart_absent() -> None:
    # No DART → tvscreener YoY proxy (here it passes), provenance proxy.
    snap = _snap(per=10.0, revenue_yoy=0.15, eps_yoy=0.25)
    ok, reason, prov = _passes_thresholds(
        snap, UNDERVALUED_GROWTH_SPEC, partition_date=_PD, dart=None
    )
    assert ok, reason
    assert prov["growth_source"] == "proxy"


def test_growth_proxy_fallback_can_exclude() -> None:
    # No DART + proxy below threshold → excluded (fail-closed, never fabricated).
    snap = _snap(per=10.0, revenue_yoy=0.05, eps_yoy=0.25)
    ok, reason, _ = _passes_thresholds(
        snap, UNDERVALUED_GROWTH_SPEC, partition_date=_PD, dart=None
    )
    assert not ok
    assert "revenue_growth_3y_avg" in (reason or "")


def test_growth_source_proxy_wins_when_mixed() -> None:
    # revenue via DART, earnings via proxy → aggregate provenance = "proxy".
    snap = _snap(per=10.0, revenue_yoy=0.0, eps_yoy=0.30)
    dart = _dart(rev=0.30)  # only revenue in DART; earnings falls back to proxy 0.30
    ok, reason, prov = _passes_thresholds(
        snap, UNDERVALUED_GROWTH_SPEC, partition_date=_PD, dart=dart
    )
    assert ok, reason
    assert prov["growth_source"] == "proxy"


# --- T3: 순이익 연속증가 streak, DART-first ---------------------------------------


def test_streak_applied_from_dart() -> None:
    snap = _snap(roe_ttm=20.0, eps_yoy=0.15)
    dart = _dart(eps=0.15, streak=5)
    ok, reason, prov = _passes_thresholds(
        snap, STABLE_GROWTH_SPEC, partition_date=_PD, dart=dart
    )
    assert ok, reason
    assert prov["streak_source"] == "dart"


def test_streak_below_threshold_excludes() -> None:
    snap = _snap(roe_ttm=20.0, eps_yoy=0.15)
    dart = _dart(eps=0.15, streak=2)  # < 3 → excluded
    ok, reason, _ = _passes_thresholds(
        snap, STABLE_GROWTH_SPEC, partition_date=_PD, dart=dart
    )
    assert not ok
    assert "earnings_increase_streak_years" in (reason or "")


def test_streak_skipped_when_dart_absent_fail_open() -> None:
    # No DART streak → SKIP (fail-open): row still passes, provenance "skipped".
    snap = _snap(roe_ttm=20.0, eps_yoy=0.15)
    ok, reason, prov = _passes_thresholds(
        snap, STABLE_GROWTH_SPEC, partition_date=_PD, dart=None
    )
    assert ok, reason
    assert prov["streak_source"] == "skipped"
    assert prov["growth_source"] == "proxy"  # eps growth fell back to YoY proxy
