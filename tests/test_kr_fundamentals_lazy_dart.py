"""ROB-504 P1: lazy DART derivation in the tvscreener KR fundamentals loader.

The loader derives the financial_fundamentals metrics (3년평균 growth / 순이익 연속증가
streak / 배당 payout·streak) ONLY for the symbols that pass the cheap valuation gate,
not the whole ~4,250-symbol partition.

Why it is safe: ``_passes_thresholds`` consumes the DART derivation strictly AFTER the
snapshot-column valuation thresholds (``_passes_valuation_only``) pass. A symbol failing
the valuation gate is excluded regardless of its DART derivation, so deriving DART for it
is pure waste — both the financial_fundamentals query IN-list and the Python derivation
shrink to the handful of valuation survivors.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.financial_fundamentals_snapshots.derive import MetricResult
from app.services.invest_view_model.fundamentals_screener import (
    HIGH_YIELD_VALUE_SPEC,
    STABLE_GROWTH_SPEC,
    UNDERVALUED_GROWTH_SPEC,
)
from app.services.invest_view_model.kr_fundamentals_tv_screener import (
    _dart_candidate_symbols,
    _passes_thresholds,
    _passes_valuation_only,
    load_kr_fundamentals_preset_from_tv_snapshot,
)

_PD = dt.date(2026, 6, 4)
_PREFIX = "9914"


def _unit_snap(symbol: str = "991400", **kw) -> InvestKrFundamentalsSnapshot:
    base = {"symbol": symbol, "snapshot_date": _PD}
    base.update(kw)
    return InvestKrFundamentalsSnapshot(**base)


# --- _passes_valuation_only: the cheap snapshot-column gate ----------------------


def test_valuation_only_passes_when_cheap_thresholds_met() -> None:
    # per <= max_per (20) and > 0 → valuation gate open, no reject reason.
    ok, reason = _passes_valuation_only(_unit_snap(per=10.0), UNDERVALUED_GROWTH_SPEC)
    assert ok
    assert reason is None


def test_valuation_only_reject_matches_passes_thresholds_regardless_of_dart() -> None:
    # A symbol failing the cheap valuation gate gets the SAME reject reason from
    # _passes_valuation_only and the full _passes_thresholds — and the full check
    # returns that reason regardless of the DART derivation (dart is never read,
    # so omitting it for valuation failures is behaviour-preserving).
    snap = _unit_snap(per=999.0, revenue_yoy=5.0, eps_yoy=5.0)  # per > max_per (20)
    v_ok, v_reason = _passes_valuation_only(snap, UNDERVALUED_GROWTH_SPEC)
    assert not v_ok
    assert v_reason == "per above max"

    dart = SimpleNamespace(
        revenue_growth_3y_avg=MetricResult(value=Decimal("9"), state="ok"),
        earnings_growth_3y_avg=MetricResult(value=Decimal("9"), state="ok"),
    )
    for dart_arg in (None, dart):
        t_ok, t_reason, _prov = _passes_thresholds(
            snap, UNDERVALUED_GROWTH_SPEC, partition_date=_PD, dart=dart_arg
        )
        assert not t_ok
        assert t_reason == v_reason


def test_valuation_only_rejects_null_required_column() -> None:
    ok, reason = _passes_valuation_only(_unit_snap(roe_ttm=None), STABLE_GROWTH_SPEC)
    assert not ok
    assert reason == "roe_ttm unavailable"


def test_valuation_only_rejects_non_positive_per() -> None:
    ok, reason = _passes_valuation_only(_unit_snap(per=0.0), UNDERVALUED_GROWTH_SPEC)
    assert not ok
    assert reason == "per not positive"


# --- _dart_candidate_symbols: which symbols actually need a DART derivation ------


def test_dart_candidates_empty_for_valuation_only_spec() -> None:
    # HIGH_YIELD_VALUE_SPEC has no DART threshold → the loader skips DART entirely.
    snaps = [_unit_snap("991401", roe_ttm=20.0, per=5.0)]
    assert (
        _dart_candidate_symbols(snaps, HIGH_YIELD_VALUE_SPEC, {"991401": "가나"}) == []
    )


def test_dart_candidates_only_valuation_survivors() -> None:
    # UNDERVALUED_GROWTH_SPEC: max_per=20 gate + growth. Only per<=20 symbols need DART.
    snaps = [
        _unit_snap("991401", per=10.0),  # survivor
        _unit_snap("991402", per=999.0),  # fails max_per → no DART
        _unit_snap("991403", per=8.0),  # survivor
    ]
    names = {"991401": "가", "991402": "나", "991403": "다"}
    assert _dart_candidate_symbols(snaps, UNDERVALUED_GROWTH_SPEC, names) == [
        "991401",
        "991403",
    ]


def test_dart_candidates_dedupe_and_drop_non_common_stock() -> None:
    snaps = [
        _unit_snap("991401", per=10.0),
        _unit_snap("991401", per=10.0),  # duplicate symbol collapses
        _unit_snap("991402", per=10.0),  # ETF name dropped by common-stock filter
    ]
    names = {"991401": "보통주", "991402": "테스트ETF"}
    assert _dart_candidate_symbols(snaps, UNDERVALUED_GROWTH_SPEC, names) == ["991401"]


# --- loader integration: the DART query is scoped to valuation survivors ---------

pytestmark_integration = pytest.mark.integration


def _snap(symbol: str, **kw) -> InvestKrFundamentalsSnapshot:
    base = {
        "symbol": symbol,
        "snapshot_date": _PD,
        "name": symbol,
        "source": "tvscreener_kr",
        "raw_payload": {},
    }
    base.update(kw)
    return InvestKrFundamentalsSnapshot(**base)


def _universe(symbol: str, name: str) -> KRSymbolUniverse:
    return KRSymbolUniverse(symbol=symbol, name=name, exchange="KOSPI", is_active=True)


async def _cleanup(db_session) -> None:
    await db_session.execute(
        sa.delete(InvestKrFundamentalsSnapshot).where(
            InvestKrFundamentalsSnapshot.symbol.like(f"{_PREFIX}%")
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like(f"{_PREFIX}%"))
    )
    await db_session.commit()


def _now():
    return dt.datetime(2026, 6, 4, 6, 0, tzinfo=dt.UTC)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_queries_dart_for_valuation_survivors_only(
    db_session, monkeypatch
) -> None:
    """The financial_fundamentals query receives ONLY the valuation survivors, and the
    included/excluded result is unchanged (the valuation-failing symbol is still
    excluded with its valuation reason — its DART derivation never mattered)."""
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}01"
    sym_fail = f"{_PREFIX}02"
    try:
        db_session.add_all(
            [
                # survivor: per <= 20 (gate open) + proxy growth high enough to pass
                _snap(
                    sym_pass,
                    price=Decimal("10000"),
                    per=Decimal("10"),
                    revenue_yoy=Decimal("0.50"),
                    eps_yoy=Decimal("0.50"),
                ),
                # fails max_per (20) → excluded before DART is consulted
                _snap(
                    sym_fail,
                    price=Decimal("10000"),
                    per=Decimal("999"),
                    revenue_yoy=Decimal("0.50"),
                    eps_yoy=Decimal("0.50"),
                ),
            ]
        )
        db_session.add_all([_universe(sym_pass, "통과"), _universe(sym_fail, "탈락")])
        await db_session.commit()

        captured: dict[str, list[str]] = {}

        from app.services.financial_fundamentals_snapshots.repository import (
            FinancialFundamentalsSnapshotsRepository,
        )

        async def _spy(self, *, market, symbols):
            captured["symbols"] = sorted(symbols)
            return {}

        monkeypatch.setattr(
            FinancialFundamentalsSnapshotsRepository,
            "latest_periods_for_symbols",
            _spy,
        )

        result = await load_kr_fundamentals_preset_from_tv_snapshot(
            db_session,
            market="kr",
            spec=UNDERVALUED_GROWTH_SPEC,
            limit=20,
            now=_now,
            universe_count=2,
        )
        assert result is not None
        # Membership (not equality): the shared test DB partition may carry residual
        # rows from sibling tests, but the ROB-504 claim is per-symbol — the valuation
        # survivor IS handed to the DART query, the valuation-failer is NOT.
        assert sym_pass in captured["symbols"]
        assert sym_fail not in captured["symbols"]
        # Behaviour preserved: survivor included, valuation-failer excluded on per
        # (its DART derivation never mattered — it was skipped, not consulted).
        row_symbols = [r["symbol"] for r in result.rows]
        assert sym_pass in row_symbols
        assert sym_fail not in row_symbols
        excluded = {e["symbol"]: e["reason"] for e in result.excluded}
        assert excluded.get(sym_fail) == "per above max"
    finally:
        # Clean up the shared test DB so the 9914 rows can't pollute order-sensitive
        # sibling tests that don't pin universe_count (this partition's snapshot_date
        # collides with theirs).
        await _cleanup(db_session)
