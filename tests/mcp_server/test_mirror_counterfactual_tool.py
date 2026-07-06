# tests/mcp_server/test_mirror_counterfactual_tool.py
import pytest


@pytest.mark.asyncio
async def test_mirror_counterfactual_tool_registered():
    from app.mcp_server.tooling.mirror_counterfactual_registration import (
        MIRROR_COUNTERFACTUAL_TOOL_NAMES,
        register_mirror_counterfactual_tools,
    )

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name, description):
            def deco(fn):
                self.tools[name] = fn
                return fn

            return deco

    mcp = FakeMCP()
    register_mirror_counterfactual_tools(mcp)
    assert MIRROR_COUNTERFACTUAL_TOOL_NAMES == {"kis_mock_mirror_execute_report"}
    assert "kis_mock_mirror_execute_report" in mcp.tools


@pytest.mark.asyncio
async def test_mirror_counterfactual_tool_delegates(monkeypatch):
    from app.mcp_server.tooling import mirror_counterfactual_tools as tool

    async def fake_execute(db, **kwargs):
        return {"success": True, "planned_count": 1, "dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(tool, "execute_mirror_for_report", fake_execute)
    result = await tool.kis_mock_mirror_execute_report(
        report_uuid="11111111-1111-1111-1111-111111111111",
        dry_run=True,
    )
    assert result["success"] is True
    assert result["planned_count"] == 1
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_mirror_counterfactual_tool_commits_apply_results(monkeypatch):
    from app.mcp_server.tooling import mirror_counterfactual_tools as tool

    class FakeSession:
        def __init__(self):
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            self.commits += 1

    fake_db = FakeSession()

    async def fake_execute(db, **kwargs):
        assert db is fake_db
        assert kwargs["dry_run"] is False
        return {"success": True, "submitted_count": 1, "dry_run": False}

    monkeypatch.setattr(tool, "AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr(tool, "execute_mirror_for_report", fake_execute)

    result = await tool.kis_mock_mirror_execute_report(
        report_uuid="11111111-1111-1111-1111-111111111111",
        dry_run=False,
    )

    assert result["success"] is True
    assert fake_db.commits == 1
