import pytest

from app.services.portfolio_allocation_service import build_portfolio_allocation


def test_build_allocation_converts_usd_and_looks_through_kr_us_etf() -> None:
    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_us",
            "market": "us",
            "symbol": "AAPL",
            "name": "Apple",
            "evaluation_amount": 1000.0,
            "profit_loss": 100.0,
        },
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "360750",
            "name": "TIGER 미국S&P500",
            "evaluation_amount": 700000.0,
            "profit_loss": 70000.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "evaluation_amount": 300000.0,
            "profit_loss": -30000.0,
        },
    ]
    cash_accounts = [
        {
            "account": "kis_domestic",
            "account_name": "기본 계좌",
            "broker": "kis",
            "currency": "KRW",
            "balance": 100000.0,
        },
        {
            "account": "kis_overseas",
            "account_name": "기본 계좌",
            "broker": "kis",
            "currency": "USD",
            "balance": 100.0,
        },
    ]
    etf_rows = [
        {
            "short_code": "360750",
            "code": "KR7360750004",
            "name": "TIGER 미국S&P500",
            "index_name": "S&P 500",
        }
    ]

    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=cash_accounts,
        usd_krw=1400.0,
        etf_rows=etf_rows,
        include_cash=True,
        include_positions=False,
        target_weights={"us_equity": 50.0, "crypto": 25.0},
        drift_threshold_pct=5.0,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(2640000.0)
    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["us_equity"]["value_krw"] == pytest.approx(2100000.0)
    assert by_class["us_equity"]["direct_value_krw"] == pytest.approx(1400000.0)
    assert by_class["us_equity"]["lookthrough_value_krw"] == pytest.approx(700000.0)
    assert by_class["crypto"]["value_krw"] == pytest.approx(300000.0)
    assert by_class["cash"]["value_krw"] == pytest.approx(240000.0)
    assert by_class["us_equity"]["weight_status"] == "overweight"
    assert result["lookthrough"][0]["effective_asset_class"] == "us_equity"
    assert result["positions"] == []


def test_build_allocation_puts_non_us_foreign_etf_in_other_bucket() -> None:
    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "453870",
            "name": "TIGER 인도니프티50",
            "evaluation_amount": 500000.0,
        }
    ]
    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=[],
        usd_krw=1400.0,
        etf_rows=[
            {
                "short_code": "453870",
                "name": "TIGER 인도니프티50",
                "index_name": "Nifty 50",
            }
        ],
        include_cash=False,
        include_positions=True,
    )

    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["other"]["value_krw"] == pytest.approx(500000.0)
    assert result["lookthrough"][0]["effective_asset_class"] == "other"


def test_build_allocation_reports_per_account_profit_loss() -> None:
    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_us",
            "market": "us",
            "symbol": "AAPL",
            "name": "Apple",
            "evaluation_amount": 1000.0,
            "profit_loss": 100.0,  # USD -> +140,000 KRW
        },
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "삼성전자",
            "evaluation_amount": 500000.0,
            "profit_loss": 50000.0,  # +50,000 KRW
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "evaluation_amount": 300000.0,
            "profit_loss": -30000.0,
        },
    ]
    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=[
            {
                "account": "kis_domestic",
                "account_name": "기본 계좌",
                "broker": "kis",
                "currency": "KRW",
                "balance": 100000.0,
            }
        ],
        usd_krw=1400.0,
        etf_rows=[],
        include_cash=True,
        include_positions=False,
    )

    by_account = {row["account"]: row for row in result["accounts"]}
    # 140,000 (AAPL USD->KRW) + 50,000 (삼성전자) = 190,000
    assert by_account["kis"]["profit_loss_krw"] == pytest.approx(190000.0)
    assert by_account["upbit"]["profit_loss_krw"] == pytest.approx(-30000.0)
    # cash-only account carries zero P&L
    assert by_account["kis_domestic"]["profit_loss_krw"] == pytest.approx(0.0)


def test_build_allocation_warns_and_skips_unvalued_positions() -> None:
    result = build_portfolio_allocation(
        positions=[
            {
                "account": "kis",
                "account_name": "기본 계좌",
                "broker": "kis",
                "instrument_type": "equity_kr",
                "market": "kr",
                "symbol": "005930",
                "name": "삼성전자",
                "evaluation_amount": None,
            }
        ],
        cash_accounts=[],
        usd_krw=1400.0,
        etf_rows=[],
        include_cash=False,
        include_positions=False,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(0.0)
    assert result["summary"]["unvalued_position_count"] == 1
    assert result["warnings"][0]["reason"] == "position_value_unavailable"
