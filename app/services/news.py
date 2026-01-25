"""
뉴스 및 공지사항 수집 서비스

- 네이버 뉴스 검색 API
- 업비트 공지사항 크롤링
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings


class NewsService:
    """뉴스 수집 서비스"""

    def __init__(self):
        self.naver_client_id = getattr(settings, "naver_client_id", None)
        self.naver_client_secret = getattr(settings, "naver_client_secret", None)

    async def get_naver_news(
        self,
        keyword: str,
        display: int = 5,
        sort: str = "date",  # date: 최신순, sim: 관련도순
    ) -> List[dict]:
        """
        네이버 뉴스 검색 API

        Args:
            keyword: 검색 키워드 (예: "비트코인", "이더리움")
            display: 검색 결과 개수 (최대 100)
            sort: 정렬 방식 (date: 최신순, sim: 관련도순)

        Returns:
            List[dict]: 뉴스 리스트
                - title: 제목
                - description: 요약
                - link: 링크
                - pub_date: 발행일
        """
        if not self.naver_client_id or not self.naver_client_secret:
            # API 키가 없으면 빈 리스트 반환
            return []

        url = "https://openapi.naver.com/v1/search/news.json"
        headers = {
            "X-Naver-Client-Id": self.naver_client_id,
            "X-Naver-Client-Secret": self.naver_client_secret,
        }
        params = {
            "query": keyword,
            "display": display,
            "sort": sort,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()

            news_list = []
            for item in data.get("items", []):
                # HTML 태그 제거
                title = self._clean_html(item.get("title", ""))
                description = self._clean_html(item.get("description", ""))

                news_list.append({
                    "title": title,
                    "description": description,
                    "link": item.get("link", ""),
                    "pub_date": item.get("pubDate", ""),
                })

            return news_list

        except Exception as e:
            print(f"네이버 뉴스 검색 실패: {e}")
            return []

    async def get_upbit_notices(
        self,
        keyword: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """
        업비트 공지사항 크롤링

        Args:
            keyword: 필터링 키워드 (예: "비트코인", "상장", "폐지")
            limit: 최대 개수

        Returns:
            List[dict]: 공지사항 리스트
                - title: 제목
                - link: 링크
                - date: 날짜
                - category: 카테고리 (있는 경우)
        """
        url = "https://api-manager.upbit.com/api/v1/notices"
        params = {
            "page": 1,
            "per_page": 20,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            notices = []
            for item in data.get("data", {}).get("list", []):
                title = item.get("title", "")

                # 키워드 필터링
                if keyword and keyword.lower() not in title.lower():
                    continue

                notices.append({
                    "title": title,
                    "link": f"https://upbit.com/service_center/notice?id={item.get('id', '')}",
                    "date": item.get("created_at", "")[:10],  # YYYY-MM-DD
                    "category": item.get("category", ""),
                })

                if len(notices) >= limit:
                    break

            return notices

        except Exception as e:
            print(f"업비트 공지사항 조회 실패: {e}")
            return []

    async def get_upbit_notices_html(
        self,
        keyword: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """
        업비트 공지사항 HTML 크롤링 (백업용)
        API가 안 될 때 사용
        """
        url = "https://upbit.com/service_center/notice"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            notices = []

            # 공지사항 목록 파싱 (구조에 따라 조정 필요)
            for item in soup.select(".notice-list-item, .NoticeList__Item"):
                title_elem = item.select_one(".title, a")
                if not title_elem:
                    continue

                title = title_elem.get_text(strip=True)

                # 키워드 필터링
                if keyword and keyword.lower() not in title.lower():
                    continue

                link = title_elem.get("href", "")
                if link and not link.startswith("http"):
                    link = f"https://upbit.com{link}"

                date_elem = item.select_one(".date, time")
                date = date_elem.get_text(strip=True) if date_elem else ""

                notices.append({
                    "title": title,
                    "link": link,
                    "date": date,
                })

                if len(notices) >= limit:
                    break

            return notices

        except Exception as e:
            print(f"업비트 공지사항 HTML 크롤링 실패: {e}")
            return []

    async def get_coin_news(
        self,
        coin_name: str,
        include_notices: bool = True,
    ) -> dict:
        """
        특정 코인 관련 뉴스 및 공지 통합 조회

        Args:
            coin_name: 코인 이름 (예: "비트코인", "이더리움")
            include_notices: 업비트 공지 포함 여부

        Returns:
            dict: {
                "news": List[dict],  # 네이버 뉴스
                "notices": List[dict],  # 업비트 공지
                "summary": str,  # 요약 문자열 (프롬프트용)
            }
        """
        # 병렬로 뉴스와 공지 조회
        tasks = [
            self.get_naver_news(f"{coin_name} 암호화폐", display=5),
        ]

        if include_notices:
            tasks.append(self.get_upbit_notices(keyword=coin_name, limit=3))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        news = results[0] if not isinstance(results[0], Exception) else []
        notices = results[1] if len(results) > 1 and not isinstance(results[1], Exception) else []

        # 프롬프트용 요약 생성
        summary = self._generate_summary(coin_name, news, notices)

        return {
            "news": news,
            "notices": notices,
            "summary": summary,
        }

    def _generate_summary(
        self,
        coin_name: str,
        news: List[dict],
        notices: List[dict],
    ) -> str:
        """프롬프트에 삽입할 뉴스 요약 생성"""
        lines = []

        if notices:
            lines.append(f"[업비트 공지 - {coin_name} 관련]")
            for n in notices[:3]:
                lines.append(f"- [{n['date']}] {n['title']}")

        if news:
            lines.append(f"\n[최근 뉴스 - {coin_name}]")
            for n in news[:5]:
                # 날짜 파싱 시도
                pub_date = n.get("pub_date", "")
                if pub_date:
                    try:
                        dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
                        pub_date = dt.strftime("%m/%d")
                    except ValueError:
                        pub_date = pub_date[:10]

                # 제목
                lines.append(f"- [{pub_date}] {n['title']}")

                # 요약(description)이 있으면 추가
                description = n.get("description", "").strip()
                if description:
                    # 너무 길면 잘라서 표시 (최대 100자)
                    if len(description) > 100:
                        description = description[:100] + "..."
                    lines.append(f"  → {description}")

        if not lines:
            return ""

        return "\n".join(lines)

    def _clean_html(self, text: str) -> str:
        """HTML 태그 및 특수문자 제거"""
        # HTML 태그 제거
        text = re.sub(r"<[^>]+>", "", text)
        # HTML 엔티티 변환
        text = text.replace("&quot;", '"')
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<")
        text = text.replace("&gt;", ">")
        text = text.replace("&apos;", "'")
        return text.strip()


# 싱글톤 인스턴스
news_service = NewsService()
