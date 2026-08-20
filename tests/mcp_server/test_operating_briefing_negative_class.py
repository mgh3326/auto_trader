"""ROB-1283 — the stall guard must reach the run-start briefing.

Why this test matters more than it looks: the operator compliance stamp
(``operator-compliance/v1``) embeds the whole ``get_operating_briefing``
response verbatim under ``briefing_capture.response``. That embedding is the
entire delivery mechanism for AC4 — it is how a stalled recording contract
lands in the session compliance stamp without any operator-repo change. If this
key silently stops being emitted, the guard is gone and nothing else fails.
"""

from datetime import UTC, datetime

import pytest

from app.mcp_server.tooling import operating_briefing as ob
from app.services.trade_journal.negative_class import NegativeClassHealth


def _patch_briefing_sections(monkeypatch):
    """Stub every section except the one under test."""

    async def fake_holdings(**kwargs):
        return {
            "total_positions": 0,
            "summary": {"total_value": 0},
            "accounts": [],
            "errors": [],
        }

    class FakePendingSnapshot:
        orders = []
        as_of = "2026-08-20T09:00:00+09:00"
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


@pytest.mark.asyncio
async def test_stalled_recording_is_visible_at_run_start(monkeypatch):
    _patch_briefing_sections(monkeypatch)

    async def fake_health(db, *, market, now):
        return NegativeClassHealth(
            status="stalled",
            market=market,
            last_recorded_at="2026-06-15T00:00:00+00:00",
            last_source="report_item",
            stale_days=66,
            stall_threshold_days=7,
            forecast_last_at=None,
            report_item_last_at="2026-06-15T00:00:00+00:00",
            gap={
                "starts_at": "2026-06-15T00:00:00+00:00",
                "ends_at": None,
                "open": True,
                "days": 66,
                "reason": "test",
                "backfilled": False,
            },
            notes=["stalled"],
        )

    monkeypatch.setattr(ob, "load_negative_class_health", fake_health)

    resp = await ob.get_operating_briefing_impl(market="kr")

    section = resp["negative_class_recording"]
    assert section["status"] == "stalled"
    assert section["stale_days"] == 66
    # The gap travels with it and is never presented as backfilled.
    assert section["gap"]["open"] is True
    assert section["gap"]["backfilled"] is False


@pytest.mark.asyncio
async def test_healthy_recording_is_reported_as_ok(monkeypatch):
    _patch_briefing_sections(monkeypatch)

    async def fake_health(db, *, market, now):
        return NegativeClassHealth(
            status="ok",
            market=market,
            last_recorded_at="2026-08-20T00:00:00+00:00",
            last_source="forecast",
            stale_days=0,
            stall_threshold_days=7,
            forecast_last_at="2026-08-20T00:00:00+00:00",
            report_item_last_at=None,
            gap=None,
        )

    monkeypatch.setattr(ob, "load_negative_class_health", fake_health)

    resp = await ob.get_operating_briefing_impl(market="kr")
    assert resp["negative_class_recording"]["status"] == "ok"
    assert resp["negative_class_recording"]["gap"] is None


@pytest.mark.asyncio
async def test_probe_failure_is_reported_not_swallowed_into_ok(monkeypatch):
    """Fail-open must not mean fail-silent.

    The briefing still returns (it is the run-start surface), but a broken probe
    reports ``unavailable`` with a reason — never a clean ``ok`` that would be
    read as "recording is fine".
    """
    _patch_briefing_sections(monkeypatch)

    async def boom(db, *, market, now):
        raise RuntimeError("column missing")

    monkeypatch.setattr(ob, "load_negative_class_health", boom)

    resp = await ob.get_operating_briefing_impl(market="kr")

    section = resp["negative_class_recording"]
    assert section["status"] == "unavailable"
    assert section["status"] != "ok"
    assert "negative_class_recording_failed" in section["unavailable_reason"]
    # The rest of the briefing is unaffected.
    assert resp["success"] is True
