from datetime import UTC, datetime

import pytest

from app.mcp_server.tooling import operating_briefing as ob


@pytest.mark.asyncio
async def test_briefing_includes_policy_version(monkeypatch):
    async def fake_holdings(**kwargs):
        return {
            "total_positions": 0,
            "summary": {"total_value": 0},
            "accounts": [],
            "errors": [],
        }

    class FakePendingSnapshot:
        orders = []
        as_of = "2026-07-02T16:30:00+09:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        return FakePendingSnapshot()

    async def fake_active_watches(**kwargs):
        return {
            "success": True,
            "count": 0,
            "as_of": datetime.now(tz=UTC).isoformat(),
            "active_watches": [],
        }

    async def fake_latest_report(db, *, market, account_scope):
        return None

    async def fake_recent_session(db, *, market, account_scope, limit):
        return {"count": 0, "entries": []}

    async def fake_recent_analysis(db, *, market):
        return {"count": 0, "artifacts": []}

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "list_active_watches_impl", fake_active_watches)
    monkeypatch.setattr(ob, "_latest_report_summary", fake_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", fake_recent_session)
    monkeypatch.setattr(ob, "_recent_analysis_artifacts", fake_recent_analysis)

    resp = await ob.get_operating_briefing_impl(market="kr")
    assert "policy_version" in resp
    # Compare against the loaded policy document (single source of truth) —
    # a hardcoded version literal broke on every policy bump.
    from app.services.trading_policy_service import load_trading_policy

    document = load_trading_policy()
    assert resp["policy_version"]["version"] == document.version
    assert resp["policy_version"]["content_hash"]
