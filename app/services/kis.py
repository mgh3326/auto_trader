import json

from app.core.config import settings
from app.services.token_cache import load_token, save_token
import httpx, logging, asyncio

BASE = "https://openapi.koreainvestment.com:9443"
VOL_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"
PRICE_TR = "FHKST01010100"
PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
VOL_TR = "FHPST01710000"  # 실전 전용


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

    async def inquire_price(self, code: str, market: str = "J") -> dict:
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
        return js["output"]  # ← 단일 dict


kis = KISClient()  # 싱글턴
