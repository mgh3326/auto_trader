"""
Trading Schemas

주식 거래 관련 Pydantic 스키마 - OHLCV, 호가 등
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OHLCVData(BaseModel):
    """OHLCV 캔들 데이터"""

    date: str = Field(..., description="날짜 (YYYY-MM-DD)")
    open: float = Field(..., description="시가")
    high: float = Field(..., description="고가")
    low: float = Field(..., description="저가")
    close: float = Field(..., description="종가")
    volume: int = Field(..., description="거래량")


class OrderbookLevel(BaseModel):
    """호가 단계 (매수/매도)"""

    price: float = Field(..., description="호가")
    quantity: int = Field(..., description="수량")


class OrderbookResponse(BaseModel):
    """호가(orderbook) 응답"""

    ticker: str = Field(..., description="종목코드")
    timestamp: datetime | None = Field(None, description="조회 시간")
    ask: list[OrderbookLevel] = Field(..., description="매도 호가 (10단계)")
    bid: list[OrderbookLevel] = Field(..., description="매수 호가 (10단계)")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticker": "005930",
                "timestamp": "2024-02-10T13:45:30",
                "ask": [
                    {"price": 70100, "quantity": 100},
                    {"price": 70200, "quantity": 200},
                ],
                "bid": [
                    {"price": 70000, "quantity": 150},
                    {"price": 69900, "quantity": 300},
                ],
            }
        }
    )


class OHLCVResponse(BaseModel):
    """OHLCV 데이터 응답"""

    ticker: str = Field(..., description="종목코드")
    data: list[OHLCVData] = Field(..., description="OHLCV 데이터 목록")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticker": "005930",
                "data": [
                    {
                        "date": "2024-02-01",
                        "open": 75000,
                        "high": 76000,
                        "low": 74500,
                        "close": 75500,
                        "volume": 1000000,
                    }
                ],
            }
        }
    )
