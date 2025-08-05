import datetime
import json
from typing import Any, Coroutine

import pandas as pd
from pandas import DataFrame

from app.core.config import settings
from app.services.token_cache import load_token, save_token
import httpx, logging, asyncio

BASE = "https://openapi.koreainvestment.com:9443"
VOL_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"
PRICE_TR = "FHKST01010100"
PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DAILY_ITEMCHARTPRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
VOL_TR = "FHPST01710000"  # 실전 전용
DAILY_ITEMCHARTPRICE_TR = "FHKST03010100"  # (일봉·주식·실전/모의 공통)


class KISClient:
    def __init__(self):
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        # ① 시작할 때 캐시 로드
        settings.kis_access_token = load_token()

    async def _fetch_token(self) -> str:
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{BASE}/oauth2/token",
                data={"grant_type": "client_credentials",
                      "appkey": settings.kis_app_key,
                      "appsecret": settings.kis_app_secret},
                timeout=5
            )
        token = r.json()["access_token"]
        save_token(token)  # ② 디스크 캐시 갱신
        logging.info("KIS 새 토큰 발급 & 캐시")
        return token

    async def _ensure_token(self):
        if settings.kis_access_token:  # 캐시 유효
            return
        settings.kis_access_token = await self._fetch_token()

    async def volume_rank(self):
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": VOL_TR,
        }
        async with httpx.AsyncClient() as cli:

            r = await cli.get(f"{BASE}{VOL_URL}", headers=hdr, params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "1",
                "FID_TRGT_CLS_CODE": "11111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000001100",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "1000000",
                "FID_VOL_CNT": "100000",
                "FID_INPUT_DATE_1": "",
            }, timeout=5)
        js = r.json()
        if js["rt_cd"] == "0":
            return js["output"]
        if js["msg_cd"] == "EGW00123":  # 토큰 만료
            settings.kis_access_token = await self._fetch_token()
            return await self.volume_rank()
        raise RuntimeError(js["msg1"])

    async def inquire_price(self, code: str, market: str = "J") -> DataFrame:
        """
        단일 종목 현재가·기본정보 조회
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/J(통합)
        :return: API output 딕셔너리
        """
        await self._ensure_token()

        # 요청 헤더
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{BASE}{PRICE_URL}", headers=hdr, params=params)
        js = r.json()
        if js["rt_cd"] != "0":
            raise RuntimeError(f'{js["msg_cd"]} {js["msg1"]}')
        out = js["output"]  # 단일 dict
        trade_date_str = out.get("stck_bsop_date")  # 예: '20250805'
        if trade_date_str:
            trade_date = pd.to_datetime(trade_date_str, format="%Y%m%d")
        else:  # 필드가 없으면 오늘 날짜
            trade_date = pd.Timestamp(datetime.date.today())

        # ── ② 체결 시각 ──
        time_str = out.get("stck_cntg_hour") or out.get("stck_cntg_time")  # 'HHMMSS'
        if time_str:
            trade_time = pd.to_datetime(time_str, format="%H%M%S").time()
        else:
            trade_time = datetime.datetime.now().time()  # 필드가 없으면 현재 시각
        row = {
            "code": out["stck_shrn_iscd"],
            "date": trade_date,
            "time": trade_time,
            "open": float(out["stck_oprc"]),
            "high": float(out["stck_hgpr"]),
            "low": float(out["stck_lwpr"]),
            "close": float(out["stck_prpr"]),
            "volume": int(out["acml_vol"]),
            "value": int(out["acml_tr_pbmn"]),
        }
        return (
            pd.DataFrame([row])
            .set_index("code")  # index = 종목코드
        )

    async def inquire_daily_itemchartprice(
            self,
            code: str,
            market: str = "J",
            n: int = 100,
            adj: bool = True,
    ) -> pd.DataFrame:
        """
        ✅ 최근 n개 일봉 OHLCV를 DataFrame으로 반환
        :param code: 6자리 종목코드
        :param market: K(코스피)/Q(코스닥)/J(통합)
        :param n: 반환할 캔들 수 (최대 100)
        :param adj: True → 수정주가(권배정 반영), False → 원본
        """
        if n > 100:
            raise ValueError("KIS 일봉 API는 호출당 최대 100건만 반환합니다.")

        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": DAILY_ITEMCHARTPRICE_TR,
        }
        today = datetime.date.today()
        start = today - datetime.timedelta(days=150)
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_PERIOD_DIV_CODE": "D",  # 일봉 고정
            "FID_ORG_ADJ_PRC": "0" if adj else "1",  # 0 = 수정, 1 = 원본
            # 날짜 범위를 지정하려면 FID_INPUT_DATE_1, FID_INPUT_DATE_2 추가
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),  # ★ 시작일
            "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),  # ★ 종료일
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{BASE}{DAILY_ITEMCHARTPRICE_URL}", headers=hdr, params=params)
        js = r.json()

        # 토큰 만료 처리
        if js["rt_cd"] == "0":
            rows = js["output2"]  # list[dict] OHLCV
        elif js["msg_cd"] == "EGW00123":
            settings.kis_access_token = await self._fetch_token()
            return await self.inquire_daily_itemchartprice(code, market, n, adj)
        else:
            raise RuntimeError(f'{js["msg_cd"]} {js["msg1"]}')

        # ── DataFrame 변환 ───────────────────────────────
        df = (
            pd.DataFrame(rows[:n])  # 최근부터 100개이므로 [:n]
            .rename(columns={
                "stck_bsop_date": "date",
                "stck_oprc": "open",
                "stck_hgpr": "high",
                "stck_lwpr": "low",
                "stck_clpr": "close",
                "acml_vol": "volume",
                "acml_tr_pbmn": "value",
            })
            .astype({
                "date": "int",
                "open": "float",
                "high": "float",
                "low": "float",
                "close": "float",
                "volume": "int",
                "value": "int",
            })
            .assign(date=lambda d: pd.to_datetime(d.date, format="%Y%m%d"))
            .sort_values("date")  # 과거→현재 순서
            .reset_index(drop=True)
        )
        return df


kis = KISClient()  # 싱글턴
