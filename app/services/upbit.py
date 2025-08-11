import pandas as pd
import httpx
from datetime import datetime, timezone, timedelta
from typing import Literal

UPBIT_REST = "https://api.upbit.com/v1"


async def _request_json(url: str, params: dict | None = None) -> list[dict]:
    """공용 GET 엔드포인트용 헬퍼 (API Key 필요 없음)"""
    async with httpx.AsyncClient(timeout=5) as cli:
        r = await cli.get(url, params=params)
        r.raise_for_status()
        return r.json()


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
    rows = await _request_json(url, {"markets": market})
    if not rows:
        raise ValueError(f"시장 {market} 반환 데이터 없음")

    t = rows[0]
    now = datetime.now(timezone.utc)

    row = {
        "code": market,
        "date": now.date(),
        "time": now.time(),
        "open": float(t["opening_price"]),
        "high": float(t["high_price"]),
        "low": float(t["low_price"]),
        "close": float(t["trade_price"]),
        "volume": float(t["acc_trade_volume_24h"]),
        "value": float(t["acc_trade_price_24h"]),
    }
    return pd.DataFrame([row]).reset_index(drop=True)


# --- 작은 데모 스크립트 (직접 실행 시) ----------------------------------------
if __name__ == "__main__":
    import asyncio, pprint

    async def demo():
        df = await fetch_ohlcv("KRW-BTC", 5)
        pprint.pp(df)
        now = await fetch_price("KRW-BTC")
        print(now.T)

    asyncio.run(demo())
