import asyncio
import datetime
import json
import logging
from typing import Any, Coroutine

import httpx
import pandas as pd
from pandas import DataFrame

from app.core.config import settings
from app.services.redis_token_manager import redis_token_manager

BASE = "https://openapi.koreainvestment.com:9443"
VOL_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"
PRICE_TR = "FHKST01010100"
PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DAILY_ITEMCHARTPRICE_URL = (
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
VOL_TR = "FHPST01710000"  # 실전 전용
DAILY_ITEMCHARTPRICE_TR = "FHKST03010100"  # (일봉·주식·실전/모의 공통)

# 분봉 데이터 관련 URL 및 TR ID 추가
MINUTE_CHART_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MINUTE_CHART_TR = "FHKST03010200"  # 분봉 조회 TR ID

# 주식잔고 조회 관련 URL 및 TR ID 추가
BALANCE_URL = "/uapi/domestic-stock/v1/trading/inquire-balance"
BALANCE_TR = "TTTC8434R"  # 실전투자 주식잔고조회
BALANCE_TR_MOCK = "VTTC8434R"  # 모의투자 주식잔고조회

# 해외주식 잔고조회 관련 URL 및 TR ID 추가
OVERSEAS_BALANCE_URL = "/uapi/overseas-stock/v1/trading/inquire-balance"
OVERSEAS_BALANCE_TR = "TTTS3012R"  # 실전투자 해외주식 잔고조회
OVERSEAS_BALANCE_TR_MOCK = "VTTS3012R"  # 모의투자 해외주식 잔고조회

# 해외주식 일봉/분봉 조회 관련 URL 및 TR ID
OVERSEAS_DAILY_CHART_URL = "/uapi/overseas-price/v1/quotations/dailyprice"
OVERSEAS_DAILY_CHART_TR = "HHDFS76240000"  # 해외주식 기간별시세 (v1_해외주식-010)
OVERSEAS_PERIOD_CHART_URL = "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
OVERSEAS_PERIOD_CHART_TR = "FHKST03030100"  # 해외주식 종목/지수/환율 기간별시세 (v1_해외주식-012)
OVERSEAS_MINUTE_CHART_URL = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
OVERSEAS_MINUTE_CHART_TR = "FHKST03010200"  # 해외주식 분봉조회 (v1_해외주식-030)
OVERSEAS_PRICE_URL = "/uapi/overseas-price/v1/quotations/price"
OVERSEAS_PRICE_TR = "HHDFS00000300"  # 해외주식 현재가 조회


class KISClient:
    def __init__(self):
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        # Redis 기반 토큰 관리 사용
        self._token_manager = redis_token_manager

    async def _fetch_token(self) -> str:
        """KIS API에서 새 토큰 발급"""
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{BASE}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "appkey": settings.kis_app_key,
                    "appsecret": settings.kis_app_secret,
                },
                timeout=5,
            )
        response = r.json()
        access_token = response["access_token"]
        expires_in = response.get("expires_in", 3600)  # 기본 1시간
        
        logging.info("KIS 새 토큰 발급 완료")
        return access_token, expires_in

    async def _ensure_token(self):
        """Redis에서 토큰을 가져오거나 새로 발급"""
        # Redis에서 토큰 확인
        token = await self._token_manager.get_token()
        if token:
            settings.kis_access_token = token
            logging.info(f"Redis에서 토큰 사용: {token[:10]}...")
            return
        
        # 토큰이 없거나 만료된 경우 새로 발급 (분산 락 사용)
        async def token_fetcher():
            access_token, expires_in = await self._fetch_token()
            logging.info(f"새 토큰 발급: {access_token[:10]}... (만료: {expires_in}초)")
            return access_token, expires_in
        
        settings.kis_access_token = await self._token_manager.refresh_token_with_lock(token_fetcher)
        logging.info(f"토큰 설정 완료: {settings.kis_access_token[:10]}...")

    async def volume_rank(self):
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": VOL_TR,
        }
        async with httpx.AsyncClient() as cli:
            r = await cli.get(
                f"{BASE}{VOL_URL}",
                headers=hdr,
                params={
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
                },
                timeout=5,
            )
        js = r.json()
        if js["rt_cd"] == "0":
            return js["output"]
        if js["msg_cd"] == "EGW00123":  # 토큰 만료
            # Redis에서 토큰 삭제 후 새로 발급
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.volume_rank()
        elif js["msg_cd"] == "EGW00121":  # 유효하지 않은 토큰
            # Redis에서 토큰 삭제 후 새로 발급
            await self._token_manager.clear_token()
            await self._ensure_token()
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
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._token_manager.clear_token()
                await self._ensure_token()
                # 재시도 1회
                return await self.inquire_price(code, market)
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
        return pd.DataFrame([row]).set_index("code")  # index = 종목코드

    async def fetch_fundamental_info(self, code: str, market: str = "J") -> dict:
        """
        종목의 기본 정보를 가져와 딕셔너리로 반환합니다.
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/J(통합)
        :return: 기본 정보 딕셔너리
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
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._token_manager.clear_token()
                await self._ensure_token()
                # 재시도 1회
                return await self.fetch_fundamental_info(code, market)
            raise RuntimeError(f'{js["msg_cd"]} {js["msg1"]}')
        out = js["output"]  # 단일 dict

        # 기본 정보 구성
        fundamental_data = {
            "종목코드": out.get("stck_shrn_iscd"),
            "종목명": out.get("hts_kor_isnm"),
            "현재가": out.get("stck_prpr"),
            "전일대비": out.get("prdy_vrss"),
            "등락률": out.get("prdy_ctrt"),
            "거래량": out.get("acml_vol"),
            "거래대금": out.get("acml_tr_pbmn"),
            "시가총액": out.get("hts_avls"),
            "상장주수": out.get("lstn_stcn"),
            "외국인비율": out.get("frgn_hlg"),
            "52주최고": out.get("w52_hgpr"),
            "52주최저": out.get("w52_lwpr"),
        }

        # None이 아닌 값만 반환
        return {k: v for k, v in fundamental_data.items() if v is not None}

    async def inquire_daily_itemchartprice(
            self,
            code: str,
            market: str = "J",
            n: int = 200,  # 최종 확보하고 싶은 캔들 수
            adj: bool = True,
            period: str = "D",  # D/W/M (일/주/월봉)
            end_date: datetime.date | None = None,  # None이면 오늘까지
            per_call_days: int = 150,  # 한 번 호출 시 조회 날짜 폭
    ) -> pd.DataFrame:
        """
        ✅ KIS 일봉/주봉/월봉을 여러 번 호출해 최근 n개 OHLCV만 반환
           (이평 같은 지표 계산은 외부에서!)
        컬럼: date • open • high • low • close • volume • value
        """
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": DAILY_ITEMCHARTPRICE_TR,
        }

        end = end_date or datetime.date.today()
        rows: list[dict] = []

        while len(rows) < n:
            start = end - datetime.timedelta(days=per_call_days)
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
                "FID_PERIOD_DIV_CODE": period,  # 'D'|'W'|'M'
                "FID_ORG_ADJ_PRC": "0" if adj else "1",  # 0=수정, 1=원본
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            }

            async with httpx.AsyncClient(timeout=5) as cli:
                r = await cli.get(
                    f"{BASE}{DAILY_ITEMCHARTPRICE_URL}", headers=hdr, params=params
                )
            js = r.json()

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:  # 토큰 만료 또는 유효하지 않은 토큰
                    # Redis에서 토큰 삭제 후 새로 발급
                    await self._token_manager.clear_token()
                    await self._ensure_token()
                    continue
                raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                break  # 더 과거 없음

            rows.extend(chunk)

            # 다음 루프에서 더 과거로
            oldest_str = min(c["stck_bsop_date"] for c in chunk)  # 'YYYYMMDD'
            oldest = datetime.datetime.strptime(oldest_str, "%Y%m%d").date()
            end = oldest - datetime.timedelta(days=1)

        # ---- DataFrame 변환 (지표 계산 없음) ----
        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_clpr": "close",
                    "acml_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "int",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )
        return df

    async def inquire_minute_chart(
            self,
            code: str,
            market: str = "J",
            time_unit: int = 1,  # 1: 1분, 3: 3분, 5: 5분, 10: 10분, 15: 15분, 30: 30분, 45: 45분, 60: 60분
            n: int = 200,  # 최종 확보하고 싶은 캔들 수
            end_date: datetime.date | None = None,  # None이면 오늘까지
    ) -> pd.DataFrame:
        """
        KIS 분봉 데이터 조회
        컬럼: datetime, date, time, open, high, low, close, volume, value
        
        Parameters
        ----------
        code : str
            6자리 종목코드 (예: "005930")
        market : str, default "J"
            시장 구분 (J: 통합, K: 코스피, Q: 코스닥)
        time_unit : int, default 1
            분봉 단위 (1, 3, 5, 10, 15, 30, 45, 60)
        n : int, default 200
            가져올 캔들 수 (최대 200)
        end_date : datetime.date, optional
            종료 날짜 (None이면 오늘까지)
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": MINUTE_CHART_TR,
        }

        # KIS 분봉 API는 time_unit 파라미터를 제대로 인식하지 못하는 문제가 있음
        # 현재로서는 모든 시간대에서 동일한 데이터가 반환됨
        # 향후 API 문서 업데이트나 기술지원을 통해 해결 필요
        
        # 현재 시간을 시분초로 설정 (장 시간 내에만 작동)
        current_time = datetime.datetime.now().strftime("%H%M%S")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,  # 현재 시분초
            "FID_INPUT_DATE_1": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_TIME_1": "01",  # 1분봉으로 고정 (API가 time_unit을 지원하지 않음)
            "FID_INPUT_TIME_2": "01",  # 1분봉으로 고정
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": ""
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{BASE}{MINUTE_CHART_URL}", headers=hdr, params=params
            )

        js = r.json()
        
        # 디버깅을 위한 로깅 추가
        logging.info(f"KIS 분봉 API 응답: {js}")

        # rt_cd가 비어있어도 output2에 데이터가 있을 수 있음
        rows = js.get("output2") or js.get("output") or []
        
        if not rows:
            # 데이터가 없는 경우 빈 DataFrame 반환
            logging.warning(f"KIS 분봉 API에서 데이터를 찾을 수 없음: {js}")
            return pd.DataFrame(columns=["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"])

        # 데이터가 있으면 성공으로 처리 (rt_cd가 비어있어도)
        logging.info(f"KIS 분봉 API에서 {len(rows)}개 데이터 수집 성공")

        # DataFrame 변환
        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_prpr": "close",
                    "cntg_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "str",
                    "time": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(
                # 날짜와 시간을 결합하여 datetime 생성
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"], 
                    format="%Y%m%d%H%M%S"
                ),
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[:, ["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"]]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )

        return df

    def _aggregate_minute_candles(self, df_1min: pd.DataFrame, time_unit: int) -> pd.DataFrame:
        """
        1분봉 데이터를 지정된 시간 단위로 집계
        
        Args:
            df_1min: 1분봉 DataFrame
            time_unit: 집계할 시간 단위 (분)
            
        Returns:
            집계된 DataFrame (완전한 시간대만)
        """
        if df_1min.empty:
            return df_1min
        
        # 시간을 time_unit 단위로 그룹화
        df_1min = df_1min.copy()
        df_1min['time_group'] = df_1min['datetime'].dt.floor(f'{time_unit}min')
        
        # 그룹별로 OHLCV 집계
        aggregated = df_1min.groupby('time_group').agg({
            'open': 'first',      # 첫 번째 시가
            'high': 'max',        # 최고가
            'low': 'min',         # 최저가
            'close': 'last',      # 마지막 종가
            'volume': 'sum',      # 거래량 합계
            'value': 'sum'        # 거래대금 합계
        }).reset_index()
        
        # 완전한 시간대만 필터링 (time_unit만큼의 분 데이터가 있는 그룹만)
        complete_periods = []
        for _, row in aggregated.iterrows():
            group_start = row['time_group']
            group_end = group_start + pd.Timedelta(minutes=time_unit)
            
            # 해당 그룹에 속하는 1분봉 개수 확인
            period_data = df_1min[
                (df_1min['datetime'] >= group_start) & 
                (df_1min['datetime'] < group_end)
            ]
            
            # 완전한 시간대인지 확인 (time_unit만큼의 분 데이터가 있어야 함)
            if len(period_data) >= time_unit:
                complete_periods.append(row)
        
        if not complete_periods:
            # 완전한 시간대가 없으면 빈 DataFrame 반환
            return pd.DataFrame(
                columns=["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"]
            )
        
        # 완전한 시간대만으로 DataFrame 재구성
        df_complete = pd.DataFrame(complete_periods)
        
        # 컬럼명 변경 및 시간 정보 추가
        df_complete = df_complete.rename(columns={'time_group': 'datetime'})
        df_complete['date'] = df_complete['datetime'].dt.date
        df_complete['time'] = df_complete['datetime'].dt.time
        
        # 원본 컬럼 순서로 재정렬
        return df_complete[['datetime', 'date', 'time', 'open', 'high', 'low', 'close', 'volume', 'value']]

    async def fetch_my_stocks(
        self,
        is_mock: bool = False,
        is_overseas: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD"
    ) -> list[dict]:
        """
        보유 주식 목록 조회 (Upbit의 fetch_my_coins와 유사한 기능)

        Args:
            is_mock: True면 모의투자, False면 실전투자
            is_overseas: True면 해외주식, False면 국내주식
            exchange_code: 해외주식 거래소 코드 (is_overseas=True일 때만 사용)
                - NASD: 나스닥
                - NYSE: 뉴욕
                - AMEX: 아멕스
                - SEHK: 홍콩
                - SHAA: 중국상해
                - SZAA: 중국심천
                - TKSE: 일본
                - HASE: 베트남하노이
                - VNSE: 베트남호치민
            currency_code: 해외주식 결제통화코드 (is_overseas=True일 때만 사용)
                - USD: 미국 달러
                - HKD: 홍콩 달러
                - CNY: 위안화
                - JPY: 엔화
                - VND: 베트남 동

        Returns:
            보유 주식 목록 (list of dict)

            국내주식 각 항목:
            - pdno: 종목코드
            - prdt_name: 종목명
            - hldg_qty: 보유수량
            - ord_psbl_qty: 주문가능수량
            - pchs_avg_pric: 매입평균가격
            - pchs_amt: 매입금액
            - prpr: 현재가
            - evlu_amt: 평가금액
            - evlu_pfls_amt: 평가손익금액
            - evlu_pfls_rt: 평가손익율

            해외주식 각 항목:
            - ovrs_pdno: 해외종목코드
            - ovrs_item_name: 종목명
            - frcr_pchs_amt1: 외화매입금액
            - ovrs_cblc_qty: 해외잔고수량
            - ord_psbl_qty: 주문가능수량
            - frcr_buy_amt_smtl1: 외화매수금액합계
            - ovrs_stck_evlu_amt: 해외주식평가금액
            - frcr_evlu_pfls_amt: 외화평가손익금액
            - evlu_pfls_rt: 평가손익율
        """
        await self._ensure_token()

        # 계좌번호 확인
        if not settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다. 계좌번호를 .env 파일에 추가해주세요.")

        # 계좌번호를 CANO(앞 8자리)와 ACNT_PRDT_CD(뒤 2자리)로 분리
        # 형식: "12345678-01" 또는 "1234567801"
        account_no = settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(f"계좌번호 형식이 올바르지 않습니다: {settings.kis_account_no}")

        cano = account_no[:8]  # 계좌번호 앞 8자리
        acnt_prdt_cd = account_no[8:10]  # 계좌상품코드 뒤 2자리

        if is_overseas:
            # 해외주식 잔고조회
            tr_id = OVERSEAS_BALANCE_TR_MOCK if is_mock else OVERSEAS_BALANCE_TR
            url = OVERSEAS_BALANCE_URL

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "OVRS_EXCG_CD": exchange_code,  # 해외거래소코드
                "TR_CRCY_CD": currency_code,  # 거래통화코드
                "CTX_AREA_FK200": "",  # 연속조회검색조건200
                "CTX_AREA_NK200": "",  # 연속조회키200
            }
        else:
            # 국내주식 잔고조회
            tr_id = BALANCE_TR_MOCK if is_mock else BALANCE_TR
            url = BALANCE_URL

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",  # 시간외단일가여부
                "OFL_YN": "",  # 오프라인여부
                "INQR_DVSN": "02",  # 조회구분(01:대출일별, 02:종목별)
                "UNPR_DVSN": "01",  # 단가구분(01:기본, 02:손익단가)
                "FUND_STTL_ICLD_YN": "N",  # 펀드결제분포함여부
                "FNCG_AMT_AUTO_RDPT_YN": "N",  # 융자금액자동상환여부
                "PRCS_DVSN": "01",  # 처리구분(00:전일매매포함, 01:전일매매미포함)
                "CTX_AREA_FK100": "",  # 연속조회검색조건100
                "CTX_AREA_NK100": "",  # 연속조회키100
            }

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": tr_id,
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{BASE}{url}",
                headers=hdr,
                params=params,
            )

        js = r.json()

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._token_manager.clear_token()
                await self._ensure_token()
                # 재시도 1회
                return await self.fetch_my_stocks(is_mock, is_overseas, exchange_code, currency_code)
            raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

        # output1: 종목별 보유 내역
        stocks = js.get("output1", [])

        if is_overseas:
            # 해외주식: 보유수량이 0인 종목 제외
            stocks = [stock for stock in stocks if int(stock.get("ovrs_cblc_qty", 0)) > 0]
        else:
            # 국내주식: 보유수량이 0인 종목 제외
            stocks = [stock for stock in stocks if int(stock.get("hldg_qty", 0)) > 0]

        return stocks

    async def fetch_my_overseas_stocks(
        self,
        is_mock: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD"
    ) -> list[dict]:
        """
        해외 보유 주식 목록 조회 편의 메서드

        Args:
            is_mock: True면 모의투자, False면 실전투자
            exchange_code: 거래소 코드 (NASD, NYSE, AMEX, SEHK, SHAA, SZAA, TKSE, HASE, VNSE)
            currency_code: 결제통화코드 (USD, HKD, CNY, JPY, VND)

        Returns:
            해외 보유 주식 목록
        """
        return await self.fetch_my_stocks(
            is_mock=is_mock,
            is_overseas=True,
            exchange_code=exchange_code,
            currency_code=currency_code
        )

    async def fetch_my_us_stocks(self, is_mock: bool = False, exchange: str = "NASD") -> list[dict]:
        """
        미국 보유 주식 목록 조회 편의 메서드

        Args:
            is_mock: True면 모의투자, False면 실전투자
            exchange: 거래소 (NASD: 나스닥, NYSE: 뉴욕, AMEX: 아멕스)

        Returns:
            미국 보유 주식 목록
        """
        return await self.fetch_my_overseas_stocks(
            is_mock=is_mock,
            exchange_code=exchange,
            currency_code="USD"
        )

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "J",
        end_date: datetime.date | None = None,
    ) -> dict:
        """
        분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환
        
        Args:
            code: 종목코드
            market: 시장 구분 (J: KRX)
            end_date: 종료 날짜 (None이면 오늘)
            
        Returns:
            분봉 캔들 데이터 딕셔너리
        """
        minute_candles = {}
        
        try:
            logging.info(f"분봉 데이터 수집 시작: {code}")
            
            # 단일 요청으로 200개 1분봉 수집
            df_1min = await self.inquire_minute_chart(
                code, market, time_unit=1, n=200, end_date=end_date
            )
            
            if not df_1min.empty:
                logging.info(f"1분봉 {len(df_1min)}개 수집 완료")
                
                minute_candles["1min"] = df_1min
                
                # 1분봉 데이터를 5분봉으로 가공
                df_5min = self._aggregate_minute_candles(df_1min, 5)
                minute_candles["5min"] = df_5min
                
                # 1분봉 데이터를 60분봉으로 가공
                df_60min = self._aggregate_minute_candles(df_1min, 60)
                minute_candles["60min"] = df_60min
                
                logging.info(f"집계 완료 - 1분봉: {len(df_1min)}개, 5분봉: {len(df_5min)}개, 60분봉: {len(df_60min)}개")
                
            else:
                # 데이터가 없는 경우 빈 DataFrame으로 설정
                empty_df = pd.DataFrame(
                    columns=["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"])
                minute_candles = {
                    "60min": empty_df,
                    "5min": empty_df,
                    "1min": empty_df
                }
                logging.warning("수집된 데이터가 없습니다")
            
        except Exception as e:
            logging.warning(f"분봉 데이터 수집 실패 ({code}): {e}")
            # 실패한 경우 빈 DataFrame으로 설정
            empty_df = pd.DataFrame(
                columns=["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"])
            minute_candles = {
                "60min": empty_df,
                "5min": empty_df,
                "1min": empty_df
            }
        
        return minute_candles

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",  # D/W/M
    ) -> pd.DataFrame:
        """
        해외주식 일봉/주봉/월봉 조회 (국내주식처럼 충분한 데이터 확보)

        Args:
            symbol: 종목 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            n: 조회할 캔들 수 (최소 200개 권장, 이동평균선 계산용)
            period: D(일봉)/W(주봉)/M(월봉)

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        await self._ensure_token()

        # KIS API는 거래소 코드를 3자리로 사용: NASD -> NAS, NYSE -> NYS, AMEX -> AMS
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": OVERSEAS_DAILY_CHART_TR,
        }

        rows: list[dict] = []
        max_iterations = 5  # 최대 5번 반복 (충분한 데이터 확보)
        iteration = 0

        # 국내주식처럼 충분한 데이터를 확보할 때까지 반복 조회
        while len(rows) < n and iteration < max_iterations:
            # BYMD 파라미터: 빈 값이면 최근, 날짜를 지정하면 해당 날짜부터 과거로
            if rows:
                # 이전에 가져온 데이터의 가장 오래된 날짜 찾기
                oldest_date = min(r.get("xymd", "99999999") for r in rows)
                # 하루 전으로 설정
                try:
                    oldest_dt = datetime.datetime.strptime(oldest_date, "%Y%m%d")
                    bymd = (oldest_dt - datetime.timedelta(days=1)).strftime("%Y%m%d")
                except:
                    bymd = ""
            else:
                bymd = ""  # 첫 요청은 최신 데이터부터

            params = {
                "AUTH": "",
                "EXCD": excd,  # 거래소코드 (3자리)
                "SYMB": symbol,  # 심볼
                "GUBN": "0",  # 0:일, 1:주, 2:월
                "BYMD": bymd,  # 조회기준일자
                "MODP": "1",  # 0:수정주가 미반영, 1:수정주가 반영
            }

            logging.info(f"해외주식 일봉 조회 요청 (반복 {iteration + 1}/{max_iterations}) - symbol: {symbol}, exchange: {excd}, bymd: {bymd}")

            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(
                    f"{BASE}{OVERSEAS_DAILY_CHART_URL}",
                    headers=hdr,
                    params=params
                )

            # 디버깅: 응답 내용 확인
            if r.status_code != 200:
                logging.error(f"HTTP 오류: {r.status_code}, 내용: {r.text[:200]}")
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

            try:
                js = r.json()
            except Exception as e:
                logging.error(f"JSON 파싱 실패. 응답 내용: {r.text[:200]}")
                raise

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._token_manager.clear_token()
                    await self._ensure_token()
                    continue
                raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

            chunk = js.get("output2", [])

            if not chunk:
                logging.info(f"더 이상 과거 데이터가 없음. 현재까지 수집: {len(rows)}개")
                break  # 더 이상 과거 데이터 없음

            rows.extend(chunk)
            iteration += 1

            logging.info(f"누적 데이터: {len(rows)}개 / 목표: {n}개")

        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df = (
            pd.DataFrame(rows)
            .rename(columns={
                "xymd": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "clos": "close",
                "tvol": "volume",
            })
            .astype({
                "date": "str",
                "open": "float",
                "high": "float",
                "low": "float",
                "close": "float",
                "volume": "int",
            }, errors="ignore")
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)  # 요청한 개수만 반환
            .reset_index(drop=True)
        )

        logging.info(f"해외주식 일봉 조회 완료: {len(df)}개 데이터 반환")
        return df

    async def inquire_overseas_price(
        self,
        symbol: str,
        exchange_code: str = "NASD"
    ) -> pd.DataFrame:
        """
        해외주식 현재가 조회

        Args:
            symbol: 종목 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)

        Returns:
            DataFrame with current price info
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": OVERSEAS_PRICE_TR,
        }

        # KIS API는 거래소 코드를 3자리로 사용
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        params = {
            "AUTH": "",
            "EXCD": excd,  # 거래소코드 (3자리)
            "SYMB": symbol,
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{BASE}{OVERSEAS_PRICE_URL}",
                headers=hdr,
                params=params
            )

        js = r.json()

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._token_manager.clear_token()
                await self._ensure_token()
                return await self.inquire_overseas_price(symbol, exchange_code)
            raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

        out = js.get("output", {})

        if not out:
            return pd.DataFrame(columns=["code", "date", "time", "open", "high", "low", "close", "volume"])

        # 현재 시간 정보
        now = datetime.datetime.now()

        # 빈 문자열을 0으로 변환하는 헬퍼 함수
        def safe_float(val, default=0.0):
            if val == '' or val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def safe_int(val, default=0):
            if val == '' or val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        row = {
            "code": symbol,
            "date": pd.Timestamp(now.date()),  # Timestamp로 변환하여 일봉 데이터와 타입 일치
            "time": now.time(),
            "open": safe_float(out.get("open")),
            "high": safe_float(out.get("high")),
            "low": safe_float(out.get("low")),
            "close": safe_float(out.get("last")),  # 현재가
            "volume": safe_int(out.get("tvol")),
            "value": 0,  # 해외주식은 거래대금 정보 없음
        }

        return pd.DataFrame([row]).set_index("code")

    async def fetch_overseas_fundamental_info(
        self,
        symbol: str,
        exchange_code: str = "NASD"
    ) -> dict:
        """
        해외주식 기본 정보 조회

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드

        Returns:
            기본 정보 딕셔너리
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": OVERSEAS_PRICE_TR,
        }

        # KIS API는 거래소 코드를 3자리로 사용
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        params = {
            "AUTH": "",
            "EXCD": excd,  # 거래소코드 (3자리)
            "SYMB": symbol,
        }

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{BASE}{OVERSEAS_PRICE_URL}",
                headers=hdr,
                params=params
            )

        js = r.json()

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._token_manager.clear_token()
                await self._ensure_token()
                return await self.fetch_overseas_fundamental_info(symbol, exchange_code)
            raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

        out = js.get("output", {})

        fundamental_data = {
            "종목코드": symbol,
            "종목명": out.get("name", ""),
            "현재가": out.get("last"),
            "전일대비": out.get("diff"),
            "등락률": out.get("rate"),
            "거래량": out.get("tvol"),
            "52주최고": out.get("h52p"),
            "52주최저": out.get("l52p"),
        }

        return {k: v for k, v in fundamental_data.items() if v is not None}

    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
    ) -> pd.DataFrame:
        """
        해외주식 분봉 조회

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드
            n: 조회할 캔들 수

        Returns:
            DataFrame with columns: datetime, date, time, open, high, low, close, volume
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": OVERSEAS_MINUTE_CHART_TR,
        }

        # KIS API는 거래소 코드를 3자리로 사용
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        params = {
            "AUTH": "",
            "EXCD": excd,  # 거래소코드 (3자리)
            "SYMB": symbol,
            "NMIN": "1",  # 1분봉
            "PINC": "1",  # 1:주가, 2:대비
            "NEXT": "",  # 연속조회
            "NREC": str(min(n, 120)),  # 최대 120개
            "FILL": "",  # 빈값: 장중만, 1:장전/장후 포함
            "KEYB": "",  # 연속조회키
        }

        # 디버깅: 요청 파라미터 로깅
        logging.info(f"해외주식 분봉 조회 요청 - symbol: {symbol}, exchange: {excd}, params: {params}")

        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(
                f"{BASE}{OVERSEAS_MINUTE_CHART_URL}",
                headers=hdr,
                params=params
            )

        js = r.json()

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._token_manager.clear_token()
                await self._ensure_token()
                return await self.inquire_overseas_minute_chart(symbol, exchange_code, n)
            logging.warning(f"해외주식 분봉 조회 실패: {js.get('msg1')}")
            return pd.DataFrame(columns=["datetime", "date", "time", "open", "high", "low", "close", "volume"])

        rows = js.get("output2", [])

        if not rows:
            logging.warning("해외주식 분봉 데이터 없음")
            return pd.DataFrame(columns=["datetime", "date", "time", "open", "high", "low", "close", "volume"])

        df = (
            pd.DataFrame(rows)
            .rename(columns={
                "kymd": "date",
                "khms": "time",
                "open": "open",
                "high": "high",
                "low": "low",
                "last": "close",
                "evol": "volume",
            })
            .astype({
                "date": "str",
                "time": "str",
                "open": "float",
                "high": "float",
                "low": "float",
                "close": "float",
                "volume": "int",
            }, errors="ignore")
            .assign(
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"],
                    format="%Y%m%d%H%M%S"
                ),
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[:, ["datetime", "date", "time", "open", "high", "low", "close", "volume"]]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(n)
            .reset_index(drop=True)
        )

        return df

    async def fetch_overseas_minute_candles(
        self,
        symbol: str,
        exchange_code: str = "NASD",
    ) -> dict:
        """
        해외주식 분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드

        Returns:
            분봉 캔들 데이터 딕셔너리
        """
        minute_candles = {}

        try:
            logging.info(f"해외주식 분봉 데이터 수집 시작: {symbol}")

            # 1분봉 수집 (최대 120개)
            df_1min = await self.inquire_overseas_minute_chart(symbol, exchange_code, n=120)

            if not df_1min.empty:
                logging.info(f"1분봉 {len(df_1min)}개 수집 완료")

                minute_candles["1min"] = df_1min.tail(10)  # 최근 10개만

                # 5분봉으로 집계
                df_5min = self._aggregate_minute_candles(df_1min, 5)
                minute_candles["5min"] = df_5min.tail(12)  # 최근 12개만

                # 60분봉으로 집계
                df_60min = self._aggregate_minute_candles(df_1min, 60)
                minute_candles["60min"] = df_60min.tail(12)  # 최근 12개만

                logging.info(f"집계 완료 - 5분봉: {len(df_5min)}개, 60분봉: {len(df_60min)}개")
            else:
                # 빈 DataFrame
                empty_df = pd.DataFrame(
                    columns=["datetime", "date", "time", "open", "high", "low", "close", "volume"])
                minute_candles = {
                    "60min": empty_df,
                    "5min": empty_df,
                    "1min": empty_df
                }
                logging.warning("수집된 데이터가 없습니다")

        except Exception as e:
            logging.warning(f"해외주식 분봉 데이터 수집 실패 ({symbol}): {e}")
            empty_df = pd.DataFrame(
                columns=["datetime", "date", "time", "open", "high", "low", "close", "volume"])
            minute_candles = {
                "60min": empty_df,
                "5min": empty_df,
                "1min": empty_df
            }

        return minute_candles


kis = KISClient()  # 싱글턴
