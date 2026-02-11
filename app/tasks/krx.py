"""KOSPI200 Celery Tasks"""

import asyncio
import logging

from celery import shared_task

from app.core.db import AsyncSessionLocal
from app.services.krx import Kospi200Service, KRXMarketDataService

logger = logging.getLogger(__name__)


@shared_task(name="krx.update_kospi200_constituents", bind=True)
def update_kospi200_constituents_task(self) -> dict:
    """KOSPI200 구성종목 데이터를 업데이트하는 Celery 태스크"""

    async def _run() -> dict:
        krx_service = KRXMarketDataService()

        try:
            self.update_state(
                state="PROGRESS",
                meta={"status": "KRX에서 데이터 수집 중..."},
            )

            # KRX에서 KOSPI200 구성종목 데이터 가져오기
            constituents_data = await krx_service.fetch_kospi200_constituents()

            if not constituents_data:
                logger.error("KOSPI200 데이터를 가져오지 못했습니다.")
                return {
                    "status": "failed",
                    "error": "KRX 데이터 수집 실패",
                    "count": 0,
                }

            logger.info(f"KOSPI200 데이터 수집 완료: {len(constituents_data)}개 종목")

            self.update_state(
                state="PROGRESS",
                meta={
                    "status": f"데이터베이스 업데이트 중... ({len(constituents_data)}개 종목)",
                },
            )

            # DB 업데이트
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

        except Exception as e:
            logger.error(f"KOSPI200 업데이트 태스크 실패: {e}")
            return {"status": "failed", "error": str(e), "count": 0}

    return asyncio.run(_run())


@shared_task(name="krx.sync_kospi200_to_stock_info", bind=True)
def sync_kospi200_to_stock_info_task(self) -> dict:
    """KOSPI200 구성종목을 StockInfo 테이블에 동기화하는 Celery 태스크"""

    async def _run() -> dict:
        from app.services.stock_info_service import StockInfoService

        try:
            self.update_state(
                state="PROGRESS",
                meta={"status": "KOSPI200 구성종목 조회 중..."},
            )

            # KOSPI200 구성종목 조회
            async with AsyncSessionLocal() as db:
                kospi200_service = Kospi200Service(db)
                kospi200_constituents = await kospi200_service.get_all_constituents(
                    active_only=True
                )

            if not kospi200_constituents:
                logger.warning("동기화할 KOSPI200 구성종목이 없습니다.")
                return {"status": "completed", "synced_count": 0}

            logger.info(f"동기화할 KOSPI200 구성종목: {len(kospi200_constituents)}개")

            self.update_state(
                state="PROGRESS",
                meta={
                    "status": f"StockInfo 동기화 중... ({len(kospi200_constituents)}개 종목)",
                },
            )

            # StockInfo 테이블에 동기화
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

        except Exception as e:
            logger.error(f"KOSPI200 동기화 태스크 실패: {e}")
            return {"status": "failed", "error": str(e), "synced_count": 0}

    return asyncio.run(_run())
