from datetime import datetime, timezone, timedelta
from typing import Literal
import uuid
import jwt  # pyjwt 라이브러리가 필요합니다 (pip install pyjwt)

import httpx
import pandas as pd

from app.core.config import settings

UPBIT_REST = "https://api.upbit.com/v1"


# --- 인증 정보 (실제 키로 교체 필요) ---
# 보안을 위해 환경 변수나 다른 안전한 방법을 사용하세요.
# 예: import os; UPBIT_ACCESS_KEY = os.environ.get("UPBIT_ACCESS_KEY")


async def _request_json(url: str, params: dict | None = None) -> list[dict]:
    """공용 GET 엔드포인트용 헬퍼 (API Key 필요 없음)"""
    async with httpx.AsyncClient(timeout=5) as cli:
        r = await cli.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def fetch_my_coins() -> list[dict]:
    """보유자산 리스트 반환 (API Key 필요)"""
    payload = {
        'access_key': settings.upbit_access_key,
        'nonce': str(uuid.uuid4()),
    }

    jwt_token = jwt.encode(payload, settings.upbit_secret_key)
    authorize_token = f'Bearer {jwt_token}'
    headers = {"Authorization": authorize_token}

    async with httpx.AsyncClient(timeout=5) as cli:
        res = await cli.get(f"{UPBIT_REST}/accounts", headers=headers)
        res.raise_for_status()
        return res.json()


async def fetch_ohlcv(
        market: str = "KRW-BTC",
        days: int = 100,
        adjust_price: Literal["true", "false"] = "false",
) -> pd.DataFrame:
    """최근 *days*개 일봉 OHLCV DataFrame 반환 (Upbit)

    Parameters
    ----------
    market : str, default "KRW-BTC"
        업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    days : int, default 100
        가져올 캔들 수 (최대 200)
    adjust_price : "true" | "false", default "false"
        리브랜딩·액면분할 등 보정 여부 (업비트는 대부분 false)
    """
    if days > 200:
        raise ValueError("Upbit 일봉 API는 최대 200개까지 요청 가능합니다.")

    url = f"{UPBIT_REST}/candles/days"
    params = {
        "market": market,
        "count": days,
        "convertingPriceUnit": adjust_price,
    }
    rows = await _request_json(url, params)

    df = (
        pd.DataFrame(rows)
        .rename(
            columns={
                "candle_date_time_kst": "datetime",
                "opening_price": "open",
                "high_price": "high",
                "low_price": "low",
                "trade_price": "close",
                "candle_acc_trade_volume": "volume",
                "candle_acc_trade_price": "value",
            }
        )
        .assign(
            date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
        )
        .loc[:, ["date", "open", "high", "low", "close", "volume", "value"]]
        .sort_values("date")
        .reset_index(drop=True)
    )
    return df


async def fetch_price(market: str = "KRW-BTC") -> pd.DataFrame:
    """실시간 현재가 1행 DataFrame 반환 (Upbit)

    반환 컬럼: date · time · open · high · low · close · volume · value
    (open/high/low 는 24시간 기준, value=24h 누적 거래대금)
    """
    url = f"{UPBIT_REST}/ticker"
    params = {"markets": market}
    rows = await _request_json(url, params)

    if not rows:
        raise ValueError(f"마켓 {market}에 대한 데이터를 찾을 수 없습니다.")

    row = rows[0]

    # 현재 시간을 KST로 설정
    now = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    kst_time = now.astimezone(timezone(timedelta(hours=9)))

    df = pd.DataFrame([{
        "date": kst_time.date(),
        "time": kst_time.time(),
        "open": row["opening_price"],
        "high": row["high_price"],
        "low": row["low_price"],
        "close": row["trade_price"],
        "volume": row["acc_trade_volume_24h"],
        "value": row["acc_trade_price_24h"],
    }])

    return df


