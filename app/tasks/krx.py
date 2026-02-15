import logging

from app.core.db import AsyncSessionLocal
from app.services.krx import Kospi200Service, KRXMarketDataService

logger = logging.getLogger(__name__)


async def update_kospi200_constituents_task() -> dict:
    krx_service = KRXMarketDataService()

    try:
        constituents_data = await krx_service.fetch_kospi200_constituents()

        if not constituents_data:
            logger.error("KOSPI200 데이터를 가져오지 못했습니다.")
            return {
                "status": "failed",
                "error": "KRX 데이터 수집 실패",
                "count": 0,
            }

        logger.info("KOSPI200 데이터 수집 완료: %d개 종목", len(constituents_data))

        async with AsyncSessionLocal() as db:
            kospi200_service = Kospi200Service(db)
            update_result = await kospi200_service.update_constituents(
                constituents_data
            )

        return {
            "status": "completed",
            "total_count": len(constituents_data),
            "added": update_result["added"],
            "updated": update_result["updated"],
            "removed": update_result["removed"],
        }
    except Exception as exc:
        logger.error("KOSPI200 업데이트 태스크 실패: %s", exc)
        return {"status": "failed", "error": str(exc), "count": 0}


async def sync_kospi200_to_stock_info_task() -> dict:
    from app.services.stock_info_service import StockInfoService

    try:
        async with AsyncSessionLocal() as db:
            kospi200_service = Kospi200Service(db)
            kospi200_constituents = await kospi200_service.get_all_constituents(
                active_only=True
            )

        if not kospi200_constituents:
            logger.warning("동기화할 KOSPI200 구성종목이 없습니다.")
            return {"status": "completed", "synced_count": 0}

        logger.info("동기화할 KOSPI200 구성종목: %d개", len(kospi200_constituents))

        synced_count = 0
        updated_count = 0

        async with AsyncSessionLocal() as db:
            for constituent in kospi200_constituents:
                stock_info_service = StockInfoService(db)

                existing_stock = await stock_info_service.get_stock_info_by_symbol(
                    constituent.stock_code
                )

                stock_data = {
                    "symbol": constituent.stock_code,
                    "name": constituent.stock_name,
                    "instrument_type": "equity_kr",
                    "exchange": "KRX",
                    "sector": constituent.sector,
                }

                if existing_stock:
                    await stock_info_service.update_stock_info(
                        existing_stock.id, stock_data
                    )
                    updated_count += 1
                else:
                    await stock_info_service.create_stock_info(stock_data)
                    synced_count += 1

        logger.info(
            "KOSPI200 동기화 완료: 신규=%d, 업데이트=%d",
            synced_count,
            updated_count,
        )

        return {
            "status": "completed",
            "synced_count": synced_count,
            "updated_count": updated_count,
            "total_count": len(kospi200_constituents),
        }
    except Exception as exc:
        logger.error("KOSPI200 동기화 태스크 실패: %s", exc)
        return {"status": "failed", "error": str(exc), "synced_count": 0}
