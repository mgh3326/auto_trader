# tests/mcp_server/test_kis_live_reconcile_expiry.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)


def _pending_evidence():
    return FillEvidence(
        verdict=FillVerdict.PENDING, filled_qty=None, avg_price=None,
        category=None, reason_code="pending", detail="",
    )


def _row():
    return SimpleNamespace(
        id=1, order_no="0011001100", symbol="005930", side="buy",
        instrument_type="equity_kr", fee=0, currency="KRW",
    )


@pytest.mark.asyncio
async def test_pending_after_close_marks_expired_when_applied():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value=mod.DATA_STATE_MARKET_CLOSED), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=False)
    assert out["verdict"] == "expired"
    assert out["action"] == "marked_expired"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "expired"


@pytest.mark.asyncio
async def test_pending_after_close_dry_run_does_not_write():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value=mod.DATA_STATE_MARKET_CLOSED), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=True)
    assert out["verdict"] == "expired"
    assert out["action"] == "would_mark_expired"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_while_market_open_stays_noop_pending():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value="fresh"), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=False)
    assert out["verdict"] == "pending"
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()
