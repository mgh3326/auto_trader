from datetime import datetime, timezone, timedelta
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


async def fetch_krw_balance() -> float:
    """KRW 잔고 조회 (원화 잔고만 반환)
    
    Returns
    -------
    float
        KRW 잔고 (원)
    """
    accounts = await fetch_my_coins()
    
    for account in accounts:
        if account.get('currency') == 'KRW':
            balance = float(account.get('balance', 0))
            locked = float(account.get('locked', 0))
            # 사용 가능한 잔고만 반환 (locked 제외)
            return balance
    
    # KRW 계정이 없으면 0 반환
    return 0.0


async def check_krw_balance_sufficient(required_amount: float) -> tuple[bool, float]:
    """KRW 잔고가 충분한지 확인
    
    Parameters
    ----------
    required_amount : float
        필요한 금액
    
    Returns
    -------
    tuple[bool, float]
        (충분 여부, 현재 KRW 잔고)
    """
    current_balance = await fetch_krw_balance()
    is_sufficient = current_balance >= required_amount
    
    return is_sufficient, current_balance


async def fetch_ohlcv(
        market: str = "KRW-BTC",
        days: int = 100,
) -> pd.DataFrame:
    """최근 *days*개 일봉 OHLCV DataFrame 반환 (Upbit)

    Parameters
    ----------
    market : str, default "KRW-BTC"
        업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    days : int, default 100
        가져올 캔들 수 (최대 200)
    """
    if days > 200:
        raise ValueError("Upbit 일봉 API는 최대 200개까지 요청 가능합니다.")

    url = f"{UPBIT_REST}/candles/days"
    params = {
        "market": market,
        "count": days,
    }
    # convertingPriceUnit 파라미터는 업비트에서 지원하지 않거나 다른 이름일 수 있음
    # 필요시 아래와 같이 추가 가능
    # if adjust_price == "true":
    #     params["convertingPriceUnit"] = "true"
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


async def fetch_all_market_codes(fiat: str = "KRW") -> list[str]:
    """
    업비트에서 거래 가능한 모든 마켓 코드를 조회합니다.
    :param fiat: 조회할 화폐 시장 (기본값: "KRW")
    :return: 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH", ...])
    """
    url = f"{UPBIT_REST}/market/all"
    params = {"isDetails": "false"}
    all_markets = await _request_json(url, params)

    # 지정된 fiat 시장의 마켓 코드만 필터링하여 반환
    return [m["market"] for m in all_markets if m["market"].startswith(fiat)]


async def fetch_top_traded_coins(fiat: str = "KRW") -> list[dict]:
    """
    지정된 fiat 시장의 모든 코인을 24시간 거래대금 순으로 정렬하여 반환합니다.
    """
    # 1. 거래 가능한 모든 KRW 마켓 코드를 가져옵니다.
    market_codes = await fetch_all_market_codes(fiat)

    # 2. 모든 마켓 코드의 현재가 정보를 한 번의 API 호출로 가져옵니다.
    all_tickers_info = await fetch_multiple_tickers(market_codes)

    # 3. 24시간 누적 거래대금(acc_trade_price_24h) 기준으로 내림차순 정렬합니다.
    sorted_coins = sorted(
        all_tickers_info,
        key=lambda x: x.get("acc_trade_price_24h", 0),
        reverse=True
    )

    return sorted_coins


async def fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
    """
    여러 마켓의 현재가 정보를 한 번에 조회합니다.
    
    Parameters
    ----------
    market_codes : list[str]
        조회할 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH"])
    
    Returns
    -------
    list[dict]
        각 마켓의 현재가 정보 리스트
        각 항목은 다음과 같은 정보를 포함:
        - market: 마켓명 (예: "KRW-BTC")
        - trade_price: 현재가
        - signed_change_price: 24시간 변동 금액
        - signed_change_rate: 24시간 변동률
        - high_price: 24시간 최고가
        - low_price: 24시간 최저가
        - acc_trade_volume_24h: 24시간 누적 거래량
        - acc_trade_price_24h: 24시간 누적 거래대금
    """
    if not market_codes:
        return []
    
    url = f"{UPBIT_REST}/ticker"
    params = {"markets": ",".join(market_codes)}
    
    return await _request_json(url, params)


async def fetch_multiple_current_prices(market_codes: list[str]) -> dict[str, float]:
    """
    여러 마켓의 현재가만 간단히 조회하여 딕셔너리로 반환합니다.
    
    Parameters
    ----------
    market_codes : list[str]
        조회할 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH"])
    
    Returns
    -------
    dict[str, float]
        마켓별 현재가 딕셔너리 (예: {"KRW-BTC": 95000000, "KRW-ETH": 4400000})
    """
    tickers_data = await fetch_multiple_tickers(market_codes)
    
    return {
        item["market"]: item["trade_price"] 
        for item in tickers_data
    }


