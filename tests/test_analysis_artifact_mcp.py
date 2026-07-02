from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.analysis_artifact_registration import (
    ANALYSIS_ARTIFACT_TOOL_NAMES,
    register_analysis_artifact_tools,
)
from app.mcp_server.tooling.analysis_artifact_tools import (
    analysis_artifact_get,
    analysis_artifact_list,
    analysis_artifact_save,
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


def test_analysis_artifact_tool_names_register() -> None:
    mcp = FakeMCP()

    register_analysis_artifact_tools(mcp)  # type: ignore[arg-type]

    assert ANALYSIS_ARTIFACT_TOOL_NAMES == {
        "analysis_artifact_save",
        "analysis_artifact_list",
        "analysis_artifact_get",
    }
    assert set(mcp.tools) == ANALYSIS_ARTIFACT_TOOL_NAMES


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_list_get_round_trip(db_session: AsyncSession) -> None:
    symbol = f"TEST-{uuid4().hex[:8]}"
    save_response = await analysis_artifact_save(
        market="kr",
        kind="profit_taking_verdicts",
        title="KR profit verdicts",
        symbols=[symbol],
        payload={"verdicts": [{symbol: "hold"}]},
        as_of="2026-07-02T02:00:00+00:00",
        created_by="claude",
        session_label="kr-2026-07-02",
    )

    assert save_response["success"] is True
    saved = save_response["artifact"]
    assert saved["symbols"] == [symbol]
    assert saved["payload"]["verdicts"] == [{symbol: "hold"}]

    list_response = await analysis_artifact_list(
        market="kr",
        kind="profit_taking_verdicts",
        symbol=symbol,
        limit=10,
    )

    assert list_response["success"] is True
    assert list_response["count"] == 1
    assert list_response["artifacts"][0]["artifact_uuid"] == saved["artifact_uuid"]

    get_response = await analysis_artifact_get(saved["id"])

    assert get_response["success"] is True
    assert get_response["artifact"]["payload"] == saved["payload"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_rejects_invalid_kind(db_session: AsyncSession) -> None:
    response = await analysis_artifact_save(
        market="kr",
        kind="bogus_kind",
        title="x",
        as_of="2026-07-02T02:00:00+00:00",
    )

    assert response["success"] is False
    assert response["error"] == "invalid_request"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_not_found_for_missing(
    db_session: AsyncSession,
) -> None:
    response = await analysis_artifact_get(999_999_999)

    assert response == {
        "success": False,
        "error": "not_found",
        "artifact_id": 999_999_999,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_returns_empty_when_no_match(
    db_session: AsyncSession,
) -> None:
    await analysis_artifact_save(
        market="us",
        kind="candidate_pool",
        title="US pool",
        symbols=[f"TEST-{uuid4().hex[:8]}"],
        as_of="2026-07-02T02:00:00+00:00",
    )

    response = await analysis_artifact_list(market="crypto", limit=20)

    assert response["success"] is True
    assert response["count"] == 0
    assert response["artifacts"] == []
