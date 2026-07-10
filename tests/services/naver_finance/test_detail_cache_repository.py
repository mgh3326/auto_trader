"""ROB-811 naver_research_detail_cache model + repository tests."""

from __future__ import annotations

import pytest

from app.models.naver_research_detail_cache import NaverResearchDetailCache


@pytest.mark.unit
def test_model_table_and_columns() -> None:
    assert NaverResearchDetailCache.__tablename__ == "naver_research_detail_cache"
    cols = set(NaverResearchDetailCache.__table__.columns.keys())
    assert cols == {"nid", "target_price", "rating", "fetched_at"}
    assert NaverResearchDetailCache.__table__.c.nid.primary_key is True