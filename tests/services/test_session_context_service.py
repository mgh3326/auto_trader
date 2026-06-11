from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.session_context import SessionContextAppendEntry
from app.services.session_context import SessionContextService


@pytest_asyncio.fixture(autouse=True)
async def _clean_session_context(db_session: AsyncSession):
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" RESTART IDENTITY CASCADE'
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" RESTART IDENTITY CASCADE'
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_append_entries_defaults_kst_date_and_preserves_refs(
    db_session: AsyncSession,
) -> None:
    service = SessionContextService(db_session)
    entries = [
        SessionContextAppendEntry.model_validate(
            {
                "market": "kr",
                "account_scope": "kis_live",
                "entry_type": "deferred",
                "title": "DB 매도 보류",
                "body": "익절 조건만 허용되어 매도 제외",
                "refs": {"symbols": ["DB"], "journal_id": 11},
                "created_by": "claude",
            }
        )
    ]

    rows = await service.append_entries(entries)

    assert len(rows) == 1
    assert rows[0].kst_date is not None
    assert rows[0].refs == {"symbols": ["DB"], "journal_id": 11}


@pytest.mark.asyncio
async def test_get_recent_filters_and_orders_newest_first(
    db_session: AsyncSession,
) -> None:
    service = SessionContextService(db_session)
    await service.append_entries(
        [
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-10",
                    "market": "kr",
                    "entry_type": "next_action",
                    "title": "old",
                    "body": "older",
                }
            ),
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "kr",
                    "account_scope": "kis_live",
                    "entry_type": "next_action",
                    "title": "new",
                    "body": "newer",
                }
            ),
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "us",
                    "entry_type": "next_action",
                    "title": "us",
                    "body": "ignored",
                }
            ),
        ]
    )

    rows = await service.get_recent(
        market="kr",
        account_scope="kis_live",
        kst_date_from=date(2026, 6, 11),
        entry_type="next_action",
        limit=10,
    )

    assert [row.title for row in rows] == ["new"]


@pytest.mark.asyncio
async def test_get_recent_clamps_limit(db_session: AsyncSession) -> None:
    service = SessionContextService(db_session)
    await service.append_entries(
        [
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "kr",
                    "entry_type": "handoff_note",
                    "title": f"note-{index}",
                    "body": "body",
                }
            )
            for index in range(3)
        ]
    )

    rows = await service.get_recent(limit=1_000)

    assert len(rows) == 3
