"""KRX Market Data Service for fetching KOSPI200 constituents via CSV download."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KRXMarketDataService:
    """KRX 마켓 데이터 서비스"""

    KRX_DOWNLOAD_URL = "https://data.krx.co.kr/comm/fileDn/DownloadOfFileService"

    async def fetch_kospi200_constituents(self) -> list[dict[str, Any]]:
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

    def _parse_krx_csv_content(self, content: str) -> list[dict[str, Any]]:
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

            stock_code = row.get("종목코드", "")
            if stock_code.startswith("KR7"):
                stock_code = stock_code[4:]

            market_cap_str = row.get("시가총액", "0").replace(",", "")
            try:
                market_cap = float(market_cap_str) if market_cap_str else 0.0
            except ValueError:
                market_cap = 0.0

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
