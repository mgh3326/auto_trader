"""
Stock Alias Service

종목 별칭 관리 서비스
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType, StockAlias


class StockAliasService:
    """종목 별칭 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _get_default_ticker_by_alias(alias: str, market_type: MarketType) -> str | None:
        normalized_alias = str(alias or "").strip()
        if not normalized_alias:
            return None

        for item in TOSS_STOCK_ALIASES:
            if (
                item["market_type"] == market_type
                and str(item["alias"]).strip() == normalized_alias
            ):
                return str(item["ticker"]).upper()

        return None

    async def get_ticker_by_alias(
        self, alias: str, market_type: MarketType
    ) -> str | None:
        """별칭으로 티커 조회"""
        result = await self.db.execute(
            select(StockAlias)
            .where(StockAlias.alias == alias)
            .where(StockAlias.market_type == market_type)
        )
        stock_alias = result.scalar_one_or_none()
        if stock_alias:
            return stock_alias.ticker

        return self._get_default_ticker_by_alias(alias, market_type)


# 토스 종목명 -> 실제 티커 매핑 기본 데이터
TOSS_STOCK_ALIASES = [
    # 해외주식
    {
        "ticker": "BRK.B",
        "alias": "버크셔 해서웨이 B",
        "market_type": MarketType.US,
        "source": "toss",
    },
    {
        "ticker": "TSMC",
        "alias": "TSMC(ADR)",
        "market_type": MarketType.US,
        "source": "toss",
    },
    {"ticker": "QQQM", "alias": "QQQM", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "QQQ", "alias": "QQQ", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "SPYM", "alias": "SPYM", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "VOO", "alias": "VOO", "market_type": MarketType.US, "source": "toss"},
    {
        "ticker": "PLTR",
        "alias": "팔란티어",
        "market_type": MarketType.US,
        "source": "toss",
    },
    {"ticker": "IVV", "alias": "IVV", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "AAPL", "alias": "애플", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "ETHU", "alias": "ETHU", "market_type": MarketType.US, "source": "toss"},
    # Samsung overseas aliases
    {
        "ticker": "TSLL",
        "alias": "DIREXION TESLA 2X",
        "market_type": MarketType.US,
        "source": "samsung",
    },
    # 국내주식
    {
        "ticker": "068270",
        "alias": "셀트리온",
        "market_type": MarketType.KR,
        "source": "toss",
    },
    {
        "ticker": "005930",
        "alias": "삼성전자",
        "market_type": MarketType.KR,
        "source": "toss",
    },
    {
        "ticker": "000660",
        "alias": "SK하이닉스",
        "market_type": MarketType.KR,
        "source": "toss",
    },
    {
        "ticker": "207940",
        "alias": "삼성바이오로직스",
        "market_type": MarketType.KR,
        "source": "toss",
    },
    {
        "ticker": "373220",
        "alias": "LG에너지솔루션",
        "market_type": MarketType.KR,
        "source": "toss",
    },
]
