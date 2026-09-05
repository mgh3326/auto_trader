"""ROB-820 regression coverage for the KIS mock read data plane."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze, portfolio_cash, portfolio_holdings
from app.mcp_server.tooling.fundamentals import _financials
from app.services.brokers.kis.circuit_breaker import KISCircuitOpen
from app.services.nxt_preflight import NxtTradability


def _kis_position() -> dict[str, object]:
    return {
        "account": "kis",
        "account_name": "기본 계좌",
        "broker": "kis",
        "source": "kis_api",
        "instrument_type": "equity_kr",
        "market": "kr",
        "symbol": "005930",
        "name": "삼성전자",
        "quantity": 2.0,
        "avg_buy_price": 70_000.0,
        "current_price": 71_000.0,
        "evaluation_amount": 142_000.0,
        "profit_loss": 2_000.0,
        "profit_rate": 1.43,
    }


@pytest.mark.asyncio
async def test_kis_mock_cash_does_not_query_live_account_sources(monkeypatch):
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": "1000000",
            "stck_cash_ord_psbl_amt": "900000",
        }
    )
    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )

    upbit_read = AsyncMock(side_effect=AssertionError("upbit_live must be isolated"))
    toss_read = AsyncMock(side_effect=AssertionError("toss_api must be isolated"))
    monkeypatch.setattr(
        portfolio_cash.upbit_service, "fetch_krw_cash_summary", upbit_read
    )
    monkeypatch.setattr(portfolio_cash, "fetch_toss_cash_snapshot", toss_read)
    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    upbit_read.assert_not_awaited()
    toss_read.assert_not_awaited()
    assert {row["account"] for row in result["accounts"]} == {"kis_domestic"}
    assert all(row["broker"] == "kis" for row in result["accounts"])
    assert all(row["account_mode"] == "kis_mock" for row in result["accounts"])
    assert all(error["source"] == "kis" for error in result["errors"])
    assert all(error["account_mode"] == "kis_mock" for error in result["errors"])


@pytest.mark.asyncio
async def test_kis_mock_available_capital_does_not_query_manual_cash(monkeypatch):
    monkeypatch.setattr(
        portfolio_cash,
        "get_cash_balance_impl",
        AsyncMock(
            return_value={
                "accounts": [
                    {
                        "account": "kis_domestic",
                        "broker": "kis",
                        "currency": "KRW",
                        "balance": 1_000_000.0,
                        "orderable": 900_000.0,
                        "account_mode": "kis_mock",
                    }
                ],
                "summary": {"unavailable_sources": {}},
                "errors": [],
            }
        ),
    )
    manual_read = AsyncMock(
        side_effect=AssertionError("manual cash must be isolated from kis_mock")
    )
    monkeypatch.setattr(portfolio_cash, "get_manual_cash_setting", manual_read)
    monkeypatch.setattr(
        portfolio_cash, "get_account_costs_setting", AsyncMock(return_value=None)
    )

    result = await portfolio_cash.get_available_capital_impl(is_mock=True)

    manual_read.assert_not_awaited()
    assert result["manual_cash"] is None
    assert result["summary"]["total_orderable_krw"] == pytest.approx(900_000.0)


@pytest.mark.asyncio
async def test_kis_mock_holdings_do_not_query_live_or_manual_sources(monkeypatch):
    kis_read = AsyncMock(return_value=([_kis_position()], []))
    upbit_read = AsyncMock(return_value=([], []))
    manual_read = AsyncMock(return_value=([], []))
    toss_read = AsyncMock(return_value=([], [], True))
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", kis_read)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", upbit_read)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", manual_read)
    monkeypatch.setattr(portfolio_holdings, "_collect_toss_api_positions", toss_read)
    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)

    positions, errors, _, _ = await portfolio_holdings._collect_portfolio_positions(
        account=None,
        market=None,
        include_current_price=False,
        is_mock=True,
    )

    kis_read.assert_awaited_once_with(None, is_mock=True)
    upbit_read.assert_not_awaited()
    manual_read.assert_not_awaited()
    toss_read.assert_not_awaited()
    assert len(positions) == 1
    assert positions[0]["source"] == "kis_api"
    assert positions[0]["current_price"] is None
    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account", "market"),
    [
        ("upbit", None),
        ("toss", None),
        ("samsung_pension", None),
        ("isa", None),
        ("paper", None),
        (None, "crypto"),
    ],
)
async def test_kis_mock_incompatible_portfolio_selector_fails_closed(
    monkeypatch, account, market
):
    monkeypatch.setattr(
        portfolio_holdings, "_collect_kis_positions", AsyncMock(return_value=([], []))
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_upbit_positions", AsyncMock(return_value=([], []))
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    with pytest.raises(ValueError, match="kis_mock"):
        await portfolio_holdings._collect_portfolio_positions(
            account=account,
            market=market,
            include_current_price=False,
            is_mock=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account", ["upbit", "toss", "samsung_pension", "isa", "paper"]
)
async def test_kis_mock_incompatible_cash_selector_fails_closed(account):
    with pytest.raises(ValueError, match="kis_mock"):
        await portfolio_cash.get_cash_balance_impl(account=account, is_mock=True)


@pytest.mark.asyncio
async def test_kis_mock_holdings_circuit_open_keeps_reason_and_evidence(monkeypatch):
    class CircuitOpenKIS:
        def __init__(self, *, is_mock: bool = False) -> None:
            assert is_mock is True

        async def fetch_my_stocks(self, *, is_mock: bool = False):
            assert is_mock is True
            raise KISCircuitOpen(45.0)

    monkeypatch.setattr(portfolio_holdings, "KISClient", CircuitOpenKIS)

    positions, errors = await portfolio_holdings._collect_kis_positions(
        "equity_kr", is_mock=True
    )

    assert positions == []
    assert errors == [
        {
            "source": "kis",
            "market": "kr",
            "error": "KIS circuit open — failing fast (retry in ~45.0s)",
        }
    ]


def _ohlcv_frame(*, date_value: object = None, include_date: bool = False):
    row: dict[str, object] = {
        "open": 70_000.0,
        "high": 72_000.0,
        "low": 69_000.0,
        "close": 71_000.0,
        "volume": 1_000,
        "value": 71_000_000.0,
    }
    if include_date:
        row["date"] = date_value
    return pd.DataFrame([row])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        _ohlcv_frame(),
        _ohlcv_frame(date_value=pd.Timestamp(0), include_date=True),
    ],
)
async def test_kr_quote_missing_or_epoch_asof_is_unavailable(monkeypatch, frame):
    monkeypatch.setattr(
        analysis_analyze, "_fetch_kr_live_quote", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        analysis_analyze, "get_kr_nxt_tradability", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        analysis_analyze, "_apply_nxt_quote_overlay", AsyncMock(return_value=False)
    )

    quote = await analysis_analyze._resolve_kr_quote("005930", frame)

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.asyncio
async def test_kr_quote_old_asof_is_stale_and_not_usable(monkeypatch):
    frame = _ohlcv_frame(date_value="2024-01-02", include_date=True)
    monkeypatch.setattr(
        analysis_analyze, "_fetch_kr_live_quote", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        analysis_analyze, "get_kr_nxt_tradability", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        analysis_analyze, "_apply_nxt_quote_overlay", AsyncMock(return_value=False)
    )

    quote = await analysis_analyze._resolve_kr_quote("005930", frame)

    assert quote is not None
    assert quote["price_as_of"].startswith("2024-01-02")
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "stale_price_asof"


@pytest.mark.asyncio
async def test_kr_live_quote_missing_asof_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_kr_live_quote",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "price": 71_000.0,
                "price_as_of": None,
                "source": "kis",
            }
        ),
    )
    monkeypatch.setattr(
        analysis_analyze, "get_kr_nxt_tradability", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        analysis_analyze, "_apply_nxt_quote_overlay", AsyncMock(return_value=False)
    )

    quote = await analysis_analyze._resolve_kr_quote("005930", pd.DataFrame())

    assert quote is not None
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.parametrize(
    ("asof", "reason"),
    [
        (None, "missing_asof"),
        (dt.datetime(2026, 6, 1, tzinfo=dt.UTC), "stale_asof"),
    ],
)
def test_nxt_tradability_missing_or_stale_asof_is_unavailable(asof, reason):
    fields = NxtTradability(
        nxt_eligible=True,
        nxt_trading_suspended=False,
        asof=asof,
    ).public_fields(now=dt.datetime(2026, 7, 13, tzinfo=dt.UTC))

    assert fields["nxt_tradable"] is None
    assert fields["nxt_tradable_observed"] is True
    assert fields["nxt_tradable_stale"] is True
    assert fields["nxt_tradable_reason"] == reason


def test_nxt_tradability_fresh_asof_remains_available():
    asof = dt.datetime(2026, 7, 13, tzinfo=dt.UTC)
    fields = NxtTradability(
        nxt_eligible=True,
        nxt_trading_suspended=False,
        asof=asof,
    ).public_fields(now=asof + dt.timedelta(hours=1))

    assert fields["nxt_tradable"] is True
    assert fields["nxt_tradable_stale"] is False
    assert "nxt_tradable_observed" not in fields
    assert "nxt_tradable_reason" not in fields


@pytest.mark.asyncio
async def test_empty_kr_financials_are_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(
        _financials,
        "_fetch_financials_naver",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "source": "naver",
                "statement": "income",
                "freq": "annual",
                "currency": "KRW",
                "periods": ["최근 연간 실적"],
                "metrics": {},
            }
        ),
    )

    result = await _financials.handle_get_financials("005930", market="kr")

    assert result["metrics"] == {}
    assert result["status"] == "unavailable"
    assert result["scoreable"] is False
    assert result["reason"] == "financial_metrics_unavailable"
    assert result["evidence"] == {
        "source": "naver",
        "statement": "income",
        "freq": "annual",
        "period_count": 1,
    }


@pytest.mark.asyncio
async def test_nonempty_kr_financials_are_available_without_synthetic_values(
    monkeypatch,
):
    metrics = {"매출액": [100_000_000]}
    monkeypatch.setattr(
        _financials,
        "_fetch_financials_naver",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "source": "naver",
                "statement": "income",
                "freq": "annual",
                "currency": "KRW",
                "periods": ["2025/12"],
                "metrics": metrics,
            }
        ),
    )

    result = await _financials.handle_get_financials("005930", market="kr")

    assert result["metrics"] == metrics
    assert result["status"] == "available"
    assert result["scoreable"] is True
    assert "reason" not in result
    assert "evidence" not in result


@pytest.mark.asyncio
async def test_us_financial_report_metadata_without_metrics_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        _financials,
        "_fetch_financials_finnhub",
        AsyncMock(
            return_value={
                "symbol": "AAPL",
                "instrument_type": "equity_us",
                "source": "finnhub",
                "statement": "income",
                "freq": "annual",
                "reports": [
                    {
                        "year": 2025,
                        "quarter": 0,
                        "filed_date": "2026-02-01",
                        "data": {},
                    }
                ],
            }
        ),
    )

    result = await _financials.handle_get_financials("AAPL", market="us")

    assert result["reports"][0]["data"] == {}
    assert result["status"] == "unavailable"
    assert result["scoreable"] is False
    assert result["reason"] == "financial_metrics_unavailable"
    assert result["evidence"]["period_count"] == 1
