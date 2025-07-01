import httpx, time, logging
from core.config import settings

BASE = "https://openapi.koreainvestment.com:9443"
VOL_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"

class KISClient:
    def __init__(self):
        self._hdr = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHKST01010200",
            "custtype": "P",
        }
    async def _token(self):
        if settings.kis_access_token: return
        async with httpx.AsyncClient() as cli:
            r = await cli.post(f"{BASE}/oauth2/token", data={
                "grant_type":"client_credentials",
                "appkey":settings.kis_app_key,
                "appsecret":settings.kis_app_secret})
            settings.kis_access_token = r.json()["access_token"]

    async def volume_rank(self):
        await self._token()
        hdr = self._hdr | {"authorization": f"Bearer {settings.kis_access_token}"}
        async with httpx.AsyncClient() as cli:
            r = await cli.get(f"{BASE}{VOL_URL}", headers=hdr, params={
                "FID_COND_MRKT_DIV_CODE":"J",
                "FID_PERIOD_DIV_CODE":"1",
                "FID_INPUT_ISCD":""})
        js = r.json()
        if js["rt_cd"] == "0":
            return js["output"]
        if js["msg_cd"] == "EGW00123":           # 토큰 만료
            settings.kis_access_token = None
            return await self.volume_rank()
        raise RuntimeError(js["msg1"])
kis = KISClient()   # 싱글턴