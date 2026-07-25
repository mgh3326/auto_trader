from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.session_context_registration import (
    SESSION_CONTEXT_TOOL_NAMES,
    register_session_context_tools,
)
from app.mcp_server.tooling.session_context_tools import (
    session_context_append,
    session_context_get_recent,
)


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


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


def test_session_context_tool_names_register() -> None:
    mcp = FakeMCP()

    register_session_context_tools(mcp)  # type: ignore[arg-type]

    assert SESSION_CONTEXT_TOOL_NAMES == {
        "session_context_append",
        "session_context_get_recent",
    }
    assert set(mcp.tools) == SESSION_CONTEXT_TOOL_NAMES


@pytest.mark.asyncio
async def test_append_and_get_recent_round_trip(db_session: AsyncSession) -> None:
    append_response = await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "kr",
                "account_scope": "kis_live",
                "entry_type": "rejected_candidate",
                "title": "반도체 후보 제외",
                "body": "target_exceeded 조건으로 신규 추가 없음",
                "refs": {"symbols": ["005930", "000660"]},
                "created_by": "claude",
                "session_label": "kr-live-2026-06-11",
            }
        ]
    )

    assert append_response["success"] is True
    assert append_response["count"] == 1
    assert append_response["entries"][0]["refs"]["symbols"] == ["005930", "000660"]

    recent_response = await session_context_get_recent(
        market="kr",
        account_scope="kis_live",
        kst_date_from="2026-06-11",
        entry_type="rejected_candidate",
        limit=20,
    )

    assert recent_response["success"] is True
    assert recent_response["count"] == 1
    assert recent_response["entries"][0]["title"] == "반도체 후보 제외"
    assert recent_response["filters"]["market"] == "kr"


@pytest.mark.asyncio
async def test_append_rejects_empty_entries(db_session: AsyncSession) -> None:
    response = await session_context_append(entries=[])

    assert response == {
        "success": False,
        "error": "empty_entries",
        "hint": "Pass one or more session context entries.",
    }


@pytest.mark.asyncio
async def test_get_recent_returns_empty_list_when_no_match(
    db_session: AsyncSession,
) -> None:
    await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "us",
                "entry_type": "handoff_note",
                "title": "US note",
                "body": "not KR",
            }
        ]
    )

    response = await session_context_get_recent(market="kr", limit=20)

    assert response["success"] is True
    assert response["count"] == 0
    assert response["entries"] == []


@pytest.mark.asyncio
async def test_append_accepts_codex_created_by(db_session: AsyncSession) -> None:
    response = await session_context_append(
        entries=[
            {
                "kst_date": "2026-07-06",
                "market": "kr",
                "entry_type": "handoff_note",
                "title": "Codex handoff",
                "body": "analysis_readonly smoke",
                "created_by": "codex",
            }
        ]
    )

    assert response["success"] is True
    assert response["entries"][0]["created_by"] == "codex"
