"""ROB-438: default-off recurring schedulers for valuation + investor-flow snapshots.

Mirrors the invest_screener double-gate (ROB-281): schedule flag registers the cron
(default off → [] → not registered), commit flag gates DB writes (default off →
dry-run-on-cron). Scheduled tasks also XKRX holiday-gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.tasks import investor_flow_snapshot_tasks as fl
from app.tasks import market_valuation_snapshot_tasks as mv

# --- schedule-label gating (default off) --------------------------------------


@pytest.mark.unit
def test_valuation_schedule_labels_gated_by_flag() -> None:
    with patch.object(mv.settings, "market_valuation_schedule_enabled", False):
        assert mv._kr_valuation_schedule("30 16 * * 1-5") == []
    with patch.object(mv.settings, "market_valuation_schedule_enabled", True):
        assert mv._kr_valuation_schedule("30 16 * * 1-5") == [
            {"cron": "30 16 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]


@pytest.mark.unit
def test_flow_schedule_labels_gated_by_flag() -> None:
    with patch.object(fl.settings, "investor_flow_schedule_enabled", False):
        assert fl._kr_flow_schedule("40 16 * * 1-5") == []
    with patch.object(fl.settings, "investor_flow_schedule_enabled", True):
        assert fl._kr_flow_schedule("40 16 * * 1-5") == [
            {"cron": "40 16 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]


# --- scheduled body: holiday-skip + commit gating -----------------------------


def _patch_session(monkeypatch, is_session: bool) -> None:
    import app.tasks.invest_screener_snapshot_tasks as iss

    monkeypatch.setattr(iss, "is_market_session_today", lambda market, **_k: is_session)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduled_valuation_skips_holiday(monkeypatch) -> None:
    _patch_session(monkeypatch, False)
    out = await mv.scheduled_kr_market_valuation()
    assert out["status"] == "skipped_holiday"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduled_flow_skips_holiday(monkeypatch) -> None:
    _patch_session(monkeypatch, False)
    out = await fl.scheduled_kr_investor_flow()
    assert out["status"] == "skipped_holiday"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("commit_flag", [False, True])
async def test_scheduled_valuation_commit_gated(monkeypatch, commit_flag) -> None:
    _patch_session(monkeypatch, True)
    captured: dict = {}

    async def _fake_build(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"committed": kwargs.get("commit")}

    monkeypatch.setattr(mv, "build_market_valuation_snapshots", _fake_build)
    with patch.object(
        mv.settings, "market_valuation_snapshots_commit_enabled", commit_flag
    ):
        await mv.scheduled_kr_market_valuation()
    assert captured["commit"] is commit_flag
    assert captured["market"] == "kr"
    assert captured["all_symbols"] is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("commit_flag", [False, True])
async def test_scheduled_flow_commit_gated(monkeypatch, commit_flag) -> None:
    _patch_session(monkeypatch, True)
    captured: dict = {}

    async def _fake_build(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"committed": kwargs.get("commit")}

    monkeypatch.setattr(fl, "build_investor_flow_snapshots", _fake_build)
    with patch.object(
        fl.settings, "investor_flow_snapshots_commit_enabled", commit_flag
    ):
        await fl.scheduled_kr_investor_flow()
    assert captured["commit"] is commit_flag
    assert captured["market"] == "kr"
    assert captured["all_symbols"] is True
