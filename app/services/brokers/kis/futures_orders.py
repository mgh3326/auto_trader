"""KIS 국내 선물 모의(주간) 주문·취소 전송 (hk 5946).

🔴 이 모듈은 **모의 전용**이다: 호스트가 정확히 VTS 호스트가 아니거나 클라이언트가 모의가
아니거나 계좌가 8자리가 아니면 요청을 만들기 전에 ``FuturesPreDispatchError`` 로 거부한다.
TR 은 모의 주간 ``VTTO1101U``/``VTTO1103U`` 뿐이다(실전·야간 TR 상수 없음). 이 모듈을
부르는 곳은 ``app/services/kis_futures_mock/`` 하나이고(정적 테스트), T1 읽기 view
(`kis_futures_read.view`)의 허용 목록에는 이 메서드들이 없다.

전송은 한 번이다: ``retry_request_errors=False``·``max_retries_override=0`` 로 어떤
재전송도 하지 않는다. HTTP 요청이 만들어지기 전에 멈췄음이 증명되는 경우(env 게이트·
호스트·계좌·토큰·분산 게이트·서킷 브레이커·pre-send 신선도)만
``FuturesPreDispatchError`` 이고, 그 밖의 모든 예외는 호출자가 ``uncertain`` 으로 다룬다.

필드 근거(공식 샘플): https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_futureoption/domestic_futureoption_functions.py
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from app.services.brokers.kis.circuit_breaker import KISCircuitOpen
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.brokers.kis.vts_distributed_gate import DistributedGateUnavailable
from app.services.kis_futures_mock import constants as C  # noqa: N812


class FuturesPreDispatchError(RuntimeError):
    """요청이 만들어지기 전에 멈췄다 — 브로커에 닿지 않았음이 증명된 거부."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"futures mock pre-dispatch refusal: {stage}")


#: 서킷·분산 게이트·신선도 거부는 HTTP=0 인 사전 거부다 (base.py 계약).
_PRE_DISPATCH_TYPES = (
    KISCircuitOpen,
    DistributedGateUnavailable,
    PreSendFreshnessError,
)


def _enabled() -> None:
    if os.environ.get(C.ENABLED_ENV) != "true":
        raise FuturesPreDispatchError("env_gate_disabled")


def _account(account: str) -> dict[str, str]:
    if len(account) != 8 or not account.isascii() or not account.isdigit():
        raise FuturesPreDispatchError("account_format")
    return {"CANO": account, "ACNT_PRDT_CD": C.ACCOUNT_PRODUCT_CODE}


class FuturesOrders:
    def __init__(self, parent: Any) -> None:
        self._parent = parent

    async def _post(
        self, path: str, tr: str, body: dict[str, str], api_name: str
    ) -> dict[str, Any]:
        _enabled()
        if (
            not getattr(self._parent, "_is_mock_client", False)
            or self._parent._settings.kis_base_url.rstrip("/") != C.MOCK_HOST
        ):
            raise FuturesPreDispatchError("not_mock_host")
        try:
            await self._parent._ensure_token()
        except Exception as exc:  # noqa: BLE001 - 토큰 발급은 주문 POST 전 단계다
            raise FuturesPreDispatchError("token") from exc
        _enabled()  # 토큰 대기 뒤 게이트 재확인
        headers = self._parent._hdr_base | {
            "authorization": f"Bearer {self._parent._settings.kis_access_token}",
            "tr_id": tr,
        }
        try:
            data = await self._parent._request_with_rate_limit(
                "POST",
                self._parent._kis_url(path),
                headers=headers,
                json_body=body,
                timeout=10,
                api_name=api_name,
                tr_id=tr,
                # 주문은 절대 다시 POST 하지 않는다 (ROB-645).
                retry_request_errors=False,
                max_retries_override=0,
            )
        except _PRE_DISPATCH_TYPES as exc:
            raise FuturesPreDispatchError(type(exc).__name__) from exc
        if not isinstance(data, dict):
            raise RuntimeError("futures mock order response is not an object")
        return data

    async def place(
        self,
        *,
        account: str,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal,
    ) -> dict[str, Any]:
        """지정가 1건. 호출자가 닫힌 상품·상한·가드를 이미 판정했어야 한다."""
        if side not in C.SLL_BUY_DVSN or quantity != 1 or price <= 0:
            raise FuturesPreDispatchError("order_shape")
        body = _account(account) | {
            "ORD_PRCS_DVSN_CD": C.ORD_PRCS_DVSN_SEND,
            "SLL_BUY_DVSN_CD": C.SLL_BUY_DVSN[side],
            "SHTN_PDNO": symbol,
            "ORD_QTY": str(quantity),
            "UNIT_PRICE": format(price, "f"),
            "NMPR_TYPE_CD": C.NMPR_TYPE_LIMIT,
            "KRX_NMPR_CNDT_CD": C.KRX_NMPR_CNDT_NONE,
            "ORD_DVSN_CD": C.ORD_DVSN_LIMIT,
            "CTAC_TLNO": "",
            "FUOP_ITEM_DVSN_CD": "",
        }
        return await self._post(C.ORDER_PATH, C.TR_PLACE, body, "futures_place_order")

    async def cancel(self, *, account: str, original_order_no: str) -> dict[str, Any]:
        """원주문 전량 취소. 원주문 번호는 호출자가 자기 원장에서 가져온다."""
        if not original_order_no or not original_order_no.isascii():
            raise FuturesPreDispatchError("order_shape")
        body = _account(account) | {
            "ORD_PRCS_DVSN_CD": C.ORD_PRCS_DVSN_SEND,
            "RVSE_CNCL_DVSN_CD": C.RVSE_CNCL_CANCEL,
            "ORGN_ODNO": original_order_no,
            "ORD_QTY": "0",
            "UNIT_PRICE": "0",
            "NMPR_TYPE_CD": C.NMPR_TYPE_LIMIT,
            "KRX_NMPR_CNDT_CD": C.KRX_NMPR_CNDT_NONE,
            "RMN_QTY_YN": "Y",
            "ORD_DVSN_CD": C.ORD_DVSN_LIMIT,
            "FUOP_ITEM_DVSN_CD": "",
        }
        return await self._post(
            C.CANCEL_PATH, C.TR_CANCEL, body, "futures_cancel_order"
        )
