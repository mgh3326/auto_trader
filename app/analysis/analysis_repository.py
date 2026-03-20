from __future__ import annotations

from app.analysis.models import StockAnalysisResponse
from app.core.db import AsyncSessionLocal
from app.models.analysis import StockAnalysisResult
from app.models.prompt import PromptResult


class AnalysisRepository:
    async def save_text_analysis(
        self,
        prompt: str,
        result: str | StockAnalysisResponse,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            record = PromptResult(
                prompt=prompt,
                result=result,
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                model_name=model_name,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            print(f"DB 저장 완료: {symbol} ({name})")

    async def save_structured_analysis(
        self,
        prompt: str,
        result: StockAnalysisResponse,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            from app.services.stock_info_service import create_stock_if_not_exists

            stock_info = await create_stock_if_not_exists(
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
            )

            record = StockAnalysisResult(
                stock_info_id=stock_info.id,
                prompt=prompt,
                model_name=model_name,
                decision=result.decision,
                confidence=result.confidence,
                appropriate_buy_min=result.price_analysis.appropriate_buy_range.min,
                appropriate_buy_max=result.price_analysis.appropriate_buy_range.max,
                appropriate_sell_min=result.price_analysis.appropriate_sell_range.min,
                appropriate_sell_max=result.price_analysis.appropriate_sell_range.max,
                buy_hope_min=result.price_analysis.buy_hope_range.min,
                buy_hope_max=result.price_analysis.buy_hope_range.max,
                sell_target_min=result.price_analysis.sell_target_range.min,
                sell_target_max=result.price_analysis.sell_target_range.max,
                reasons=result.reasons,
                detailed_text=result.detailed_text,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            print(
                f"JSON 분석 결과 DB 저장 완료: {symbol} ({name}) - StockInfo ID: {stock_info.id}"
            )