async def _request_with_auth(method: str, url: str, query_params: dict = None, body_params: dict = None) -> dict:
    """인증이 필요한 API 요청을 처리하는 헬퍼 함수"""
    import hashlib
    from urllib.parse import urlencode
    import json
    
    payload = {
        'access_key': settings.upbit_access_key,
        'nonce': str(uuid.uuid4()),
    }
    
    # GET/DELETE 요청: query_params로 query_hash 생성
    if method.upper() in ["GET", "DELETE"] and query_params:
        query_string = urlencode(query_params, doseq=True)
        payload['query_hash'] = hashlib.sha512(query_string.encode()).hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    
    # POST 요청: body_params로 query_hash 생성
    elif method.upper() == "POST" and body_params:
        query_string = urlencode(body_params, doseq=True)
        payload['query_hash'] = hashlib.sha512(query_string.encode()).hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    
    jwt_token = jwt.encode(payload, settings.upbit_secret_key)
    authorize_token = f'Bearer {jwt_token}'
    headers = {"Authorization": authorize_token}
    
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
    
    async with httpx.AsyncClient(timeout=10) as cli:
        if method.upper() == "GET":
            response = await cli.get(url, headers=headers, params=query_params)
        elif method.upper() == "POST":
            response = await cli.post(url, headers=headers, json=body_params)
        elif method.upper() == "DELETE":
            response = await cli.delete(url, headers=headers, params=query_params)
        else:
            raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")
        
        response.raise_for_status()
        return response.json()


async def fetch_open_orders(market: str = None) -> list[dict]:
    """체결 대기 중인 주문 목록을 조회합니다.
    
    Parameters
    ----------
    market : str, optional
        특정 마켓만 조회하려면 마켓 코드 지정 (예: "KRW-BTC")
        None이면 모든 마켓의 주문을 조회
    
    Returns
    -------
    list[dict]
        체결 대기 중인 주문 목록
        각 주문은 다음과 같은 정보를 포함:
        - uuid: 주문 고유 ID
        - side: 주문 종류 (bid: 매수, ask: 매도)
        - ord_type: 주문 타입 (limit, price, market)
        - price: 주문 가격
        - volume: 주문 수량
        - remaining_volume: 미체결 수량
        - market: 마켓명
        - created_at: 주문 시간
        - state: 주문 상태 (wait: 체결대기, cancel: 취소)
    """
    url = f"{UPBIT_REST}/orders"
    params = {}
    if market:
        params["market"] = market
    params["state"] = "wait"  # 체결 대기 중인 주문만
    
    return await _request_with_auth("GET", url, query_params=params)


async def cancel_orders(order_uuids: list[str]) -> list[dict]:
    """주문을 취소합니다.
    
    Parameters
    ----------
    order_uuids : list[str]
        취소할 주문들의 UUID 리스트
    
    Returns
    -------
    list[dict]
        취소된 주문들의 정보 리스트
    """
    results = []
    
    for order_uuid in order_uuids:
        url = f"{UPBIT_REST}/order"
        params = {"uuid": order_uuid}
        
        try:
            result = await _request_with_auth("DELETE", url, query_params=params)
            results.append(result)
        except Exception as e:
            print(f"주문 {order_uuid} 취소 실패: {e}")
            # 실패한 주문도 결과에 포함하되 에러 정보 추가
            results.append({"uuid": order_uuid, "error": str(e)})
    
    return results


async def place_sell_order(market: str, volume: str, price: str) -> dict:
    """지정가 매도 주문을 넣습니다.
    
    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    volume : str
        매도할 수량 (문자열로 전달)
    price : str
        매도 가격 (문자열로 전달)
    
    Returns
    -------
    dict
        주문 결과 정보
        - uuid: 주문 고유 ID
        - side: "ask" (매도)
        - ord_type: "limit"
        - price: 주문 가격
        - volume: 주문 수량
        - market: 마켓명
        - created_at: 주문 시간
    """
    url = f"{UPBIT_REST}/orders"
    
    body_params = {
        "market": market,
        "side": "ask",  # 매도
        "volume": volume,
        "price": price,
        "ord_type": "limit"  # 지정가 주문
    }
    
    return await _request_with_auth("POST", url, body_params=body_params)


