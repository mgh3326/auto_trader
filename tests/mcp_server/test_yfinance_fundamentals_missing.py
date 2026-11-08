from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import fundamentals_sources_yfinance as yfinance_sources
from app.mcp_server.tooling.fundamentals import _financials, _valuation


@pytest.fixture
def stub_yfinance(monkeypatch):
    class FakeTicker:
        income_stmt = pd.DataFrame()
        balance_sheet = pd.DataFrame()
        cashflow = pd.DataFrame()
        quarterly_income_stmt = pd.DataFrame()
        quarterly_balance_sheet = pd.DataFrame()
        quarterly_cashflow = pd.DataFrame()

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )
    monkeypatch.setattr(
        yfinance_sources,
        "yfinance_tracing_session",
        lambda: _null_context(object()),
    )
    monkeypatch.setattr(yfinance_sources.asyncio, "to_thread", _run_sync)
    monkeypatch.setattr(
        _financials,
        "_fetch_financials_finnhub",
        AsyncMock(side_effect=RuntimeError("Finnhub unavailable")),
    )


class _null_context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


async def _run_sync(function, *args, **kwargs):
    return function(*args, **kwargs)


@pytest.mark.asyncio
async def test_financials_yfinance_404_no_fundamentals_is_normal_missing_result(
    stub_yfinance, monkeypatch
):
    class FakeTicker:
        @property
        def income_stmt(self):
            raise RuntimeError(
                'HTTP Error 404: {"quoteSummary":{"error":{"description":'
                '"No fundamentals data found for symbol: QQQM"}}}'
            )

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )

    result = await _financials.handle_get_financials("QQQM", market="us")

    assert result == {
        "success": True,
        "symbol": "QQQM",
        "instrument_type": "equity_us",
        "source": "yfinance",
        "statement": "income",
        "freq": "annual",
        "data": {},
        "data_state": "missing",
        "reason": "no_fundamentals",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("HTTP Error 401: Unauthorized"),
        TimeoutError("provider timed out"),
        RuntimeError('HTTP Error 404: {"description":"symbol not found"}'),
        RuntimeError('HTTP Error 500: "No fundamentals data found"'),
    ],
)
async def test_financials_yfinance_other_failures_remain_errors(
    stub_yfinance, monkeypatch, failure
):
    class FakeTicker:
        @property
        def income_stmt(self):
            raise failure

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )

    result = await _financials.handle_get_financials("QQQM", market="us")

    assert "data_state" not in result
    assert result["error"]
    assert result["source"] == "yfinance"


@pytest.mark.asyncio
async def test_financials_yfinance_data_payload_is_unchanged(
    stub_yfinance, monkeypatch
):
    frame = pd.DataFrame(
        {pd.Timestamp("2025-12-31"): {"Total Revenue": 42}},
    )

    class FakeTicker:
        income_stmt = frame

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )

    result = await _financials.handle_get_financials("AAPL", market="us")

    assert result == {
        "symbol": "AAPL",
        "instrument_type": "equity_us",
        "source": "yfinance",
        "statement": "income",
        "freq": "annual",
        "data": {"2025-12-31": {"Total Revenue": 42}},
        "status": "available",
        "scoreable": True,
    }


@pytest.mark.asyncio
async def test_empty_financial_statement_is_normal_missing_result(stub_yfinance):
    result = await _financials.handle_get_financials("QQQM", market="us")
    assert result["success"] is True
    assert result["data_state"] == "missing"
    assert result["reason"] == "no_fundamentals"


def test_classifier_requires_both_http_404_and_no_fundamentals_message():
    assert yfinance_sources._is_no_fundamentals_error(
        RuntimeError("HTTP Error 404: No fundamentals data found for symbol: VOO")
    )
    assert not yfinance_sources._is_no_fundamentals_error(
        RuntimeError("HTTP Error 404: symbol not found")
    )
    assert not yfinance_sources._is_no_fundamentals_error(
        RuntimeError("HTTP Error 500: No fundamentals data found")
    )


@pytest.mark.asyncio
async def test_valuation_quote_summary_404_is_normal_missing_result(
    monkeypatch, allow_external_providers
):
    class FakeTicker:
        @property
        def info(self):
            raise RuntimeError(
                "HTTP Error 404: No fundamentals data found for symbol: VOO"
            )

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )
    monkeypatch.setattr(
        yfinance_sources, "build_yfinance_tracing_session", lambda: object()
    )
    monkeypatch.setattr(
        yfinance_sources, "close_yfinance_session", lambda _session: None
    )
    monkeypatch.setattr(yfinance_sources.asyncio, "to_thread", _run_sync)

    result = await _valuation.handle_get_valuation("VOO", market="us")

    assert result == yfinance_sources._missing_fundamentals_payload("VOO")


@pytest.mark.asyncio
async def test_analyst_quote_summary_404_is_normal_missing_result(
    monkeypatch, allow_external_providers
):
    class FakeTicker:
        @property
        def analyst_price_targets(self):
            raise RuntimeError(
                "HTTP Error 404: No fundamentals data found for symbol: VOO"
            )

        @property
        def recommendations(self):
            return None

        @property
        def upgrades_downgrades(self):
            return None

        @property
        def info(self):
            return {}

    monkeypatch.setattr(
        yfinance_sources.yf, "Ticker", lambda *args, **kwargs: FakeTicker()
    )
    monkeypatch.setattr(
        yfinance_sources, "build_yfinance_tracing_session", lambda: object()
    )
    monkeypatch.setattr(
        yfinance_sources, "close_yfinance_session", lambda _session: None
    )
    monkeypatch.setattr(yfinance_sources.asyncio, "to_thread", _run_sync)

    result = await _valuation.handle_get_investment_opinions("VOO", market="us")

    assert result["success"] is True
    assert result["data_state"] == "missing"
    assert result["reason"] == "no_fundamentals"
