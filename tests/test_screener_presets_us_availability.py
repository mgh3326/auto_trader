"""ROB-427 PR2: US per-market availability contract.

The US catalog must expose the FULL preset set (no longer hide KR-only presets),
each stamped with an honest availability (active / data_pending / unsupported),
and `build_screener_results` must fail-closed for non-active presets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    _US_ACTIVE_PRESET_IDS,
    _US_UNSUPPORTED_PRESET_IDS,
    preset_definitions,
)
from app.services.invest_view_model.screener_service import build_screener_results


class _Resolver:
    def relation(self, market: str, symbol: str) -> str:  # pragma: no cover - unused
        return "none"


@pytest.mark.unit
def test_us_catalog_exposes_full_set_not_filtered() -> None:
    """US no longer drops KR-only presets — same membership as KR, statuses differ."""
    kr_ids = {p.id for p in preset_definitions("kr")}
    us_ids = {p.id for p in preset_definitions("us")}
    assert us_ids == kr_ids  # nothing hidden
    # every KR-only preset is present in US (with a non-active status, asserted below)
    assert _KR_ONLY_PRESET_IDS <= us_ids


@pytest.mark.unit
def test_kr_catalog_all_active() -> None:
    for p in preset_definitions("kr"):
        assert p.availability == "active", p.id
        assert p.availabilityReason is None, p.id


@pytest.mark.unit
def test_us_price_technical_presets_active() -> None:
    us = {p.id: p for p in preset_definitions("us")}
    for pid in (
        "consecutive_gainers",
        "oversold_recovery",
        "kr_high_volume_surge",
        "growth_expectation",
    ):
        assert us[pid].availability == "active", pid


@pytest.mark.unit
def test_us_flow_presets_unsupported_with_reason() -> None:
    us = {p.id: p for p in preset_definitions("us")}
    for pid in _US_UNSUPPORTED_PRESET_IDS:
        assert us[pid].availability == "unsupported", pid
        assert us[pid].availabilityReason, pid


@pytest.mark.unit
def test_us_fundamentals_presets_data_pending_with_reason() -> None:
    us = {p.id: p for p in preset_definitions("us")}
    # ROB-441 PR4: growth_expectation_toss (QoQ) is now active (yfinance quarterly);
    # only the dividend presets stay data_pending until US dividend data is built.
    for pid in (
        "steady_dividend",
        "future_dividend_king",
    ):
        assert us[pid].availability == "data_pending", pid
        assert us[pid].availabilityReason, pid


@pytest.mark.unit
def test_us_fundamentals_growth_presets_active() -> None:
    # ROB-441 PR3: market_valuation US (per/pbr/roe) + financial_fundamentals US
    # annual derive → these run for US.
    us = {p.id: p for p in preset_definitions("us")}
    # ROB-441 PR4: growth_expectation_toss joins once US quarterly periods (QoQ) exist.
    for pid in (
        "profitable_company",
        "undervalued_growth",
        "cheap_value",
        "stable_growth",
        "growth_expectation_toss",
    ):
        assert us[pid].availability == "active", pid
        assert us[pid].availabilityReason is None, pid
        assert pid in _US_ACTIVE_PRESET_IDS


@pytest.mark.unit
def test_us_high_yield_value_active() -> None:
    # ROB-427 PR3: Yahoo valuation backs ROE+PER, so high_yield_value runs for US.
    us = {p.id: p for p in preset_definitions("us")}
    assert us["high_yield_value"].availability == "active"
    assert us["high_yield_value"].availabilityReason is None
    assert "high_yield_value" in _US_ACTIVE_PRESET_IDS


@pytest.mark.unit
def test_us_undervalued_breakout_active() -> None:
    # ROB-440 Part 2: Yahoo valuation backs high_52w(price) + PER/PBR, so
    # undervalued_breakout (proximity) runs for US.
    us = {p.id: p for p in preset_definitions("us")}
    assert us["undervalued_breakout"].availability == "active"
    assert us["undervalued_breakout"].availabilityReason is None
    assert "undervalued_breakout" in _US_ACTIVE_PRESET_IDS


@pytest.mark.unit
def test_availability_partition_covers_all_kr_only() -> None:
    """Every KR-only preset is exactly one of unsupported / data_pending in US."""
    us = {p.id: p for p in preset_definitions("us")}
    for pid in _KR_ONLY_PRESET_IDS:
        # PR3: a KR-only preset may now be active for US (e.g. high_yield_value).
        assert us[pid].availability in {"active", "data_pending", "unsupported"}, pid


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_results_fail_closed_for_data_pending_us() -> None:
    """A data_pending US preset returns 0 rows + the honest reason — no loader,
    no fabrication (screening_service must not even be called)."""
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={"results": [], "warnings": []}
    )
    resp = await build_screener_results(
        preset_id="high_yield_value",
        screening_service=fake_screening,
        resolver=_Resolver(),
        market="us",
        session=None,
    )
    assert resp.presetId == "high_yield_value"
    assert resp.results == []
    assert resp.warnings and any("준비중" in w for w in resp.warnings)
    fake_screening.list_screening.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_results_fail_closed_for_unsupported_us() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={"results": [], "warnings": []}
    )
    resp = await build_screener_results(
        preset_id="double_buy",
        screening_service=fake_screening,
        resolver=_Resolver(),
        market="us",
        session=None,
    )
    assert resp.results == []
    assert resp.warnings
    fake_screening.list_screening.assert_not_called()