async def fetch_minute_candles(
    market: str = "KRW-BTC",
    unit: int = 1,
    count: int = 200
) -> pd.DataFrame:
    """분봉 캔들 데이터를 가져오는 메서드 (Upbit)

    Parameters
    ----------
    market : str, default "KRW-BTC"
        업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    unit : int, default 1
        분봉 단위 (1, 3, 5, 10, 15, 30, 60, 240)
    count : int, default 200
        가져올 캔들 수 (최대 200)

    Returns
    -------
    pd.DataFrame
        컬럼: datetime, open, high, low, close, volume, value
    """
    if unit not in [1, 3, 5, 10, 15, 30, 60, 240]:
        raise ValueError("unit은 1, 3, 5, 10, 15, 30, 60, 240 중 하나여야 합니다.")

    if count > 200:
        raise ValueError("Upbit 분봉 API는 최대 200개까지 요청 가능합니다.")

    url = f"{UPBIT_REST}/candles/minutes/{unit}"
    params = {
        "market": market,
        "count": count,
    }

    rows = await _request_json(url, params)

    df = (
        pd.DataFrame(rows)
        .rename(
            columns={
                "candle_date_time_kst": "datetime",
                "opening_price": "open",
                "high_price": "high",
                "low_price": "low",
                "trade_price": "close",
                "candle_acc_trade_volume": "volume",
                "candle_acc_trade_price": "value",
            }
        )
        .assign(
            datetime=lambda d: pd.to_datetime(d["datetime"]),
            date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
            time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
        )
        .loc[:, ["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"]]
        .sort_values("datetime")
        .reset_index(drop=True)
    )

    return df


async def fetch_hourly_candles(market: str = "KRW-BTC", count: int = 24) -> pd.DataFrame:
    """60분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=60, count=count)


async def fetch_5min_candles(market: str = "KRW-BTC", count: int = 24) -> pd.DataFrame:
    """5분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=5, count=count)


async def fetch_1min_candles(market: str = "KRW-BTC", count: int = 20) -> pd.DataFrame:
    """1분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=1, count=count)


async def fetch_fundamental_info(market: str = "KRW-BTC") -> dict:
    """
    암호화폐의 기본 정보를 가져와 딕셔너리로 반환합니다.
    :param market: 업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    :return: 기본 정보 딕셔너리
    """
    url = f"{UPBIT_REST}/ticker"
    rows = await _request_json(url, {"markets": market})
    if not rows:
        raise ValueError(f"시장 {market} 반환 데이터 없음")

    t = rows[0]

    # 기본 정보 구성
    fundamental_data = {
        "마켓코드": t.get("market"),
        "현재가": t.get("trade_price"),
        "24시간변동": t.get("signed_change_price"),
        "24시간변동률": t.get("signed_change_rate"),
        "24시간고가": t.get("high_price"),
        "24시간저가": t.get("low_price"),
        "24시간거래량": t.get("acc_trade_volume_24h"),
        "24시간거래대금": t.get("acc_trade_price_24h"),
        "최고가": t.get("highest_52_week_price"),
        "최저가": t.get("lowest_52_week_price"),
        "최고가대비": t.get("highest_52_week_ratio"),
        "최저가대비": t.get("lowest_52_week_ratio"),
    }

    # None이 아닌 값만 반환
    return {k: v for k, v in fundamental_data.items() if v is not None}


# --- 작은 데모 스크립트 (직접 실행 시) ----------------------------------------
if __name__ == "__main__":
    import asyncio
    import pprint


    async def demo():
        # # 기존 데모 코드
        # df = await fetch_ohlcv("KRW-BTC", 5)
        # pprint.pp(df)
        # now = await fetch_price("KRW-BTC")
        # print(now.T)
        try:
            print("--- 내 보유 자산 ---")
            my_coins = await fetch_my_coins()
            pprint.pp(my_coins)
        except httpx.HTTPStatusError as e:
            print(f"API 호출에 실패했습니다: {e.response.status_code}")
            print(f"응답 내용: {e.response.text}")
        except Exception as e:
            print(f"오류 발생: {e}")


    asyncio.run(demo())
