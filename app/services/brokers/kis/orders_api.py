"""KIS API order operations module.

This module provides the OrdersAPI class that handles all order placement,
cancellation, and modification operations for the KIS (Korea Investment & Securities) API:
- Domestic stock orders (국내주식 주문)
- Overseas stock orders (해외주식 주문)
- Order inquiry and cancellation (주문 조회 및 취소)
- Order modification (주문 정정)

The class receives KISTransport via constructor injection for HTTP communication.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.symbol import to_kis_symbol
from app.services.brokers.kis.constants import (
    BASE_URL,
    DOMESTIC_ORDER_BUY_TR,
    DOMESTIC_ORDER_BUY_TR_MOCK,
    DOMESTIC_ORDER_CANCEL_TR,
    DOMESTIC_ORDER_CANCEL_TR_MOCK,
    DOMESTIC_ORDER_CANCEL_URL,
    DOMESTIC_ORDER_INQUIRY_TR,
    DOMESTIC_ORDER_INQUIRY_URL,
    DOMESTIC_ORDER_SELL_TR,
    DOMESTIC_ORDER_SELL_TR_MOCK,
    DOMESTIC_ORDER_URL,
    OVERSEAS_ORDER_BUY_TR,
    OVERSEAS_ORDER_BUY_TR_MOCK,
    OVERSEAS_ORDER_CANCEL_TR,
    OVERSEAS_ORDER_CANCEL_TR_MOCK,
    OVERSEAS_ORDER_CANCEL_URL,
    OVERSEAS_ORDER_INQUIRY_TR,
    OVERSEAS_ORDER_INQUIRY_URL,
    OVERSEAS_ORDER_SELL_TR,
    OVERSEAS_ORDER_SELL_TR_MOCK,
    OVERSEAS_ORDER_URL,
)
from app.services.brokers.kis.transport import _log_kis_api_failure

if TYPE_CHECKING:
    from app.services.brokers.kis.transport import KISTransport


class OrdersAPI:
    """KIS API order operations.

    This class handles all order placement, cancellation, and modification operations:
    - Overseas stock orders (buy/sell)
    - Domestic stock orders (buy/sell)
    - Order inquiry (pending orders)
    - Order cancellation and modification

    The class receives KISTransport via constructor injection for HTTP
    communication, rate limiting, and token management.

    Example:
        transport = KISTransport()
        orders = OrdersAPI(transport)
        result = await orders.buy_overseas_stock("AAPL", "NASD", 10, is_mock=True)
    """

    def __init__(self, transport: "KISTransport") -> None:
        """Initialize the OrdersAPI with a transport layer.

        Args:
            transport: KISTransport instance for HTTP communication
        """
        self._transport = transport

        # Base headers for KIS API requests
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",  # Placeholder, overridden per request
            "custtype": "P",
        }

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    @staticmethod
    def _extract_korea_order_orgno(order: dict[str, Any]) -> str | None:
        """Extract the KRX forwarding order organization number from an order dict.

        This helper extracts the orgno (organization number) from various possible
        field names returned by the KIS API.

        Args:
            order: Order dictionary from inquire_korea_orders

        Returns:
            Organization number string, or None if not found
        """
        for key in (
            "KRX_FWDG_ORD_ORGNO",
            "krx_fwdg_ord_orgno",
            "ORD_GNO_BRNO",
            "ord_gno_brno",
        ):
            value = order.get(key)
            if value is None:
                continue
            orgno = str(value).strip()
            if orgno:
                return orgno
        return None

    async def _resolve_korea_order_orgno(
        self,
        order_number: str,
        stock_code: str | None,
        is_mock: bool,
    ) -> str:
        """Resolve the KRX forwarding order organization number for a given order.

        This method looks up the order in the pending orders list and extracts
        the organization number needed for cancellation/modification requests.

        Args:
            order_number: The order number to look up
            stock_code: Optional stock code for additional matching
            is_mock: True for mock trading, False for real trading

        Returns:
            Organization number string

        Raises:
            ValueError: If the order is not found or orgno cannot be extracted
        """
        target_order_number = order_number.strip()
        target_stock_code = stock_code.strip() if stock_code is not None else None

        open_orders = await self.inquire_korea_orders(is_mock=is_mock)

        for order in open_orders:
            listed_order_number = (
                order.get("odno")
                or order.get("ODNO")
                or order.get("ord_no")
                or order.get("ORD_NO")
            )
            if str(listed_order_number).strip() != target_order_number:
                continue

            if target_stock_code:
                listed_stock_code = order.get("pdno") or order.get("PDNO")
                if str(listed_stock_code).strip() != target_stock_code:
                    continue

            orgno = self._extract_korea_order_orgno(order)
            if orgno:
                return orgno

        raise ValueError(f"KRX_FWDG_ORD_ORGNO not found for order {order_number}")

    # ========================================================================
    # OVERSEAS ORDER METHODS
    # ========================================================================

    async def order_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        order_type: str,  # "buy" or "sell"
        quantity: int,
        price: float = 0.0,  # 0이면 시장가
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 (매수/매도)

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            order_type: "buy"(매수) 또는 "sell"(매도)
            quantity: 주문수량
            price: 주문가격 (0이면 시장가)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            주문 결과 딕셔너리
            - odno: 주문번호
            - ord_tmd: 주문시각
        """
        await self._transport.ensure_token()

        if not settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        if order_type.lower() == "buy":
            tr_id = OVERSEAS_ORDER_BUY_TR_MOCK if is_mock else OVERSEAS_ORDER_BUY_TR
            order_type_korean = "매수"
        elif order_type.lower() == "sell":
            tr_id = OVERSEAS_ORDER_SELL_TR_MOCK if is_mock else OVERSEAS_ORDER_SELL_TR
            order_type_korean = "매도"
        else:
            raise ValueError(
                f"order_type은 'buy' 또는 'sell'이어야 합니다: {order_type}"
            )

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": tr_id,
        }

        ord_dvsn = "01" if price == 0 else "00"  # 00: 지정가, 01: 시장가

        # SLL_TYPE: 매도 주문 시 "00", 매수 주문 시 "" (공란)
        sll_type = "00" if order_type.lower() == "sell" else ""

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange_code,
            "PDNO": to_kis_symbol(symbol),
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(price) if price > 0 else "0",
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "SLL_TYPE": sll_type,
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        logging.info(
            f"해외주식 {order_type_korean} 주문 - symbol: {symbol}, "
            f"거래소: {exchange_code}, 수량: {quantity}, 가격: {price if price > 0 else '시장가'}"
        )
        logging.debug("해외주식 주문 payload 필드: %s", sorted(body.keys()))
        logging.debug(
            "해외주식 주문 payload 핵심값 - symbol=%s, exchange=%s, order_type=%s, "
            "ord_dvsn=%s, ord_qty=%s, ovrs_ord_unpr=%s",
            symbol,
            exchange_code,
            order_type.lower(),
            body.get("ORD_DVSN"),
            body.get("ORD_QTY"),
            body.get("OVRS_ORD_UNPR"),
        )

        js = await self._transport.request(
            "POST",
            f"{BASE_URL}{OVERSEAS_ORDER_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="order_overseas_stock",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._transport._token_manager.clear_token()
                await self._transport.ensure_token()
                return await self.order_overseas_stock(
                    symbol, exchange_code, order_type, quantity, price, is_mock
                )

            error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
            logging.error(f"해외주식 주문 실패: {error_msg}")
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO"),  # 주문번호
            "ord_tmd": output.get("ORD_TMD"),  # 주문시각
            "msg": js.get("msg1"),  # 응답메시지
        }

        logging.info(
            f"{order_type_korean} 주문 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )

        return result

    async def buy_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매수 주문 편의 메서드

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드
            quantity: 매수 수량
            price: 매수 가격 (0이면 시장가)
            is_mock: 모의투자 여부

        Returns:
            주문 결과
        """
        return await self.order_overseas_stock(
            symbol, exchange_code, "buy", quantity, price, is_mock
        )

    async def sell_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매도 주문 편의 메서드

        Args:
            symbol: 종목 심볼
            exchange_code: 거래소 코드
            quantity: 매도 수량
            price: 매도 가격 (0이면 시장가)
            is_mock: 모의투자 여부

        Returns:
            주문 결과
        """
        return await self.order_overseas_stock(
            symbol, exchange_code, "sell", quantity, price, is_mock
        )

    async def inquire_overseas_orders(
        self,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict]:
        """해외주식 미체결 주문 조회 (모든 페이지 조회)

        Args:
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            미체결 주문 목록 (list of dict)
            각 항목:
            - odno: 주문번호
            - orgn_odno: 원주문번호
            - sll_buy_dvsn_cd: 매도매수구분코드 (01:매도, 02:매수)
        """
        # TODO: Implement in subtask-3-2
        raise NotImplementedError("Will be implemented in subtask-3-2")

    async def cancel_overseas_order(
        self,
        order_number: str,
        exchange_code: str,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 취소

        Args:
            order_number: 주문번호
            exchange_code: 거래소 코드
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            취소 결과
        """
        # TODO: Implement in subtask-3-3
        raise NotImplementedError("Will be implemented in subtask-3-3")

    async def inquire_daily_order_overseas(
        self,
        order_date: str,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict]:
        """해외주식 일별 체결조회 (주문 히스토리)

        Args:
            order_date: 조회 일자 (YYYYMMDD)
            exchange_code: 거래소 코드
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            체결 내역 목록
        """
        # TODO: Implement in subtask-3-3
        raise NotImplementedError("Will be implemented in subtask-3-3")

    async def modify_overseas_order(
        self,
        order_number: str,
        exchange_code: str,
        quantity: int,
        price: float,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 정정

        Args:
            order_number: 주문번호
            exchange_code: 거래소 코드
            quantity: 정정 수량
            price: 정정 가격
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            정정 결과
        """
        # TODO: Implement in subtask-3-5
        raise NotImplementedError("Will be implemented in subtask-3-5")

    # ========================================================================
    # DOMESTIC (KOREA) ORDER METHODS
    # ========================================================================

    async def order_korea_stock(
        self,
        stock_code: str,
        order_type: str,  # "buy" 또는 "sell"
        quantity: int,
        price: int = 0,  # 0이면 시장가
        is_mock: bool = False,
    ) -> dict:
        """국내주식 주문 (매수/매도)

        Args:
            stock_code: 종목코드 (예: "005930")
            order_type: "buy"(매수) 또는 "sell"(매도)
            quantity: 주문수량
            price: 주문가격 (0이면 시장가)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            주문 결과 딕셔너리
            - odno: 주문번호
            - ord_tmd: 주문시각
        """
        # TODO: Implement in subtask-3-4
        raise NotImplementedError("Will be implemented in subtask-3-4")

    async def sell_korea_stock(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 매도 주문 편의 메서드

        Args:
            stock_code: 종목코드
            quantity: 매도 수량
            price: 매도 가격 (0이면 시장가)
            is_mock: 모의투자 여부

        Returns:
            주문 결과
        """
        # TODO: Implement in subtask-3-4
        raise NotImplementedError("Will be implemented in subtask-3-4")

    async def inquire_korea_orders(
        self,
        is_mock: bool = False,
    ) -> list[dict]:
        """국내주식 정정취소가능주문 조회 (모든 페이지 조회)

        Args:
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            미체결 주문 목록 (list of dict)
            각 항목:
            - ord_no: 주문번호
            - orgn_ord_no: 원주문번호
            - sll_buy_dvsn_cd: 매도매수구분코드 (01:매도, 02:매수)
            - sll_buy_dvsn_cd_name: 매도매수구분명
            - rvse_cncl_dvsn_cd: 정정취소구분코드
            - pdno: 상품번호(종목코드)
            - prdt_name: 상품명
            - ord_qty: 주문수량
            - ord_unpr: 주문단가
            - ord_tmd: 주문시각
        """
        # TODO: Implement in subtask-3-4
        raise NotImplementedError("Will be implemented in subtask-3-4")

    async def cancel_korea_order(
        self,
        order_number: str,
        stock_code: str | None = None,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 주문 취소

        Args:
            order_number: 주문번호
            stock_code: 종목코드 (선택사항, orgno 조회용)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            취소 결과
        """
        # TODO: Implement in subtask-3-5
        raise NotImplementedError("Will be implemented in subtask-3-5")

    async def modify_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        price: int,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 주문 정정

        Args:
            order_number: 주문번호
            stock_code: 종목코드
            quantity: 정정 수량
            price: 정정 가격
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            정정 결과
        """
        # TODO: Implement in subtask-3-5
        raise NotImplementedError("Will be implemented in subtask-3-5")

    async def inquire_daily_order_domestic(
        self,
        order_date: str,
        is_mock: bool = False,
    ) -> list[dict]:
        """국내주식 일별 체결조회 (주문 히스토리)

        Args:
            order_date: 조회 일자 (YYYYMMDD)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            체결 내역 목록
        """
        # TODO: Implement in subtask-3-5
        raise NotImplementedError("Will be implemented in subtask-3-5")
