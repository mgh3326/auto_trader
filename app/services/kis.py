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

    async def fetch_my_stocks(self, is_mock: bool = False) -> list[dict]:
        """
        보유 주식 목록 조회 (Upbit의 fetch_my_coins와 유사한 기능)

        Args:
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            보유 주식 목록 (list of dict)
            각 항목은 다음 정보를 포함:
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
            - evlu_erng_rt: 평가수익률
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

        tr_id = BALANCE_TR_MOCK if is_mock else BALANCE_TR

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": tr_id,
        }

        params = {
            "CANO": cano,  # 계좌번호 앞 8자리
            "ACNT_PRDT_CD": acnt_prdt_cd,  # 계좌상품코드 뒤 2자리
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

        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{BASE}{BALANCE_URL}",
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
                return await self.fetch_my_stocks(is_mock)
            raise RuntimeError(f'{js.get("msg_cd")} {js.get("msg1")}')

        # output1: 종목별 보유 내역
        stocks = js.get("output1", [])

        # 보유수량이 0인 종목은 제외 (실제 보유 중인 종목만 반환)
        stocks = [stock for stock in stocks if int(stock.get("hldg_qty", 0)) > 0]

        return stocks

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


kis = KISClient()  # 싱글턴
