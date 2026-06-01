from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest


def _row(**kw):
    base = {"odno": "US-1", "pdno": "AAPL", "ovrs_excg_cd": "NASD"}
    base.update(kw)
    return base


@pytest.mark.unit
def test_normalize_overseas_row_maps_ft_keys():
    from app.mcp_server.tooling.live_order_evidence import (
        _normalize_overseas_for_classify,
    )

    norm = _normalize_overseas_for_classify(
        _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5")
    )
    assert norm["odno"] == "US-1"
    assert norm["ord_qty"] == "3"
    assert norm["tot_ccld_qty"] == "3"
    assert norm["ccld_unpr"] == "191.5"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_filled():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-1"

    fake_kis = object()
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev,
            "_build_us_exchange_candidates",
            new=AsyncMock(return_value=["NASD"]),
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(
                return_value=(
                    _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5"),
                    "NASD",
                )
            ),
        ),
    ):
        adapter = ev.UsOverseasEvidenceAdapter()
        evidence = await adapter.fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.FILLED
    assert evidence.filled_qty == Decimal("3")
    assert evidence.avg_price == Decimal("191.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_not_found_is_pending():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-MISSING"

    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.PENDING  # fail-closed, no booking
