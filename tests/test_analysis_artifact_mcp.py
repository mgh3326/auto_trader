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
        sa.text('TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE')
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text('TRUNCATE TABLE review."analysis_artifacts" RESTART IDENTITY CASCADE')
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
    symbol = f"TEST_{uuid4().hex[:8]}"
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_normalization(db_session: AsyncSession) -> None:
    save_response = await analysis_artifact_save(
        market="us",
        kind="screening_ranking",
        title="US normal",
        symbols=["BRK-B", "BRK/A", "AAPL"],
        as_of="2026-07-02T02:00:00+00:00",
    )
    assert save_response["success"] is True
    saved = save_response["artifact"]
    assert saved["symbols"] == ["BRK.B", "BRK.A", "AAPL"]

    # Dash input saved as dot-format must be findable by dot-format lookup.
    list_response = await analysis_artifact_list(market="us", symbol="BRK.B")
    assert list_response["count"] == 1
    # And a dash-format query normalizes to the same hit.
    list_dash = await analysis_artifact_list(market="us", symbol="BRK-B")
    assert list_dash["count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_rejects_payload_too_large(db_session: AsyncSession) -> None:
    response = await analysis_artifact_save(
        market="kr",
        kind="screening_ranking",
        title="too big",
        payload={"blob": "x" * (101 * 1024)},
        as_of="2026-07-02T02:00:00+00:00",
    )

    assert response["success"] is False
    assert response["error"] == "payload_too_large"
    assert response["size_bytes"] > response["cap_bytes"] == 100 * 1024


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correlation_id_idempotent_upsert(db_session: AsyncSession) -> None:
    correlation_id = f"corr-{uuid4().hex[:12]}"
    first = await analysis_artifact_save(
        market="kr",
        kind="profit_taking_verdicts",
        title="v1",
        payload={"rev": 1},
        as_of="2026-07-02T02:00:00+00:00",
        correlation_id=correlation_id,
    )
    assert first["success"] is True
    assert first["action"] == "created"

    second = await analysis_artifact_save(
        market="kr",
        kind="profit_taking_verdicts",
        title="v2",
        payload={"rev": 2},
        as_of="2026-07-02T03:00:00+00:00",
        correlation_id=correlation_id,
    )
    assert second["success"] is True
    assert second["action"] == "updated"
    assert second["artifact"]["id"] == first["artifact"]["id"]
    assert second["artifact"]["payload"] == {"rev": 2}
    assert second["artifact"]["title"] == "v2"

    listed = await analysis_artifact_list(
        market="kr", correlation_id=correlation_id, limit=10
    )
    assert listed["count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_is_metadata_only(db_session: AsyncSession) -> None:
    symbol = f"TEST_{uuid4().hex[:8]}"
    await analysis_artifact_save(
        market="kr",
        kind="flow_assessment",
        title="meta only",
        symbols=[symbol],
        payload={"big": "가나다" * 100},
        as_of="2026-07-02T02:00:00+00:00",
    )

    response = await analysis_artifact_list(market="kr", symbol=symbol, limit=5)

    assert response["count"] == 1
    row = response["artifacts"][0]
    assert "payload" not in row
    assert row["payload_size_bytes"] > 0
    assert row["is_stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_artifact_flagged_and_filtered(db_session: AsyncSession) -> None:
    symbol = f"TEST_{uuid4().hex[:8]}"
    save_response = await analysis_artifact_save(
        market="kr",
        kind="support_resistance_map",
        title="stale one",
        symbols=[symbol],
        as_of="2026-07-01T02:00:00+00:00",
        valid_until="2026-07-01T06:35:00+09:00",
    )
    saved = save_response["artifact"]
    assert saved["is_stale"] is True

    default_list = await analysis_artifact_list(market="kr", symbol=symbol)
    assert default_list["count"] == 0

    with_stale = await analysis_artifact_list(
        market="kr", symbol=symbol, include_stale=True
    )
    assert with_stale["count"] == 1
    assert with_stale["artifacts"][0]["is_stale"] is True
