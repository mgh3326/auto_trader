"""ROB-664 integration smoke for operator session-context read filtering.

Confirms ``SessionContextService.get_recent`` already provides the ordering
(``created_at DESC, id DESC``) and filters (``entry_type``, ``kst_date_from``)
that the new read-only router exposes — no new service code is needed, this
guards against regressions. Mirrors the ROB-663 integration-test scaffold.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_context import OperatorSessionContext
from app.services.session_context import SessionContextService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,  # noqa: ARG001 - xdist DDL lock
):
    await db_session.execute(sa.delete(OperatorSessionContext))
    await db_session.commit()
    yield
    await db_session.execute(sa.delete(OperatorSessionContext))
    await db_session.commit()


async def _add(db_session: AsyncSession, **kw) -> OperatorSessionContext:
    base = {
        "kst_date": date(2026, 7, 3),
        "market": "kr",
        "entry_type": "handoff_note",
        "title": "t",
        "body": "b",
        "refs": {},
        "created_by": "claude",
    }
    base.update(kw)
    row = OperatorSessionContext(**base)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_get_recent_newest_first_and_entry_type_filter(db_session: AsyncSession):
    await _add(db_session, title="old", entry_type="plan")
    await _add(db_session, title="new", entry_type="decision")
    svc = SessionContextService(db_session)

    all_rows = await svc.get_recent()
    assert [r.title for r in all_rows] == ["new", "old"]  # created_at/id DESC

    decisions = await svc.get_recent(entry_type="decision")
    assert [r.title for r in decisions] == ["new"]


@pytest.mark.asyncio
async def test_get_recent_kst_date_from(db_session: AsyncSession):
    await _add(db_session, title="jun", kst_date=date(2026, 6, 30))
    await _add(db_session, title="jul", kst_date=date(2026, 7, 2))
    svc = SessionContextService(db_session)

    rows = await svc.get_recent(kst_date_from=date(2026, 7, 1))
    assert [r.title for r in rows] == ["jul"]
