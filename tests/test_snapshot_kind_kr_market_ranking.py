# tests/test_snapshot_kind_kr_market_ranking.py
import pytest

from app.models.investment_snapshots import InvestmentSnapshot


def _snapshot_kind_check_sql() -> str:
    for c in InvestmentSnapshot.__table__.constraints:
        name = getattr(c, "name", "")
        if name in (
            "ck_investment_snapshots_snapshot_kind",
            "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind",
        ):
            return str(c.sqltext)
    raise AssertionError("snapshot_kind CHECK not found")


@pytest.mark.unit
def test_check_includes_kr_market_ranking_and_preserves_old_kinds():
    sql = _snapshot_kind_check_sql()
    assert "kr_market_ranking" in sql
    for kind in (
        "portfolio",
        "market",
        "news",
        "symbol",
        "candidate_universe",
        "browser_probe",
        "invest_page",
        "journal",
        "watch_context",
        "naver_remote_debug",
        "toss_remote_debug",
        "llm_input_frozen",
        "pending_orders",
        "validated_run_card",
    ):
        assert kind in sql, f"existing kind dropped: {kind}"
