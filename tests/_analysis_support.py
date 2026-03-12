from __future__ import annotations

import pandas as pd

from app.analysis.models import PriceAnalysis, PriceRange, StockAnalysisResponse


def build_analysis_sample_df(rows: int = 220) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    close_values = [100.0 + (index * 0.5) for index in range(rows)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value - 1.0 for value in close_values],
            "high": [value + 2.0 for value in close_values],
            "low": [value - 2.0 for value in close_values],
            "close": close_values,
            "volume": [1_000_000.0 + (index * 1_000.0) for index in range(rows)],
        }
    )


def sample_fundamental_info() -> dict[str, object]:
    return {
        "PER": 12.5,
        "PBR": 1.3,
        "배당수익률": "2.1%",
        "시가총액": "500조원",
    }


def sample_position_info() -> dict[str, object]:
    return {
        "quantity": 15,
        "avg_price": 95_000,
        "total_value": 1_425_000,
        "locked_quantity": 3,
    }


def build_minute_candles() -> dict[str, pd.DataFrame]:
    candles_60 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-10 09:00", periods=12, freq="60min"),
            "close": [110_000.0 + index for index in range(12)],
            "volume": [5_000.0 + (index * 50.0) for index in range(12)],
        }
    )
    candles_5 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-10 09:00", periods=12, freq="5min"),
            "close": [109_500.0 + index for index in range(12)],
            "volume": [1_000.0 + (index * 25.0) for index in range(12)],
        }
    )
    candles_1 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-10 09:00", periods=10, freq="1min"),
            "close": [109_000.0 + index for index in range(10)],
            "volume": [300.0 + (index * 10.0) for index in range(10)],
        }
    )
    return {
        "60min": candles_60,
        "5min": candles_5,
        "1min": candles_1,
    }


def build_stock_analysis_response() -> StockAnalysisResponse:
    return StockAnalysisResponse(
        decision="buy",
        reasons=[
            "기술적 추세가 우상향입니다.",
            "거래량이 증가하고 있습니다.",
            "리스크 대비 진입 타이밍이 양호합니다.",
        ],
        price_analysis=PriceAnalysis(
            appropriate_buy_range=PriceRange(min=98_000, max=100_000),
            appropriate_sell_range=PriceRange(min=105_000, max=108_000),
            buy_hope_range=PriceRange(min=95_000, max=97_000),
            sell_target_range=PriceRange(min=112_000, max=115_000),
        ),
        detailed_text="**매수**\n\n상승 추세와 거래량 증가를 근거로 합니다.",
        confidence=86,
    )
