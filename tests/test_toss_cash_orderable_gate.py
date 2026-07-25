"""ROB-549: Toss cash ``orderable`` is gated on TOSS_LIVE_ORDER_MUTATIONS_ENABLED.

While Toss live mutations are disabled the cash is reported as balance with
``orderable=0.0`` (reference-only, matching the holdings sellability signal).
Once mutations are armed, the buying power is surfaced as orderable so
``get_available_capital`` and the morning report stop treating Toss cash as
unusable while the toss_live order tools are live.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.mcp_server.tooling import portfolio_cash
from app.services.toss_portfolio_service import TossCashSnapshot

pytestmark = pytest.mark.asyncio


async def _run(monkeypatch, *, mutations_enabled: bool) -> dict:
    monkeypatch.setattr(
        portfolio_cash.settings, "toss_api_enabled", True, raising=False
    )
    monkeypatch.setattr(
        portfolio_cash.settings,
        "toss_live_order_mutations_enabled",
        mutations_enabled,
        raising=False,
    )

    async def _fake_snapshot():
        return TossCashSnapshot(cash_krw=Decimal("1000000"), cash_usd=None, errors=[])

    monkeypatch.setattr(portfolio_cash, "fetch_toss_cash_snapshot", _fake_snapshot)
    # Avoid KIS/Upbit network paths; scope to the toss account only.
    return await portfolio_cash.get_cash_balance_impl(account="toss")


async def test_toss_orderable_zero_when_mutations_disabled(monkeypatch):
    result = await _run(monkeypatch, mutations_enabled=False)
    toss_krw = next(
        a
        for a in result["accounts"]
        if a["broker"] == "toss" and a["currency"] == "KRW"
    )
    assert toss_krw["balance"] == pytest.approx(1000000.0)
    assert toss_krw["orderable"] == pytest.approx(0.0)


async def test_toss_orderable_equals_balance_when_mutations_enabled(monkeypatch):
    result = await _run(monkeypatch, mutations_enabled=True)
    toss_krw = next(
        a
        for a in result["accounts"]
        if a["broker"] == "toss" and a["currency"] == "KRW"
    )
    assert toss_krw["orderable"] == pytest.approx(1000000.0)
