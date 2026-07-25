"""ROB-918: read-only 2-week shadow aggregation for kr-preopen new candidates.

The script never writes to the DB — it only reads trading_decision_sessions
and joins kr_candles_1d (via a mockable helper, since kr_candles_1d is a raw
Timescale hypertable absent from the unit test_db) to compute D+1 % moves.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.trading_decision import TradingDecisionSession
from app.services import trading_decision_service
from scripts.shadow_new_candidates_report import build_shadow_report, summarize_rows

# ROB-918 owns this strategy_name marker so cleanup can scope to just these
# tests' rows in the shared persistent test_db (no exact-count assertions).
_STRATEGY_NAME = "kr-preopen-shadow-test"
_BASELINE_DATE = dt.date(2026, 7, 15)
_SYMBOL = "918555"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session):
    async def _purge() -> None:
        await db_session.execute(
            sa.delete(TradingDecisionSession).where(
                TradingDecisionSession.strategy_name == _STRATEGY_NAME
            )
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_shadow_report_computes_d1_close_pct(db_session, user, monkeypatch):
    async def _fake_next_close(_session, *, symbol, venue, after_date):
        if symbol != _SYMBOL:
            return None
        assert venue == "KRX"
        assert after_date == _BASELINE_DATE
        return 106.0, dt.datetime(2026, 7, 16, 6, 0, tzinfo=dt.UTC)

    import scripts.shadow_new_candidates_report as mod

    monkeypatch.setattr(mod, "_fetch_next_close", _fake_next_close)

    market_brief = {
        "advisory_only": True,
        "new_candidates": {
            "advisory_only": True,
            "market_state": "normal",
            "consecutive_gainers": [
                {
                    "symbol": _SYMBOL,
                    "name": "테스트종목",
                    "reason": "consecutive_gainers",
                    "advisory_only": True,
                    "baseline_date": _BASELINE_DATE.isoformat(),
                    "baseline_close": 100.0,
                    "outcome": {"d1_close_pct": None},
                }
            ],
            "theme_leaders": [],
            "double_buy": [],
        },
    }
    await trading_decision_service.create_decision_session(
        db_session,
        user_id=user.id,
        source_profile="research_run",
        strategy_name="kr-preopen-shadow-test",
        market_scope="kr",
        market_brief=market_brief,
        generated_at=dt.datetime(2026, 7, 16, 0, 0, tzinfo=dt.UTC),
    )
    await db_session.commit()

    rows = await build_shadow_report(
        db_session, since=dt.date(2026, 7, 1), market_scope="kr"
    )

    matches = [r for r in rows if r.symbol == _SYMBOL]
    assert matches, f"expected a row for {_SYMBOL}, got {[r.symbol for r in rows]}"
    row = matches[0]
    assert row.reason == "consecutive_gainers"
    assert row.baseline_close == pytest.approx(100.0)
    assert row.d1_close == pytest.approx(106.0)
    assert row.d1_close_pct == pytest.approx(6.0)
    assert row.evaluable is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_shadow_report_marks_unevaluable_when_no_next_candle(
    db_session, user, monkeypatch
):
    async def _fake_next_close(_session, *, symbol, venue, after_date):
        return None

    import scripts.shadow_new_candidates_report as mod

    monkeypatch.setattr(mod, "_fetch_next_close", _fake_next_close)

    symbol = "918556"
    market_brief = {
        "new_candidates": {
            "advisory_only": True,
            "market_state": "normal",
            "consecutive_gainers": [
                {
                    "symbol": symbol,
                    "name": "노데이터",
                    "reason": "consecutive_gainers",
                    "baseline_date": _BASELINE_DATE.isoformat(),
                    "baseline_close": 100.0,
                    "outcome": {"d1_close_pct": None},
                }
            ],
            "theme_leaders": [],
            "double_buy": [],
        },
    }
    await trading_decision_service.create_decision_session(
        db_session,
        user_id=user.id,
        source_profile="research_run",
        strategy_name="kr-preopen-shadow-test",
        market_scope="kr",
        market_brief=market_brief,
        generated_at=dt.datetime(2026, 7, 16, 0, 0, tzinfo=dt.UTC),
    )
    await db_session.commit()

    rows = await build_shadow_report(
        db_session, since=dt.date(2026, 7, 1), market_scope="kr"
    )

    matches = [r for r in rows if r.symbol == symbol]
    assert matches
    assert matches[0].evaluable is False
    assert matches[0].d1_close_pct is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_shadow_report_ignores_sessions_before_since(
    db_session, user, monkeypatch
):
    async def _fake_next_close(_session, **_kwargs):
        return 999.0, dt.datetime(2026, 6, 2, tzinfo=dt.UTC)

    import scripts.shadow_new_candidates_report as mod

    monkeypatch.setattr(mod, "_fetch_next_close", _fake_next_close)

    symbol = "918557"
    market_brief = {
        "new_candidates": {
            "consecutive_gainers": [
                {
                    "symbol": symbol,
                    "baseline_date": "2026-06-01",
                    "baseline_close": 50.0,
                    "reason": "consecutive_gainers",
                }
            ],
            "theme_leaders": [],
            "double_buy": [],
        }
    }
    await trading_decision_service.create_decision_session(
        db_session,
        user_id=user.id,
        source_profile="research_run",
        strategy_name="kr-preopen-shadow-test",
        market_scope="kr",
        market_brief=market_brief,
        generated_at=dt.datetime(2026, 6, 1, 0, 0, tzinfo=dt.UTC),
    )
    await db_session.commit()

    rows = await build_shadow_report(
        db_session, since=dt.date(2026, 7, 1), market_scope="kr"
    )

    assert all(r.symbol != symbol for r in rows)


@pytest.mark.unit
def test_summarize_rows_computes_recovered_and_false_positive_rate():
    from scripts.shadow_new_candidates_report import ShadowCandidateRow

    rows = [
        ShadowCandidateRow(
            session_id=1,
            session_uuid="a",
            generated_at=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            reason="consecutive_gainers",
            symbol="A",
            name=None,
            baseline_date=_BASELINE_DATE,
            baseline_close=100.0,
            d1_close=110.0,
            d1_time=None,
            d1_close_pct=10.0,
            evaluable=True,
        ),
        ShadowCandidateRow(
            session_id=1,
            session_uuid="a",
            generated_at=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            reason="consecutive_gainers",
            symbol="B",
            name=None,
            baseline_date=_BASELINE_DATE,
            baseline_close=100.0,
            d1_close=95.0,
            d1_time=None,
            d1_close_pct=-5.0,
            evaluable=True,
        ),
        ShadowCandidateRow(
            session_id=1,
            session_uuid="a",
            generated_at=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            reason="consecutive_gainers",
            symbol="C",
            name=None,
            baseline_date=_BASELINE_DATE,
            baseline_close=None,
            d1_close=None,
            d1_time=None,
            d1_close_pct=None,
            evaluable=False,
        ),
    ]

    summary = summarize_rows(rows)

    cg = summary["consecutive_gainers"]
    assert cg["total"] == 3
    assert cg["evaluated"] == 2
    assert cg["recovered_count"] == 1
    assert cg["recovered_rate"] == pytest.approx(0.5)
    assert cg["false_positive_rate"] == pytest.approx(0.5)
    assert cg["avg_d1_close_pct"] == pytest.approx(2.5)
