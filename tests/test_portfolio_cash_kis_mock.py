from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import portfolio_cash


@pytest.mark.asyncio
async def test_cash_balance_mock_uses_domestic_cash_not_integrated_margin(monkeypatch):
    fake_kis = MagicMock()
    fake_kis.inquire_integrated_margin = AsyncMock(
        side_effect=AssertionError("must not call integrated margin in mock"),
    )
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": 1000.0,
            "stck_cash_ord_psbl_amt": 900.0,
            "raw": {},
        },
    )
    fake_kis.inquire_overseas_margin = AsyncMock(
        side_effect=RuntimeError("mock unsupported"),
    )
    fake_kis.inquire_korea_orders = AsyncMock(return_value=[])

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    accounts = {a["account"]: a for a in result["accounts"]}
    assert "kis_domestic" in accounts
    assert accounts["kis_domestic"]["balance"] == pytest.approx(1000.0)
    assert accounts["kis_domestic"]["orderable"] == pytest.approx(900.0)
    # Overseas should be reported as a mock_unsupported error, not silent zero.
    assert any(
        e.get("market") == "us" and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    )


@pytest.mark.asyncio
async def test_cash_balance_mock_pending_buy_tolerates_egw02006(monkeypatch):
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": 1000.0,
            "stck_cash_ord_psbl_amt": 1000.0,
            "raw": {},
        },
    )
    fake_kis.inquire_overseas_margin = AsyncMock(
        side_effect=RuntimeError("mock unsupported"),
    )
    fake_kis.inquire_korea_orders = AsyncMock(
        side_effect=RuntimeError("EGW02006 모의투자 TR 이 아닙니다"),
    )

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    # Pending deduction failed -> orderable falls back to raw orderable
    # (not zero, not crash).
    accounts = {a["account"]: a for a in result["accounts"]}
    assert accounts["kis_domestic"]["orderable"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_cash_balance_mock_pending_buy_records_mock_unsupported(monkeypatch):
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": 1000.0,
            "stck_cash_ord_psbl_amt": 1000.0,
            "raw": {},
        },
    )
    fake_kis.inquire_overseas_margin = AsyncMock(
        side_effect=RuntimeError("mock unsupported"),
    )
    fake_kis.inquire_korea_orders = AsyncMock(
        side_effect=RuntimeError(
            "KIS domestic pending-orders inquiry (TTTC8036R) is not "
            "available in mock mode."
        ),
    )

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    assert any(
        e.get("source") == "kis"
        and e.get("market") == "kr"
        and e.get("mock_unsupported") is True
        for e in result["errors"]
    ), result["errors"]
