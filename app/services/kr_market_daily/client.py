"""Strict, paced, unauthenticated Naver sources for KR market-wide daily data."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

INVESTOR_NAMES = {
    "8000": "개인",
    "9000": "외국인",
    "9001": "외국인(기타)",
    "1000": "금융투자",
    "2000": "보험",
    "3000": "투신(사모)",
    "3100": "투신(사모)",
    "4000": "은행",
    "5000": "기타금융기관",
    "6000": "연기금등",
    "7100": "기타법인(추정)",
    "7000": "기타",
}
ALLOWED_HOSTS = {"m.stock.naver.com", "stock.naver.com"}
SOURCE = "naver"
AMOUNT_UNIT = "KRW"
DEPOSIT_UNIT = "억원"
QUANTITY_UNIT = "주"
PAGE_SIZE = 100


class NaverMarketDailyError(RuntimeError):
    """A source-specific failure; failed responses must never become empty data."""


class BreadthFetchError(NaverMarketDailyError):
    """Naver breadth request or response failed."""


class InvestorFetchError(NaverMarketDailyError):
    """Naver investor-flow request or response failed."""


class DepositFetchError(NaverMarketDailyError):
    """Naver deposit/credit request or response failed."""


class _Client:
    def __init__(self, *, http: Any = httpx, sleep: Any = asyncio.sleep) -> None:
        self._http = http
        self._sleep = sleep
        self._last_request: float | None = None

    async def get_json(
        self, url: str, params: dict[str, str], error: type[NaverMarketDailyError]
    ) -> object:
        host = urlsplit(url).hostname
        if host not in ALLOWED_HOSTS:
            raise error("Naver host is not allowlisted")
        if self._last_request is not None:
            delay = 1.0 - (time.monotonic() - self._last_request)
            if delay > 0:
                await self._sleep(delay)
        try:
            async with self._http.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "auto-trader-kr-market-daily/1.0",
                },
            ) as client:
                self._last_request = time.monotonic()
                response = await client.get(url, params=params)
                response.raise_for_status()
                if (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise error("Naver response content type is not application/json")
                return response.json()
        except NaverMarketDailyError:
            raise
        except Exception as exc:
            raise error(f"Naver request failed: {type(exc).__name__}") from exc


def _decimal(value: object, *, field: str) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise ValueError(f"{field} must be an exact decimal string")
    text = str(value).strip().replace(",", "")
    if not text:
        raise ValueError(f"{field} is empty")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not decimal") from exc


def _items(payload: object, key: str) -> list[Mapping[str, Any]]:
    rows = payload.get(key) if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"Naver payload must contain a {key} list of objects")
    return rows


def parse_breadth_page(payload: object) -> tuple[int, list[Mapping[str, Any]]]:
    if not isinstance(payload, Mapping):
        raise ValueError("breadth payload must be an object")
    rows = _items(payload, "stocks")
    total = payload.get("totalCount")
    if type(total) is not int or total < 0:
        raise ValueError("breadth totalCount must be a nonnegative integer")
    return total, rows


def count_breadth(rows: list[Mapping[str, Any]]) -> tuple[int, int, int]:
    up = down = flat = 0
    for row in rows:
        change = _decimal(
            row.get("compareToPreviousClosePrice"), field="compareToPreviousClosePrice"
        )
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
    return up, down, flat


def parse_investor_rows(payload: object, expected_date: date) -> list[dict[str, Any]]:
    rows = _items(payload, "content")
    output: list[dict[str, Any]] = []
    for row in rows:
        bizdate = str(row.get("bizdate", ""))
        if bizdate != expected_date.strftime("%Y%m%d"):
            continue
        amounts = row.get("netAmounts")
        if not isinstance(amounts, list) or any(
            not isinstance(item, Mapping) for item in amounts
        ):
            raise ValueError("investor netAmounts must be a list of objects")
        for item in amounts:
            code = item.get("investorGubun")
            if not isinstance(code, str) or not code:
                raise ValueError("investorGubun must be a nonempty string")
            output.append(
                {
                    "investor_code": code,
                    "investor_name": INVESTOR_NAMES.get(code),
                    "net_amount": _decimal(item.get("diffValue"), field="diffValue"),
                    "buy_amount": _decimal(item.get("buyPrice"), field="buyPrice"),
                    "sell_amount": _decimal(item.get("sellPrice"), field="sellPrice"),
                    "buy_quantity": _decimal(item.get("buyQuant"), field="buyQuant"),
                    "sell_quantity": _decimal(item.get("sellQuant"), field="sellQuant"),
                    "amount_unit": AMOUNT_UNIT,
                    "quantity_unit": QUANTITY_UNIT,
                }
            )
    return output


def parse_deposit_rows(payload: object) -> list[dict[str, Any]]:
    rows = _items(payload, "content")
    output: list[dict[str, Any]] = []
    for row in rows:
        raw_date = row.get("bizdate")
        if (
            not isinstance(raw_date, str)
            or len(raw_date) != 8
            or not raw_date.isdigit()
        ):
            raise ValueError("deposit bizdate must be YYYYMMDD")
        try:
            bizdate = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
        except ValueError as exc:
            raise ValueError("deposit bizdate is invalid") from exc
        output.append(
            {
                "bizdate": bizdate,
                "customer_deposit": _decimal(
                    row["customerDeposit"], field="customerDeposit"
                ),
                "credit_loan": _decimal(row["creditLoan"], field="creditLoan"),
                "credit_loan_diff": _decimal(
                    row["creditLoanDiff"], field="creditLoanDiff"
                ),
                "beneficiary_certificate": _decimal(
                    row["beneficiaryCertificate"], field="beneficiaryCertificate"
                )
                if row.get("beneficiaryCertificate") is not None
                else None,
                "amount_unit": DEPOSIT_UNIT,
            }
        )
    return output


class NaverMarketDailyClient(_Client):
    async def fetch_breadth(self, market: str) -> dict[str, Any]:
        if market not in {"KOSPI", "KOSDAQ"}:
            raise BreadthFetchError("unsupported breadth market")
        url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
        rows: list[Mapping[str, Any]] = []
        total: int | None = None
        page = 1
        try:
            while True:
                payload = await self.get_json(
                    url,
                    {"page": str(page), "pageSize": str(PAGE_SIZE)},
                    BreadthFetchError,
                )
                page_total, page_rows = parse_breadth_page(payload)
                if total is None:
                    total = page_total
                elif total != page_total:
                    raise ValueError("breadth totalCount changed while paging")
                rows.extend(page_rows)
                if not page_rows or len(rows) >= total:
                    break
                page += 1
            up, down, flat = count_breadth(rows)
        except Exception as exc:
            raise BreadthFetchError(
                f"breadth payload invalid: {type(exc).__name__}"
            ) from exc
        assert total is not None
        return {
            "market": market,
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "total_count": total,
            "fetched_count": len(rows),
            "complete": len(rows) == total,
        }

    async def fetch_investors(self, market: str, bizdate: date) -> list[dict[str, Any]]:
        url = "https://stock.naver.com/api/domestic/market/trend/daily"
        params = {
            "tradeType": "KRX",
            "marketType": market,
            "bizdate": bizdate.strftime("%Y%m%d"),
            "startIdx": "0",
            "pageSize": "30",
        }
        try:
            payload = await self.get_json(url, params, InvestorFetchError)
            return parse_investor_rows(payload, bizdate)
        except Exception as exc:
            raise InvestorFetchError(
                f"investor payload invalid: {type(exc).__name__}"
            ) from exc

    async def fetch_deposits(self) -> list[dict[str, Any]]:
        url = "https://stock.naver.com/api/domestic/market/trendDeposit"
        try:
            payload = await self.get_json(
                url, {"startIdx": "0", "pageSize": "20"}, DepositFetchError
            )
            return parse_deposit_rows(payload)
        except Exception as exc:
            raise DepositFetchError(
                f"deposit payload invalid: {type(exc).__name__}"
            ) from exc
