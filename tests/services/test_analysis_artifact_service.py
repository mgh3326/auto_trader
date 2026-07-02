from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.schemas.analysis_artifact import AnalysisArtifactSave
from app.services.analysis_artifact import AnalysisArtifactService


@pytest_asyncio.fixture(autouse=True)
async def _clean_analysis_artifacts(db_session: AsyncSession):
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE'
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE'
        )
    )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_list_get_round_trip(db_session: AsyncSession) -> None:
    service = AnalysisArtifactService(db_session)
    symbol = f"TEST-{uuid4().hex[:8]}"
    entry = AnalysisArtifactSave.model_validate(
        {
            "market": "kr",
            "kind": "candidate_pool",
            "title": "round-trip candidate pool",
            "symbols": [symbol, "005930"],
            "payload": {"ranked": [symbol, "005930"]},
            "as_of": "2026-07-02T01:00:00+00:00",
            "created_by": "claude",
        }
    )

    saved = await service.save(entry)

    listed = await service.list_artifacts(
        market="kr",
        kind="candidate_pool",
        symbol=symbol,
        limit=10,
    )
    assert [row.id for row in listed] == [saved.id]

    fetched = await service.get(saved.id)
    assert fetched is not None
    assert fetched.payload == {"ranked": [symbol, "005930"]}

    fetched_by_uuid = await service.get(str(saved.artifact_uuid))
    assert fetched_by_uuid is not None
    assert fetched_by_uuid.id == saved.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_excludes_stale_unless_include_stale(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    base = now_kst()
    stale_symbol = f"TEST-{uuid4().hex[:8]}"
    fresh_symbol = f"TEST-{uuid4().hex[:8]}"

    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "session_summary",
                "title": "stale summary",
                "symbols": [stale_symbol],
                "as_of": (base - timedelta(hours=2)).isoformat(),
                "valid_until": (base - timedelta(hours=1)).isoformat(),
            }
        )
    )
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "session_summary",
                "title": "fresh summary",
                "symbols": [fresh_symbol],
                "as_of": base.isoformat(),
                "valid_until": (base + timedelta(hours=1)).isoformat(),
            }
        )
    )

    fresh_only = await service.list_artifacts(
        market="kr",
        kind="session_summary",
        limit=10,
    )
    assert {row.title for row in fresh_only} == {"fresh summary"}

    with_stale = await service.list_artifacts(
        market="kr",
        kind="session_summary",
        include_stale=True,
        limit=10,
    )
    assert {row.title for row in with_stale} == {"stale summary", "fresh summary"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_symbol_containment_and_since_filter(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    shared_symbol = f"TEST-{uuid4().hex[:8]}"
    other_symbol = f"TEST-{uuid4().hex[:8]}"
    base = now_kst()

    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "us",
                "kind": "screening_ranking",
                "title": "old ranking",
                "symbols": [shared_symbol],
                "as_of": (base - timedelta(days=2)).isoformat(),
            }
        )
    )
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "us",
                "kind": "screening_ranking",
                "title": "new ranking",
                "symbols": [shared_symbol, other_symbol],
                "as_of": base.isoformat(),
            }
        )
    )

    by_symbol = await service.list_artifacts(
        market="us",
        kind="screening_ranking",
        symbol=other_symbol,
        include_stale=True,
        limit=10,
    )
    assert [row.title for row in by_symbol] == ["new ranking"]

    since_filtered = await service.list_artifacts(
        market="us",
        kind="screening_ranking",
        symbol=shared_symbol,
        since=base - timedelta(days=1),
        include_stale=True,
        limit=10,
    )
    assert [row.title for row in since_filtered] == ["new ranking"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_none_for_missing(db_session: AsyncSession) -> None:
    service = AnalysisArtifactService(db_session)

    assert await service.get(999_999_999) is None
    assert await service.get("not-a-uuid-or-int") is None
