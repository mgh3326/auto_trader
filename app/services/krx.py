"""KRX (Korea Exchange) Market Data Service"""

import logging
from datetime import datetime

import httpx

from app.models.kospi200 import Kospi200Constituent

logger = logging.getLogger(__name__)


class KRXMarketDataService:
    """KRX 마켓 데이터 서비스"""

    KRX_DOWNLOAD_URL = "http://data.krx.co.kr/comm/fileDn/DownloadOfFileService"

    async def fetch_kospi200_constituents(self) -> list[dict]:
        """KRX에서 KOSPI200 구성종목 데이터를 가져옵니다.

        Returns:
            List[Dict]: 종목 정보 목록
            {
                "종목코드": "005930",
                "종목명": "삼성전자",
                "시가총액": 1234567890,
                "지수비중": 1.23,
                "섹터": "전기전자"
            }
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {
                "mktId": "STK",
                "trdDd": datetime.now().strftime("%Y%m%d"),
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }

            try:
                response = await client.post(
                    self.KRX_DOWNLOAD_URL,
                    data=params,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0",
                    },
                )

                if response.status_code == 200:
                    content = response.text
                    return self._parse_krx_csv_content(content)
                else:
                    logger.error(
                        "KRX API 호출 실패: status_code=%d", response.status_code
                    )
                    return []

            except Exception as e:
                logger.error("KRX 데이터 수집 중 오류 발생: %s", e)
                return []

    def _parse_krx_csv_content(self, content: str) -> list[dict]:
        """KRX에서 반환된 CSV 형식의 데이터를 파싱합니다."""
        if not content or len(content) < 100:
            logger.warning("KRX 응답 데이터가 비어있거나 너무 짧습니다")
            return []

        lines = content.split("\n")
        if len(lines) < 2:
            return []

        headers = lines[0].split("\t")
        constituents = []

        for line in lines[1:]:
            if not line.strip():
                continue

            values = line.split("\t")
            if len(values) < len(headers):
                continue

            row = dict(zip(headers, values, strict=False))

            # 종목코드 형식: 'KR70684000' 또는 '005930'
            stock_code = row.get("종목코드", "")
            if stock_code.startswith("KR7"):
                stock_code = stock_code[4:]

            # 시가총액 파싱 (숫자로 변환)
            market_cap_str = row.get("시가총액", "0").replace(",", "")
            try:
                market_cap = float(market_cap_str) if market_cap_str else 0.0
            except ValueError:
                market_cap = 0.0

            # 지수비중 파싱
            weight_str = row.get("지수비중", "0").replace(",", "")
            try:
                weight = float(weight_str) if weight_str else 0.0
            except ValueError:
                weight = 0.0

            constituents.append(
                {
                    "stock_code": stock_code,
                    "stock_name": row.get("종목명", ""),
                    "market_cap": market_cap,
                    "weight": weight,
                    "sector": row.get("섹터", ""),
                }
            )

        return constituents


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
        self, constituents_data: list[dict]
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

        # 현재 DB에 있는 모든 구성종목의 코드를 가져옴
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
                # 기존 종목 업데이트
                if existing.is_active:
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
                    # 이전에 제외되었던 종목이 다시 포함됨
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
                # 새로운 종목 추가
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

        # KRX 목록에 없는 종목들은 구성종목에서 제외 처리
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
