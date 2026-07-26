# tests/test_trade_retrospective_aggregate.py
"""ROB-474 — retrospective list + aggregate."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import TradeRetrospective
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


async def _seed(
    db, *, strategy, pnl, currency="KRW", evidence=True, account_mode="kis_mock"
):
    await svc.save_retrospective(
        db,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode=account_mode,
        outcome="filled",
        strategy_key=strategy,
        realized_pnl=(pnl if evidence else None),
        realized_pnl_currency=(currency if evidence else None),
        pnl_pct=(1.0 if pnl is not None and pnl > 0 else -1.0) if evidence else None,
    )
    await db.commit()


@pytest.mark.asyncio
async def test_aggregate_by_strategy_win_rate_and_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    await _seed(db_session, strategy="A", pnl=-50.0)
    await _seed(db_session, strategy="B", pnl=200.0)
    result = await svc.build_retrospective_aggregate(
        db_session,
        group_by="strategy",
    )
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["A"]["sample_size"] == 2
    assert groups["A"]["wins"] == 1
    assert groups["A"]["misses"] == 1
    assert groups["A"]["win_rate_pct"] == 50.0
    assert groups["A"]["realized_pnl_sum"]["KRW"] == 50.0  # 100 + (-50)
    assert groups["B"]["win_rate_pct"] == 100.0


@pytest.mark.asyncio
async def test_currency_separated_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, currency="KRW")
    await _seed(db_session, strategy="A", pnl=5.0, currency="USD")
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    g = result["groups"][0]
    assert g["realized_pnl_sum"] == {"KRW": 100.0, "USD": 5.0}


@pytest.mark.asyncio
async def test_no_fill_evidence_excluded_from_aggregate(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, evidence=True)
    # kiwoom: no evidence row
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kiwoom_mock",
        outcome="unfilled",
        strategy_key="A",
    )
    await db_session.commit()
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    assert result["excluded_no_fill_evidence"] == 1
    assert result["groups"][0]["sample_size"] == 1


@pytest.mark.asyncio
async def test_empty_window_returns_no_groups(db_session: AsyncSession):
    result = await svc.build_retrospective_aggregate(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2000-01-02",
        group_by="strategy",
    )
    assert result["groups"] == []


@pytest.mark.asyncio
async def test_get_retrospectives_list_and_summary(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    res = await svc.get_retrospectives(db_session, strategy_key="A")
    assert res["summary"]["count"] == 1
    assert res["summary"]["by_outcome"]["filled"] == 1
    assert res["entries"][0]["strategy_key"] == "A"


@pytest.mark.asyncio
async def test_win_rate_denominator_is_decided_rows(db_session: AsyncSession):
    # one decided win + one evidence-available UNDECIDED row (no realized_pnl/pnl_pct).
    # win_rate must use decided rows (1) as denominator -> 100.0, not sample_size (2) -> 50.0.
    await _seed(db_session, strategy="A", pnl=100.0)
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
    )  # undecided: no realized_pnl, no pnl_pct
    await db_session.commit()
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["sample_size"] == 2
    assert g["wins"] == 1
    assert g["misses"] == 0  # undecided row is NOT a miss
    assert g["win_rate_pct"] == 100.0


@pytest.mark.asyncio
async def test_pnl_pct_only_win(db_session: AsyncSession):
    # percent-only retro (realized_pnl=None, pnl_pct>0) must count as a win
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
        pnl_pct=2.5,
    )
    await db_session.commit()
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["wins"] == 1
    assert g["win_rate_pct"] == 100.0
    assert g["realized_pnl_sum"] == {}  # no absolute amount


@pytest.mark.asyncio
async def test_group_by_day(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    result = await svc.build_retrospective_aggregate(db_session, group_by="day")
    assert result["group_by"] == "day"
    today_kst = now_kst().date().isoformat()
    assert result["groups"][0]["group"] == today_kst


@pytest.mark.asyncio
async def test_avg_pnl_pct_value_and_none(db_session: AsyncSession):
    # two rows pnl_pct +1.0 / -1.0 -> avg 0.0 (computed, not None)
    await _seed(db_session, strategy="A", pnl=100.0)  # pnl_pct 1.0
    await _seed(db_session, strategy="A", pnl=-50.0)  # pnl_pct -1.0
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["avg_pnl_pct"] == 0.0
    # a group whose only rows have no pnl_pct -> None (not 0)
    await db_session.execute(delete(TradeRetrospective))
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="B",
        realized_pnl=10.0,
        realized_pnl_currency="KRW",
    )  # decided by realized_pnl, but pnl_pct is None
    await db_session.commit()
    g2 = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g2["avg_pnl_pct"] is None


@pytest.mark.asyncio
async def test_aggregate_sums_fx_and_total_krw(db_session: AsyncSession):
    await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        strategy_key="A",
        realized_pnl=60.0,
        realized_pnl_currency="USD",
        fx_pnl_krw=22772.0,
        total_pnl_krw=112963.4,
    )
    await db_session.commit()

    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    group = result["groups"][0]
    assert group["fx_pnl_krw_sum"] == pytest.approx(22772.0)
    assert group["total_pnl_krw_sum"] == pytest.approx(112963.4)


# ---------------------------------------------------------------------------
# ROB-647 — trigger_type / root_cause grouping dimensions
# ---------------------------------------------------------------------------


async def _seed_postmortem(
    db,
    *,
    trigger_type,
    root_cause_class,
    account_mode="kis_live",
    outcome="filled",
):
    await svc.save_retrospective(
        db,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode=account_mode,
        outcome=outcome,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        next_actions=[{"action": "follow up"}],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_aggregate_group_by_trigger_type(db_session: AsyncSession):
    await _seed_postmortem(db_session, trigger_type="fill", root_cause_class="analysis")
    await _seed_postmortem(
        db_session, trigger_type="fill", root_cause_class="execution"
    )
    await _seed_postmortem(
        db_session,
        trigger_type="rejected_order",
        root_cause_class="policy",
        outcome="rejected",
    )
    result = await svc.build_retrospective_aggregate(
        db_session, group_by="trigger_type"
    )
    assert result["group_by"] == "trigger_type"
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["fill"]["sample_size"] == 2
    assert groups["rejected_order"]["sample_size"] == 1
    # per-group breakdown dimensions present
    assert groups["fill"]["by_root_cause_class"] == {"analysis": 1, "execution": 1}


@pytest.mark.asyncio
async def test_aggregate_group_by_root_cause(db_session: AsyncSession):
    await _seed_postmortem(db_session, trigger_type="fill", root_cause_class="analysis")
    await _seed_postmortem(
        db_session,
        trigger_type="policy_violation",
        root_cause_class="policy",
        outcome="rejected",
    )
    result = await svc.build_retrospective_aggregate(db_session, group_by="root_cause")
    groups = {g["group"]: g for g in result["groups"]}
    assert set(groups) == {"analysis", "policy"}
    assert groups["policy"]["by_trigger_type"] == {"policy_violation": 1}


# ---------------------------------------------------------------------------
# ROB-691 — get_retrospectives filters: outcome_filter / symbol_search /
# kst_date_from-to. The outcome_filter SQL predicates MUST match the Python
# `_is_win`/`_is_decided` semantics used by build_retrospective_aggregate
# (same win/loss/decided rules, tie=0 counted as loss) — this is the parallel
# equivalence test the plan calls out as the biggest correctness risk.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_filter_matches_python_predicate(db_session: AsyncSession):
    from sqlalchemy import select as sa_select

    # win via realized_pnl
    await _seed(db_session, strategy="A", pnl=100.0)
    # loss via realized_pnl
    await _seed(db_session, strategy="A", pnl=-50.0)
    # tie (0) -> decided but NOT a win (loss bucket)
    await _seed(db_session, strategy="A", pnl=0.0)
    # win via pnl_pct fallback (realized_pnl absent)
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="B",
        pnl_pct=1.5,
    )
    # loss via pnl_pct fallback, tie(0) included
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="B",
        pnl_pct=0.0,
    )
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="B",
        pnl_pct=-2.0,
    )
    # no-evidence row: neither realized_pnl nor pnl_pct -> not decided at all
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kiwoom_mock",
        outcome="unfilled",
        strategy_key="C",
    )
    await db_session.commit()

    # Ground truth computed in Python directly against the ORM rows, exactly
    # mirroring what build_retrospective_aggregate uses internally.
    all_rows = (await db_session.execute(sa_select(TradeRetrospective))).scalars().all()
    expected_win_ids = {r.id for r in all_rows if svc._is_win(r)}
    expected_decided_ids = {r.id for r in all_rows if svc._is_decided(r)}
    expected_loss_ids = expected_decided_ids - expected_win_ids
    # 7 rows seeded, 1 has no evidence at all (neither realized_pnl nor
    # pnl_pct) -> 6 decided. Wins: pnl=100 (realized_pnl) + pnl_pct=1.5
    # (fallback) = 2. Losses: pnl=-50, pnl=0 (tie), pnl_pct=0.0 (tie),
    # pnl_pct=-2.0 = 4.
    assert len(expected_decided_ids) == 6
    assert len(expected_win_ids) == 2
    assert len(expected_loss_ids) == 4

    win_result = await svc.get_retrospectives(
        db_session, outcome_filter="win", limit=100
    )
    assert {e["id"] for e in win_result["entries"]} == expected_win_ids

    loss_result = await svc.get_retrospectives(
        db_session, outcome_filter="loss", limit=100
    )
    assert {e["id"] for e in loss_result["entries"]} == expected_loss_ids

    decided_result = await svc.get_retrospectives(
        db_session, outcome_filter="decided", limit=100
    )
    assert {e["id"] for e in decided_result["entries"]} == expected_decided_ids


@pytest.mark.asyncio
async def test_outcome_filter_invalid_value_raises(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.get_retrospectives(db_session, outcome_filter="bogus")


@pytest.mark.asyncio
async def test_symbol_search_prefix_ilike(db_session: AsyncSession):
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
    )
    await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
    )
    await db_session.commit()

    res = await svc.get_retrospectives(db_session, symbol_search="005")
    assert {e["symbol"] for e in res["entries"]} == {"005930"}

    res_lower = await svc.get_retrospectives(db_session, symbol_search="aap")
    assert {e["symbol"] for e in res_lower["entries"]} == {"AAPL"}

    res_none = await svc.get_retrospectives(db_session, symbol_search="ZZZ")
    assert res_none["entries"] == []


@pytest.mark.asyncio
async def test_kst_date_from_to_boundaries(db_session: AsyncSession):
    from datetime import timedelta

    from app.core.timezone import now_kst

    await _seed(db_session, strategy="A", pnl=100.0)
    await db_session.commit()
    today = now_kst().date().isoformat()
    yesterday = (now_kst().date() - timedelta(days=1)).isoformat()
    tomorrow = (now_kst().date() + timedelta(days=1)).isoformat()

    in_window = await svc.get_retrospectives(
        db_session, kst_date_from=today, kst_date_to=today
    )
    assert in_window["summary"]["count"] == 1

    out_of_window = await svc.get_retrospectives(
        db_session, kst_date_from=yesterday, kst_date_to=yesterday
    )
    assert out_of_window["summary"]["count"] == 0

    range_window = await svc.get_retrospectives(
        db_session, kst_date_from=yesterday, kst_date_to=tomorrow
    )
    assert range_window["summary"]["count"] == 1


@pytest.mark.asyncio
async def test_process_dims_include_no_fill_evidence_rows(db_session: AsyncSession):
    # kiwoom_mock has no fill evidence — excluded from PnL dims, INCLUDED in
    # process dims (rejected/cancelled postmortems must still be analyzed).
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kiwoom_mock",
        outcome="rejected",
        trigger_type="rejected_order",
        root_cause_class="policy",
        next_actions=[{"action": "review policy"}],
    )
    await db_session.commit()

    by_strategy = await svc.build_retrospective_aggregate(
        db_session, group_by="strategy"
    )
    assert by_strategy["excluded_no_fill_evidence"] == 1
    assert by_strategy["groups"] == []

    by_trigger = await svc.build_retrospective_aggregate(
        db_session, group_by="trigger_type"
    )
    assert by_trigger["excluded_no_fill_evidence"] == 0
    groups = {g["group"]: g for g in by_trigger["groups"]}
    assert groups["rejected_order"]["sample_size"] == 1


@pytest.mark.asyncio
async def test_missed_cohort_is_separate_from_trade_performance_groups(
    db_session: AsyncSession,
):
    await _seed(db_session, strategy="executed", pnl=100.0)
    await svc.save_retrospective(
        db_session,
        symbol="000660",
        instrument_type="equity_kr",
        account_mode="toss_live",
        market="kr",
        outcome="unfilled",
        strategy_key="missed_opportunity",
        pnl_pct=7.5,
        trigger_type="missed_opportunity",
        next_actions=[{"action": "score D+5"}],
    )
    await svc.save_retrospective(
        db_session,
        symbol="035420",
        instrument_type="equity_kr",
        account_mode="toss_live",
        market="kr",
        outcome="unfilled",
        strategy_key="missed_opportunity",
        trigger_type="missed_opportunity",
        next_actions=[{"action": "score D+5"}],
    )
    await db_session.commit()

    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")

    groups = {g["group"]: g for g in result["groups"]}
    assert set(groups) == {"executed"}
    assert result["excluded_missed_opportunity"] == 2
    assert result["missed_cohort"] == {
        "sample_size": 2,
        "scored_sample_size": 1,
        "pending_sample_size": 1,
        "positive_opportunities": 1,
        "non_positive_opportunities": 0,
        "positive_opportunity_rate_pct": 100.0,
        "avg_opportunity_return_pct": 7.5,
        "by_market": {"kr": 2},
    }

    by_trigger = await svc.build_retrospective_aggregate(
        db_session, group_by="trigger_type"
    )
    trigger_groups = {g["group"]: g for g in by_trigger["groups"]}
    assert trigger_groups["missed_opportunity"]["sample_size"] == 2
