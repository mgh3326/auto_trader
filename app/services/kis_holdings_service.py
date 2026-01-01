"""
Helpers for fetching KIS holdings data.
"""

import logging

from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.services.kis import KISClient

logger = logging.getLogger(__name__)


async def get_kis_holding_for_ticker(
    kis_client: KISClient, ticker: str, market_type: MarketType
) -> dict[str, float]:
    """Fetch KIS holding info for a single ticker."""
    normalized_ticker = to_db_symbol(ticker.upper())
    default = {"quantity": 0, "avg_price": 0.0, "current_price": 0.0}

    try:
        if market_type == MarketType.KR:
            stocks = await kis_client.fetch_my_stocks()
            for stock in stocks:
                if stock.get("pdno") == normalized_ticker:
                    return {
                        "quantity": int(stock.get("hldg_qty", 0)),
                        "avg_price": float(stock.get("pchs_avg_pric", 0)),
                        "current_price": float(stock.get("prpr", 0)),
                    }
        else:
            stocks = await kis_client.fetch_overseas_stocks()
            for stock in stocks:
                # KIS API 응답의 심볼도 정규화하여 비교
                if to_db_symbol(stock.get("ovrs_pdno", "")) == normalized_ticker:
                    return {
                        "quantity": int(float(stock.get("ovrs_cblc_qty", 0))),
                        "avg_price": float(stock.get("pchs_avg_pric", 0)),
                        "current_price": float(stock.get("now_pric2", 0)),
                    }
    except Exception as exc:
        logger.warning(
            "Failed to fetch KIS holdings for %s (%s): %s",
            normalized_ticker,
            market_type.value,
            exc,
        )

    return default
