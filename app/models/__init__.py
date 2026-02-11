# app/models/__init__.py
from .analysis import StockAnalysisResult, StockInfo
from .base import Base
from .dca_plan import (
    DcaPlan,
    DcaPlanStatus,
    DcaPlanStep,
    DcaStepStatus,
)
from .manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
    StockAlias,
)
from .news import NewsAnalysisResult, NewsArticle, Sentiment
from .prompt import PromptResult
from .symbol_trade_settings import SymbolTradeSettings
from .trading import Exchange, Instrument, User, UserChannel, UserRole, UserWatchItem

# 필요한 다른 모델도 여기서 import
# from .alert import AlertRule, AlertEvent
# from .price import PricesLatest, PricesOHLCV, FxRate

__all__ = [
    "Base",
    "Exchange",
    "Instrument",
    "User",
    "UserRole",
    "UserChannel",
    "UserWatchItem",
    "PromptResult",
    "StockInfo",
    "StockAnalysisResult",
    "SymbolTradeSettings",
    "NewsArticle",
    "NewsAnalysisResult",
    "Sentiment",
    "BrokerType",
    "MarketType",
    "BrokerAccount",
    "StockAlias",
    "ManualHolding",
    # DCA Models
    "DcaPlan",
    "DcaPlanStatus",
    "DcaPlanStep",
    "DcaStepStatus",
    # "AlertRule", "AlertEvent",
    # "PricesLatest", "PricesOHLCV", "FxRate",
]
