from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from app.models.portfolio_decision_run import PortfolioDecisionRun


@pytest.mark.unit
def test_portfolio_decision_run_model_shape() -> None:
    assert PortfolioDecisionRun.__tablename__ == "portfolio_decision_runs"
    columns = PortfolioDecisionRun.__table__.columns
    assert columns["run_id"].primary_key is True
    assert columns["user_id"].index is True
    assert columns["market_scope"].type.length == 20
    assert isinstance(columns["payload"].type, JSONB)
    for field in ("filters", "summary", "facets", "symbol_groups", "warnings"):
        assert isinstance(columns[field].type, JSONB)

    index_names = {index.name for index in PortfolioDecisionRun.__table__.indexes}
    assert "ix_portfolio_decision_runs_user_generated_at" in index_names
    assert "ix_portfolio_decision_runs_market_scope" in index_names


@pytest.mark.unit
def test_portfolio_decision_run_repr_mentions_run_id_and_market() -> None:
    run = PortfolioDecisionRun(
        run_id="decision-test",
        user_id=7,
        generated_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        market_scope="CRYPTO",
        mode="analysis_only",
        source="portfolio_decision_service_v1",
        filters={},
        summary={},
        facets={},
        symbol_groups=[],
        warnings=[],
        payload={},
        created_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
    )

    assert "decision-test" in repr(run)
    assert "CRYPTO" in repr(run)
