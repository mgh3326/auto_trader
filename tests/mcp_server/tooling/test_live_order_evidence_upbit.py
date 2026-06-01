import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch


def _detail(**kw):
    base = {"uuid": "U-1", "state": "wait", "executed_volume": "0",
            "remaining_volume": "1", "avg_price": None, "price": "100"}
    base.update(kw)
    return base


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_wait_is_pending():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=_detail())):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.PENDING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_done_filled():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="done", executed_volume="1", remaining_volume="0", avg_price="101.5")
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.FILLED
    assert e.filled_qty == Decimal("1")
    assert e.avg_price == Decimal("101.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_partial():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="wait", executed_volume="0.4", remaining_volume="0.6", avg_price="100")
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.PARTIAL
    assert e.filled_qty == Decimal("0.4")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_cancelled_zero_fill_is_none():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="cancel", executed_volume="0", remaining_volume="0", avg_price=None)
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.NONE