async def place_market_sell_order(market: str, volume: str) -> dict:
    """시장가 전량 매도 주문을 넣습니다.
    
    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    volume : str
        매도할 수량 (문자열로 전달, 보유 전량)
    
    Returns
    -------
    dict
        주문 결과 정보
        - uuid: 주문 고유 ID
        - side: "ask" (매도)
        - ord_type: "market"
        - volume: 주문 수량
        - market: 마켓명
        - created_at: 주문 시간
    """
    url = f"{UPBIT_REST}/orders"
    
    body_params = {
        "market": market,
        "side": "ask",  # 매도
        "volume": volume,
        "ord_type": "market"  # 시장가 주문 (즉시 체결)
    }
    
    return await _request_with_auth("POST", url, body_params=body_params)


async def place_buy_order(market: str, price: str, volume: str = None, ord_type: str = "limit") -> dict:
    """매수 주문을 넣습니다.
    
    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    price : str
        매수 가격 (지정가) 또는 매수 금액 (시장가)
    volume : str, optional
        매수할 수량 (지정가일 때 필요)
    ord_type : str, default "limit"
        주문 타입 ("limit": 지정가, "price": 시장가 매수)
    
    Returns
    -------
    dict
        주문 결과 정보
        - uuid: 주문 고유 ID
        - side: "bid" (매수)
        - ord_type: 주문 타입
        - price: 주문 가격/금액
        - volume: 주문 수량 (지정가일 때)
        - market: 마켓명
        - created_at: 주문 시간
    """
    url = f"{UPBIT_REST}/orders"
    
    body_params = {
        "market": market,
        "side": "bid",  # 매수
        "ord_type": ord_type
    }
    
    if ord_type == "limit":
        # 지정가 매수: 수량과 가격 모두 필요
        if not volume:
            raise ValueError("지정가 매수는 volume이 필요합니다")
        body_params["volume"] = volume
        body_params["price"] = price
    elif ord_type == "price":
        # 시장가 매수: 매수 금액만 필요
        body_params["price"] = price
    else:
        raise ValueError("ord_type은 'limit' 또는 'price'여야 합니다")
    
    return await _request_with_auth("POST", url, body_params=body_params)


async def place_market_buy_order(market: str, price: str) -> dict:
    """시장가 매수 주문을 넣습니다 (지정 금액만큼 매수).
    
    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    price : str
        매수할 금액 (문자열로 전달, 예: "100000")
    
    Returns
    -------
    dict
        주문 결과 정보
        - uuid: 주문 고유 ID
        - side: "bid" (매수)
        - ord_type: "price"
        - price: 매수 금액
        - market: 마켓명
        - created_at: 주문 시간
    """
    return await place_buy_order(market, price, ord_type="price")


def adjust_price_to_upbit_unit(price: float) -> float:
    """업비트 가격 단위에 맞게 가격을 조정합니다.
    
    업비트 가격 단위 규칙:
    - 2,000,000원 이상: 1,000원 단위
    - 1,000,000원 이상 ~ 2,000,000원 미만: 500원 단위  
    - 500,000원 이상 ~ 1,000,000원 미만: 100원 단위
    - 100,000원 이상 ~ 500,000원 미만: 50원 단위
    - 10,000원 이상 ~ 100,000원 미만: 10원 단위
    - 1,000원 이상 ~ 10,000원 미만: 5원 단위
    - 100원 이상 ~ 1,000원 미만: 1원 단위
    - 10원 이상 ~ 100원 미만: 0.1원 단위
    - 1원 이상 ~ 10원 미만: 0.01원 단위
    - 0.1원 이상 ~ 1원 미만: 0.001원 단위
    - 0.01원 이상 ~ 0.1원 미만: 0.0001원 단위
    - 0.01원 미만: 0.00001원 단위
    
    Parameters
    ----------
    price : float
        조정할 가격
        
    Returns
    -------
    float
        업비트 단위에 맞게 조정된 가격
    """
    if price >= 2000000:
        return round(price / 1000) * 1000
    elif price >= 1000000:
        return round(price / 500) * 500
    elif price >= 500000:
        return round(price / 100) * 100
    elif price >= 100000:
        return round(price / 50) * 50
    elif price >= 10000:
        return round(price / 10) * 10
    elif price >= 1000:
        return round(price / 5) * 5
    elif price >= 100:
        return round(price)
    elif price >= 10:
        return round(price, 1)
    elif price >= 1:
        return round(price, 2)
    elif price >= 0.1:
        return round(price, 3)
    elif price >= 0.01:
        return round(price, 4)
    else:
        return round(price, 5)


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
