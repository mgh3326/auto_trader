"""Type definitions and data models for KIS API."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import pandas as pd
from pydantic import BaseModel

# ============================================================================
# ORDER TYPES
# ============================================================================

OrderType = str  # "buy" | "sell"


# ============================================================================
# MARKET TYPES
# ============================================================================

DomesticMarket = str  # "K" | "Q" | "J"
ExchangeCode = str  # "NASD" | "NYSE" | "AMEX" | "SEHK" | "SHAA" | "SZAA" | "TKSE" | "HASE" | "VNSE"


# ============================================================================
# STOCK PRICE DATA
# ============================================================================


@dataclass
class StockPrice:
    """Stock price data."""

    code: str
    date: date
    time: time | None = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    value: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StockPrice":
        """Create StockPrice from API response dict."""
        return cls(**data)

    @classmethod
    def from_dataframe_row(cls, row: pd.Series) -> "StockPrice":
        """Create StockPrice from DataFrame row."""
        return cls(
            code=str(row.get("code", "")),
            date=pd.to_datetime(row.get("date")).date()
            if row.get("date")
            else date.today(),
            time=pd.to_datetime(row.get("time")).time() if row.get("time") else None,
            open=float(row.get("open", 0)),
            high=float(row.get("high", 0)),
            low=float(row.get("low", 0)),
            close=float(row.get("close", 0)),
            volume=int(row.get("volume", 0)),
            value=int(row.get("value", 0)),
        )


@dataclass
class FundamentalInfo:
    """Fundamental stock information."""

    code: str
    name: str = ""
    current_price: float = 0.0
    previous_change: float = 0.0
    change_rate: float = 0.0
    volume: int = 0
    value: int = 0
    market_cap: int = 0
    listed_shares: int = 0
    foreigner_ratio: float = 0.0
    week_52_high: float = 0.0
    week_52_low: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FundamentalInfo":
        """Create FundamentalInfo from dict."""
        return cls(
            code=str(data.get("code", "")),
            name=str(data.get("name", "")),
            current_price=float(data.get("current_price", 0)),
            previous_change=float(data.get("previous_change", 0)),
            change_rate=float(data.get("change_rate", 0)),
            volume=int(data.get("volume", 0)),
            value=int(data.get("value", 0)),
            market_cap=int(data.get("market_cap", 0)),
            listed_shares=int(data.get("listed_shares", 0)),
            foreigner_ratio=float(data.get("foreigner_ratio", 0)),
            week_52_high=float(data.get("week_52_high", 0)),
            week_52_low=float(data.get("week_52_low", 0)),
        )


# ============================================================================
# HOLDINGS DATA
# ============================================================================


@dataclass
class DomesticHolding:
    """Domestic stock holding data."""

    pdno: str  # 종목코드
    prdt_name: str  # 종목명
    hldg_qty: int  # 보유수량
    ord_psbl_qty: int  # 주문가능수량
    pchs_avg_pric: float  # 매입평균가격
    pchs_amt: int  # 매입금액
    prpr: float  # 현재가
    evlu_amt: float  # 평가금액
    evlu_pfls_amt: float  # 평가손익금액
    evlu_pfls_rt: float  # 평가손익율


@dataclass
class OverseasHolding:
    """Overseas stock holding data."""

    ovrs_pdno: str  # 해외종목코드
    ovrs_item_name: str  # 종목명
    frcr_pchs_amt1: float  # 외화매입금액
    ovrs_cblc_qty: int  # 해외잔고수량
    ord_psbl_qty: int  # 주문가능수량
    frcr_buy_amt_smtl1: float  # 외화매수금액합계
    ovrs_stck_evlu_amt: float  # 해외주식평가금액
    frcr_evlu_pfls_amt: float  # 외화평가손익금액
    evlu_pfls_rt: float  # 평가손익율
    ovrs_excg_cd: str  # 거래소코드


# ============================================================================
# ORDER DATA
# ============================================================================


@dataclass
class OrderResponse:
    """Order placement response."""

    odno: str  # 주문번호
    ord_tmd: str  # 주문시각
    msg: str  # 메시지

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderResponse":
        """Create OrderResponse from API response."""
        return cls(
            odno=str(data.get("odno", "")),
            ord_tmd=str(data.get("ord_tmd", "")),
            msg=str(data.get("msg", "")),
        )


@dataclass
class OrderRequest:
    """Order placement request."""

    stock_code: str | None = None  # For domestic: 종목코드
    symbol: str | None = None  # For overseas: 종목심볼
    order_type: OrderType = "buy"
    quantity: int = 0
    price: int | float = 0
    exchange_code: ExchangeCode = "NASD"  # For overseas
    is_mock: bool = False  # Mock trading mode


# ============================================================================
# MARGIN DATA
# ============================================================================


@dataclass
class IntegratedMargin:
    """Integrated margin information."""

    dnca_tot_amt: float  # 주문가능원화증거금
    usd_balance: float  # USD 잔고 (computed from overseas margin)
    krw_balance: float  # KRW 잔고 (computed from dnca_tot_amt)
    frrc_tota_amt: float = 0.0  # 외화증거금합계

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntegratedMargin":
        """Create IntegratedMargin from API response."""
        return cls(
            dnca_tot_amt=float(data.get("dnca_tot_amt", 0)),
            usd_balance=float(data.get("usd_balance", 0)),
            krw_balance=float(data.get("dnca_tot_amt", 0)),
            frrc_tota_amt=float(data.get("frrc_tota_amt", 0)),
        )


# ============================================================================
# CHART DATA
# ============================================================================


@dataclass
class ChartData:
    """Chart data (OHLCV)."""

    datetime: datetime
    date: date
    time: time | None
    open: float
    high: float
    low: float
    close: float
    volume: int
    value: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChartData":
        """Create ChartData from dict."""
        return cls(**data)

    @classmethod
    def from_dataframe_row(cls, row: pd.Series) -> "ChartData":
        """Create ChartData from DataFrame row."""
        dt = pd.to_datetime(row.get("datetime") or row.get("date"))
        return cls(
            datetime=dt,
            date=dt.date(),
            time=dt.time() if hasattr(dt, "time") else None,
            open=float(row.get("open", 0)),
            high=float(row.get("high", 0)),
            low=float(row.get("low", 0)),
            close=float(row.get("close", 0)),
            volume=int(row.get("volume", 0)),
            value=int(row.get("value", 0)),
        )


# ============================================================================
# API RESPONSE MODELS
# ============================================================================


class KISAPIResponse(BaseModel):
    """Base KIS API response model."""

    rt_cd: str  # 응답코드 (0: 성공)
    msg_cd: str | None = None  # 메시지코드
    msg1: str | None = None  # 응답메시지1
    output: dict[str, Any] | None = None  # 단일 결과
    output1: list[dict[str, Any]] | None = None  # 리스트 결과
    output2: list[dict[str, Any]] | None = None  # 리스트 결과2

    @property
    def is_success(self) -> bool:
        """Check if response is successful."""
        return self.rt_cd == "0"

    @property
    def error_message(self) -> str:
        """Get error message if any."""
        if self.is_success:
            return ""
        return f"{self.msg_cd}: {self.msg1}"
