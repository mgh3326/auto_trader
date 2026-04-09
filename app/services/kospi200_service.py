"""KOSPI200 constituent management service."""

from __future__ import annotations

import logging
from typing import Any

from app.models.kospi200 import Kospi200Constituent

logger = logging.getLogger(__name__)


class Kospi200Service:
    """KOSPI200 구성종목 관리 서비스"""

    def __init__(self, db_session):
        self.db = db_session

    async def get_all_constituents(
        self, active_only: bool = True
    ) -> list[Kospi200Constituent]:
        """KOSPI200 구성종목 목록 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent)

        if active_only:
            query = query.where(Kospi200Constituent.is_active == True)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_constituent_by_code(
        self, stock_code: str
    ) -> Kospi200Constituent | None:
        """종목코드로 구성종목 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent).where(
            Kospi200Constituent.stock_code == stock_code
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def update_constituents(
        self, constituents_data: list[dict[str, Any]]
    ) -> dict[str, int]:
        """KOSPI200 구성종목 정보를 업데이트합니다.

        Args:
            constituents_data: KRX에서 가져온 종목 데이터 목록

        Returns:
            Dict: 업데이트 결과 통계
            {
                "added": 10,
                "updated": 180,
                "removed": 5
            }
        """
        from datetime import datetime as dt

        from sqlalchemy import select, update

        added = 0
        updated = 0
        removed = 0

        now = dt.now()

        existing_codes_query = select(Kospi200Constituent.stock_code).where(
            Kospi200Constituent.is_active == True
        )
        existing_codes_result = await self.db.execute(existing_codes_query)
        existing_codes = {row[0] for row in existing_codes_result.fetchall()}

        new_codes = set()

        for data in constituents_data:
            stock_code = data["stock_code"]
            new_codes.add(stock_code)

            existing = await self.get_constituent_by_code(stock_code)

            if existing:
                if existing.is_active is True:
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            updated_at=now,
                        )
                    )
                    updated += 1
                else:
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            is_active=True,
                            removed_at=None,
                            added_at=now,
                            updated_at=now,
                        )
                    )
                    added += 1
            else:
                new_constituent = Kospi200Constituent(
                    stock_code=stock_code,
                    stock_name=data["stock_name"],
                    market_cap=data["market_cap"],
                    weight=data["weight"],
                    sector=data["sector"],
                    is_active=True,
                    added_at=now,
                )
                self.db.add(new_constituent)
                added += 1

        removed_codes = existing_codes - new_codes
        if removed_codes:
            await self.db.execute(
                update(Kospi200Constituent)
                .where(Kospi200Constituent.stock_code.in_(removed_codes))
                .values(is_active=False, removed_at=now, updated_at=now)
            )
            removed = len(removed_codes)

        await self.db.commit()

        logger.info(
            "KOSPI200 구성종목 업데이트 완료: 추가=%d, 업데이트=%d, 제외=%d",
            added,
            updated,
            removed,
        )

        return {"added": added, "updated": updated, "removed": removed}
