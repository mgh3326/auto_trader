"""Regression tests: NULL ``account_scope`` rows must reach the operating briefing.

An operator instruction stored with ``account_scope=NULL`` means "market-wide".
``SessionContextService.get_recent`` filtered scope by strict equality, and
``get_operating_briefing`` always resolves a concrete scope
(``_default_account_scope``), so those rows were invisible to the run-start
briefing forever. Observed three times in production (twice on crypto).

The fix is an opt-in ``include_market_wide`` flag consumed only by the
briefing. These tests pin BOTH halves: the briefing now sees NULL rows, and the
two other ``get_recent`` consumers (HTTP router, ``session_context_get_recent``
MCP tool) keep strict equality.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.operating_briefing import _recent_session_context
from app.mcp_server.tooling.session_context_tools import session_context_get_recent
from app.models.session_context import OperatorSessionContext
from app.services.session_context import SessionContextService

_TRUNCATE = 'TRUNCATE TABLE review."operator_session_context" RESTART IDENTITY CASCADE'


@pytest_asyncio.fixture(autouse=True)
async def _clean_session_context(db_session: AsyncSession):
    await db_session.execute(sa.text(_TRUNCATE))
    await db_session.commit()
    yield
    await db_session.execute(sa.text(_TRUNCATE))
    await db_session.commit()


async def _insert(
    db: AsyncSession,
    *,
    market: str,
    account_scope: str | None,
    title: str,
) -> None:
    db.add(
        OperatorSessionContext(
            kst_date=date(2026, 8, 24),
            market=market,
            account_scope=account_scope,
            entry_type="handoff_note",
            title=title,
            body="body",
            refs={},
            created_by="operator",
        )
    )
    await db.commit()


def _titles(payload: dict) -> set[str]:
    return {entry["title"] for entry in payload["entries"]}


@pytest.mark.asyncio
async def test_briefing_includes_market_wide_null_scope_row_kr(
    db_session: AsyncSession,
) -> None:
    """market=kr + account_scope=NULL must appear in the kis_live briefing."""
    await _insert(
        db_session, market="kr", account_scope=None, title="KR 시장 전체 지시"
    )
    await _insert(
        db_session, market="kr", account_scope="kis_live", title="KR kis_live 지시"
    )

    payload = await _recent_session_context(
        db_session, market="kr", account_scope="kis_live", limit=10
    )

    assert _titles(payload) == {"KR 시장 전체 지시", "KR kis_live 지시"}
    assert payload["count"] == 2


@pytest.mark.asyncio
async def test_briefing_includes_market_wide_null_scope_row_crypto(
    db_session: AsyncSession,
) -> None:
    """Two of the three observed recurrences were crypto — pin it explicitly."""
    await _insert(
        db_session, market="crypto", account_scope=None, title="crypto 시장 전체 지시"
    )

    payload = await _recent_session_context(
        db_session, market="crypto", account_scope="upbit_live", limit=10
    )

    assert _titles(payload) == {"crypto 시장 전체 지시"}


@pytest.mark.asyncio
async def test_briefing_does_not_leak_other_market_null_scope_rows(
    db_session: AsyncSession,
) -> None:
    """Widening is scope-only: the market filter still holds."""
    await _insert(
        db_session, market="us", account_scope=None, title="US 시장 전체 지시"
    )
    await _insert(
        db_session, market="kr", account_scope=None, title="KR 시장 전체 지시"
    )

    payload = await _recent_session_context(
        db_session, market="kr", account_scope="kis_live", limit=10
    )

    assert _titles(payload) == {"KR 시장 전체 지시"}
    assert "US 시장 전체 지시" not in _titles(payload)


@pytest.mark.asyncio
async def test_service_default_keeps_strict_account_scope_equality(
    db_session: AsyncSession,
) -> None:
    """``include_market_wide`` defaults to False — NULL rows stay excluded."""
    await _insert(db_session, market="kr", account_scope=None, title="null-scope")
    await _insert(db_session, market="kr", account_scope="kis_live", title="kis_live")

    service = SessionContextService(db_session)
    rows = await service.get_recent(market="kr", account_scope="kis_live")

    assert {row.title for row in rows} == {"kis_live"}


@pytest.mark.asyncio
async def test_unfiltered_scope_call_is_unaffected_by_the_flag(
    db_session: AsyncSession,
) -> None:
    """``account_scope=None`` applies no scope filter — the flag is a no-op."""
    await _insert(db_session, market="kr", account_scope=None, title="null-scope")
    await _insert(db_session, market="kr", account_scope="kis_live", title="kis_live")
    await _insert(db_session, market="kr", account_scope="kis_mock", title="kis_mock")

    service = SessionContextService(db_session)
    strict = await service.get_recent(market="kr", account_scope=None)
    widened = await service.get_recent(
        market="kr", account_scope=None, include_market_wide=True
    )

    assert {row.title for row in strict} == {"null-scope", "kis_live", "kis_mock"}
    assert [row.title for row in strict] == [row.title for row in widened]


@pytest.mark.asyncio
async def test_mcp_get_recent_tool_keeps_strict_account_scope_equality(
    db_session: AsyncSession,
) -> None:
    await _insert(db_session, market="kr", account_scope=None, title="null-scope")
    await _insert(db_session, market="kr", account_scope="kis_live", title="kis_live")

    response = await session_context_get_recent(
        market="kr", account_scope="kis_live", limit=20
    )

    assert response["success"] is True
    assert {entry["title"] for entry in response["entries"]} == {"kis_live"}


@pytest.mark.asyncio
async def test_http_router_keeps_strict_account_scope_equality(
    db_session: AsyncSession,
) -> None:
    from app.core.db import get_db
    from app.routers import invest_session_context
    from app.routers.dependencies import get_authenticated_user

    await _insert(db_session, market="kr", account_scope=None, title="null-scope")
    await _insert(db_session, market="kr", account_scope="kis_live", title="kis_live")

    app = FastAPI()
    app.include_router(invest_session_context.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/trading/api/invest/session-context/recent",
            params={"market": "kr", "account_scope": "kis_live", "limit": 20},
        )

    assert response.status_code == 200
    body = response.json()
    assert {entry["title"] for entry in body["entries"]} == {"kis_live"}
