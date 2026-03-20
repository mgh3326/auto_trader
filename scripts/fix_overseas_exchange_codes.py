#!/usr/bin/env python3
"""
기존 해외주식 symbol_trade_settings에 exchange_code가 없는 경우 자동으로 채워주는 스크립트

Usage:
    uv run python scripts/fix_overseas_exchange_codes.py
"""

import asyncio

from sqlalchemy import select, update

from app.core.db import AsyncSessionLocal
from app.models.symbol_trade_settings import SymbolTradeSettings
from app.models.trading import InstrumentType
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol


async def fix_exchange_codes():
    """exchange_code가 없는 해외주식 설정에 대해 자동으로 거래소 코드를 채움"""
    async with AsyncSessionLocal() as db:
        # exchange_code가 없는 해외주식 설정 조회
        stmt = select(SymbolTradeSettings).where(
            SymbolTradeSettings.instrument_type == InstrumentType.equity_us,
            (SymbolTradeSettings.exchange_code.is_(None))
            | (SymbolTradeSettings.exchange_code == ""),
        )
        result = await db.execute(stmt)
        settings_list = result.scalars().all()

        if not settings_list:
            print("exchange_code가 없는 해외주식 설정이 없습니다.")
            return

        print(f"exchange_code가 없는 해외주식 설정: {len(settings_list)}개")
        print("-" * 50)

        updated_count = 0
        failed_count = 0
        for settings in settings_list:
            try:
                exchange_code = await get_us_exchange_by_symbol(settings.symbol, db)
                print(f"  {settings.symbol}: {exchange_code}")
            except Exception as exc:
                failed_count += 1
                print(f"  {settings.symbol}: 조회 실패 -> {exc}")
                continue

            # 업데이트
            stmt = (
                update(SymbolTradeSettings)
                .where(SymbolTradeSettings.id == settings.id)
                .values(exchange_code=exchange_code)
            )
            await db.execute(stmt)
            updated_count += 1

        await db.commit()
        print("-" * 50)
        print(f"총 {updated_count}개 설정 업데이트 완료")
        if failed_count:
            print(f"조회 실패 {failed_count}개")


if __name__ == "__main__":
    asyncio.run(fix_exchange_codes())
