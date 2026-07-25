"""ROB-664 integration tests for analysis-artifact read filtering.

Mirrors the ROB-663 ``tests/test_forecast_web_read.py`` pattern: integration
mark, the ``investment_reports_cleanup_lock`` fixture for xdist DDL
serialization, and an autouse cleanup that wipes ``review.analysis_artifacts``
before/after each test so the suite is order-independent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.analysis_artifact import AnalysisArtifact
from app.services.analysis_artifact import AnalysisArtifactService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,  # noqa: ARG001 - xdist DDL lock
):
    await db_session.execute(sa.delete(AnalysisArtifact))
    await db_session.commit()
    yield
    await db_session.execute(sa.delete(AnalysisArtifact))
    await db_session.commit()


async def _add(db_session: AsyncSession, **kw) -> AnalysisArtifact:
    now = now_kst()
    defaults = {
        "market": "kr",
        "kind": "screening_ranking",
        "title": "t",
        "symbols": [],
        "payload": {},
        "as_of": now,
        "valid_until": now + timedelta(days=1),
        "created_by": "claude",
        "version": 1,
    }
    defaults.update(kw)
    row = AnalysisArtifact(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_list_filters_by_readiness_label(db_session: AsyncSession):
    await _add(db_session, title="ready", readiness_label="ready_for_order_review")
    await _add(db_session, title="blocked", readiness_label="blocked")
    svc = AnalysisArtifactService(db_session)

    rows = await svc.list_artifacts(readiness_label="ready_for_order_review")

    assert [r.title for r in rows] == ["ready"]


@pytest.mark.asyncio
async def test_list_readiness_none_returns_all(db_session: AsyncSession):
    await _add(db_session, title="a", readiness_label="ready_for_order_review")
    await _add(db_session, title="b", readiness_label=None)
    svc = AnalysisArtifactService(db_session)

    rows = await svc.list_artifacts()

    assert {r.title for r in rows} == {"a", "b"}
