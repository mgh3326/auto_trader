import pytest
from sqlalchemy import inspect

from app.models.research_pipeline import (
    ResearchSession,
    ResearchSummary,
    StageAnalysis,
    SummaryStageLink,
    UserResearchNote,
)


@pytest.mark.unit
def test_research_session_columns():
    cols = {c.name for c in inspect(ResearchSession).columns}
    assert {"id", "stock_info_id", "research_run_id", "status",
            "started_at", "finalized_at", "created_at", "updated_at"} <= cols


@pytest.mark.unit
def test_stage_analysis_columns_and_constraints():
    cols = {c.name for c in inspect(StageAnalysis).columns}
    assert {"id", "session_id", "stage_type", "verdict", "confidence",
            "signals", "raw_payload", "source_freshness", "model_name",
            "prompt_version", "snapshot_at", "executed_at"} <= cols
    constraint_names = {c.name for c in StageAnalysis.__table__.constraints if c.name}
    assert any("stage_type" in n for n in constraint_names)
    assert any("verdict" in n for n in constraint_names)


@pytest.mark.unit
def test_research_summary_no_unique_session_id():
    from sqlalchemy import UniqueConstraint
    summary_table = ResearchSummary.__table__
    for uc in summary_table.constraints:
        if not isinstance(uc, UniqueConstraint):
            continue
        cols = getattr(uc, "columns", None)
        if cols is None:
            continue
        col_names = {c.name for c in cols}
        assert col_names != {"session_id"}, "session_id must NOT be unique (append-only re-summaries allowed)"


@pytest.mark.unit
def test_summary_stage_link_columns():
    cols = {c.name for c in inspect(SummaryStageLink).columns}
    assert {"id", "summary_id", "stage_analysis_id", "weight", "direction",
            "rationale"} <= cols


@pytest.mark.unit
def test_user_research_note_columns():
    cols = {c.name for c in inspect(UserResearchNote).columns}
    assert {"id", "session_id", "user_id", "body", "created_at", "updated_at"} <= cols
