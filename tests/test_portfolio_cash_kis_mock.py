from unittest.mock import AsyncMock, MagicMock

import httpx
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
async def test_cash_balance_mock_orderable_is_raw_without_pending_deduction(
    monkeypatch,
):
    """ROB-596: mock KR orderable 는 inquire_domestic_cash_balance 의 raw 값을
    그대로 쓴다. (과거엔 미체결 매수 차감을 시도했으나 mock 은 TTTC8036R 미지원이라
    항상 raw 로 폴백했고, double-count 제거로 차감 자체를 하지 않는다.)"""
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
    # 더 이상 미체결 매수 조회를 하지 않으므로 호출되면 안 된다.
    fake_kis.inquire_korea_orders = AsyncMock(
        side_effect=AssertionError("inquire_korea_orders must not be called"),
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

    accounts = {a["account"]: a for a in result["accounts"]}
    assert accounts["kis_domestic"]["orderable"] == pytest.approx(1000.0)
    fake_kis.inquire_korea_orders.assert_not_called()


@pytest.mark.asyncio
async def test_cash_balance_mock_kis_timeout_surfaces_reason_and_marks_unavailable(
    monkeypatch,
):
    """ROB-600: a KIS read timeout must (1) surface 'ReadTimeout' (not ''),
    (2) appear in summary.unavailable_sources, (3) NOT add a kis_domestic row,
    (4) leave total_krw excluding KIS."""
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(side_effect=httpx.ReadTimeout(""))

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    # (1) concrete reason, not empty
    kis_kr_err = next(
        e for e in result["errors"] if e["source"] == "kis" and e["market"] == "kr"
    )
    assert kis_kr_err["error"] == "ReadTimeout"
    # (2) machine-readable unavailable flag
    assert result["summary"]["unavailable_sources"]["kis_domestic"] == "ReadTimeout"
    # (3) no placeholder row injected
    assert "kis_domestic" not in {a["account"] for a in result["accounts"]}
    # (4) KIS cash not silently summed as a number
    assert result["summary"]["total_krw"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_available_capital_propagates_unavailable_sources(monkeypatch):
    """ROB-600: capital summary carries unavailable_sources so KIS failure is not
    mistaken for 0 orderable cash."""
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(side_effect=httpx.ReadTimeout(""))

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )
    monkeypatch.setattr(
        portfolio_cash, "get_account_costs_setting", AsyncMock(return_value=None)
    )

    result = await portfolio_cash.get_available_capital_impl(
        include_manual=False, is_mock=True
    )

    assert result["summary"]["unavailable_sources"]["kis_domestic"] == "ReadTimeout"
    assert result["summary"]["total_orderable_krw"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_live_kis_orderable_raises_when_row_missing(monkeypatch):
    """ROB-600 regression guard: with NO placeholder row added, the live precheck
    source still RAISES on a missing kis row instead of silently reading 0."""
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(
        order_validation,
        "get_cash_balance_impl",
        AsyncMock(
            return_value={
                "accounts": [],
                "summary": {
                    "total_krw": 0.0,
                    "total_usd": 0.0,
                    "unavailable_sources": {"kis_domestic": "ReadTimeout"},
                },
                "errors": [{"source": "kis", "market": "kr", "error": "ReadTimeout"}],
            }
        ),
    )

    with pytest.raises(RuntimeError, match="orderable not found"):
        await order_validation._live_kis_orderable("kis_domestic")
