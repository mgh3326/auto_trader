from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.schemas.analysis_artifact import AnalysisArtifactSave
from app.services.analysis_artifact import (
    AnalysisArtifactService,
    compute_content_hash,
    default_valid_until,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_analysis_artifacts(db_session: AsyncSession):
    await db_session.execute(
        sa.text('TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE')
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text('TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE')
    )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_list_get_round_trip(db_session: AsyncSession) -> None:
    service = AnalysisArtifactService(db_session)
    symbol = f"TEST_{uuid4().hex[:8]}"
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

    saved, action = await service.save(entry)
    assert action == "created"

    listed = await service.list_artifacts(
        market="kr",
        kind="candidate_pool",
        symbol=symbol,
        include_stale=True,
        limit=10,
    )
    assert [row.id for row in listed] == [saved.id]

    import sqlalchemy as sa

    service._session.expunge_all()
    listed = await service.list_artifacts(
        market="kr", kind="candidate_pool", symbol=symbol, include_stale=True, limit=10
    )
    assert [row.id for row in listed] == [saved.id]
    assert "payload" in sa.inspect(listed[0]).unloaded

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
    stale_symbol = f"TEST_{uuid4().hex[:8]}"
    fresh_symbol = f"TEST_{uuid4().hex[:8]}"

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
    shared_symbol = f"TEST_{uuid4().hex[:8]}"
    other_symbol = f"TEST_{uuid4().hex[:8]}"
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


@pytest.mark.unit
def test_default_valid_until_is_per_kind_and_never_none() -> None:
    as_of = datetime(2026, 7, 2, 3, 0, 0, tzinfo=KST)
    # Price/screen-derived kind → end of the as_of KST day.
    daily = default_valid_until("screening_ranking", as_of)
    assert daily == datetime(2026, 7, 2, 23, 59, 59, tzinfo=KST)
    # Session summary / briefing → end of the next KST day.
    summary = default_valid_until("session_summary", as_of)
    assert summary == datetime(2026, 7, 3, 23, 59, 59, tzinfo=KST)
    # Unknown kind falls back to end of the as_of day (never None).
    unknown = default_valid_until("mystery", as_of)
    assert unknown == datetime(2026, 7, 2, 23, 59, 59, tzinfo=KST)


@pytest.mark.unit
def test_content_hash_is_order_insensitive() -> None:
    a = compute_content_hash({"x": 1, "y": [1, 2]})
    b = compute_content_hash({"y": [1, 2], "x": 1})
    assert a == b
    assert a != compute_content_hash({"x": 2, "y": [1, 2]})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_applies_per_kind_default_ttl_when_omitted(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    as_of = datetime(2026, 7, 2, 3, 0, 0, tzinfo=KST)
    saved, _ = await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "flow_assessment",
                "title": "ttl default",
                "symbols": [f"TEST_{uuid4().hex[:8]}"],
                "as_of": as_of.isoformat(),
            }
        )
    )
    assert saved.valid_until is not None
    assert saved.valid_until == default_valid_until("flow_assessment", as_of)
    assert saved.content_hash == compute_content_hash({})
    assert saved.version == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correlation_save_unchanged_then_bumped(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    corr = f"corr-{uuid4().hex[:12]}"
    base = {
        "market": "kr",
        "kind": "profit_taking_verdicts",
        "title": "v1",
        "as_of": "2026-07-02T02:00:00+00:00",
        "correlation_id": corr,
        "payload": {"a": 1},
    }

    first, action1 = await service.save(AnalysisArtifactSave.model_validate(base))
    assert action1 == "created"
    assert first.version == 1

    # Same payload (different title) → unchanged, version preserved, no write.
    same, action2 = await service.save(
        AnalysisArtifactSave.model_validate({**base, "title": "v1-retry"})
    )
    assert action2 == "unchanged"
    assert same.id == first.id
    assert same.version == 1
    assert same.title == "v1"  # no-op: stored title untouched

    # Changed payload → updated + version bump.
    changed, action3 = await service.save(
        AnalysisArtifactSave.model_validate(
            {**base, "title": "v2", "payload": {"a": 2}}
        )
    )
    assert action3 == "updated"
    assert changed.id == first.id
    assert changed.version == 2
    assert changed.payload == {"a": 2}
    assert changed.content_hash == compute_content_hash({"a": 2})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_artifacts_for_symbols_overlap_and_staleness(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    sym = f"TEST_{uuid4().hex[:8]}"
    other = f"TEST_{uuid4().hex[:8]}"
    base = now_kst()

    fresh, _ = await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "support_resistance_map",
                "title": "fresh sr",
                "symbols": [sym],
                "as_of": base.isoformat(),
                "valid_until": (base + timedelta(hours=6)).isoformat(),
            }
        )
    )
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "support_resistance_map",
                "title": "stale sr",
                "symbols": [sym],
                "as_of": (base - timedelta(hours=2)).isoformat(),
                "valid_until": (base - timedelta(hours=1)).isoformat(),
            }
        )
    )

    hits = await service.fresh_artifacts_for_symbols(symbols=[sym], market="kr")
    assert [row.id for row in hits] == [fresh.id]

    # A symbol with no artifact returns nothing.
    assert await service.fresh_artifacts_for_symbols(symbols=[other]) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_none_for_missing(db_session: AsyncSession) -> None:
    service = AnalysisArtifactService(db_session)

    assert await service.get(999_999_999) is None
    assert await service.get("not-a-uuid-or-int") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_artifacts_filters_by_correlation_ids(
    db_session: AsyncSession,
) -> None:
    service = AnalysisArtifactService(db_session)
    sym = f"ZZ{uuid4().hex[:6].upper()}"
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "us",
                "kind": "screening_ranking",
                "title": "corr-a",
                "symbols": [sym],
                "payload": {"a": 1},
                "as_of": "2026-07-02T01:00:00+00:00",
                "created_by": "claude",
                "correlation_id": "live:kis_live:aaa",
            }
        )
    )
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "us",
                "kind": "screening_ranking",
                "title": "corr-b",
                "symbols": [sym],
                "payload": {"b": 2},
                "as_of": "2026-07-02T01:00:00+00:00",
                "created_by": "claude",
                "correlation_id": "live:kis_live:bbb",
            }
        )
    )
    await service.save(
        AnalysisArtifactSave.model_validate(
            {
                "market": "us",
                "kind": "screening_ranking",
                "title": "corr-c",
                "symbols": [sym],
                "payload": {"c": 3},
                "as_of": "2026-07-02T01:00:00+00:00",
                "created_by": "claude",
                "correlation_id": "live:kis_live:ccc",
            }
        )
    )
    rows = await service.list_artifacts(
        correlation_ids=["live:kis_live:aaa", "live:kis_live:bbb"],
        include_stale=True,
    )
    assert {row.correlation_id for row in rows} == {
        "live:kis_live:aaa",
        "live:kis_live:bbb",
    }
