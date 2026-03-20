# pyright: reportAttributeAccessIssue=false, reportImplicitStringConcatenation=false, reportMissingTypeArgument=false
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.core.symbol import to_kis_symbol

from . import constants

if TYPE_CHECKING:
    from .protocols import KISClientProtocol


_VALID_KIS_EXCHANGE_CODES = {
    "NASD",
    "NYSE",
    "AMEX",
    "SEHK",
    "SHAA",
    "SZAA",
    "TKSE",
    "HASE",
    "VNSE",
}

_EXCHANGE_ALIAS_MAP = {
    "NASDAQ": "NASD",
    "NASDAQ_GS": "NASD",
    "NYQ": "NYSE",
    "NYSEMKT": "AMEX",
}


def _normalize_kis_exchange_code(code: str) -> str:
    """Normalize exchange code to KIS format.

    Maps common aliases (NASDAQ -> NASD, NYQ -> NYSE, etc.) to canonical
    KIS exchange codes and validates against supported exchanges.

    Args:
        code: Exchange code to normalize (e.g., "NASDAQ", "NYSE", "NASD")

    Returns:
        Normalized KIS exchange code (e.g., "NASD", "NYSE", "AMEX")

    Raises:
        ValueError: If the exchange code is not supported
    """
    upper = str(code or "").strip().upper()
    normalized = _EXCHANGE_ALIAS_MAP.get(upper, upper)
    if normalized not in _VALID_KIS_EXCHANGE_CODES:
        raise ValueError(f"Unsupported KIS exchange_code: {code!r}")
    return normalized


class OverseasOrderClient:
    """Client for KIS overseas stock order operations.

    Handles buy/sell orders, cancellations, modifications, and order history
    for international markets (US, Hong Kong, Japan, etc.).
    """

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self) -> Any:
        return self._parent._settings

    async def order_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        order_type: str,  # "buy" or "sell"
        quantity: int,
        price: float = 0.0,  # 0이면 시장가
        is_mock: bool = False,
    ) -> dict:
        """
        해외주식 주문 (매수/매도)

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
        await self._parent._ensure_token()

        if not self._settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        if order_type.lower() == "buy":
            tr_id = (
                constants.OVERSEAS_ORDER_BUY_TR_MOCK
                if is_mock
                else constants.OVERSEAS_ORDER_BUY_TR
            )
            order_type_korean = "매수"
        elif order_type.lower() == "sell":
            tr_id = (
                constants.OVERSEAS_ORDER_SELL_TR_MOCK
                if is_mock
                else constants.OVERSEAS_ORDER_SELL_TR
            )
            order_type_korean = "매도"
        else:
            raise ValueError(
                f"order_type은 'buy' 또는 'sell'이어야 합니다: {order_type}"
            )

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
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

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.OVERSEAS_ORDER_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="order_overseas_stock",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
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
        """
        해외주식 매수 주문 편의 메서드

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
        """
        해외주식 매도 주문 편의 메서드

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
        """
        해외주식 미체결 주문 조회 (모든 페이지 조회)

        Args:
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            미체결 주문 목록 (list of dict)
            각 항목:
            - odno: 주문번호
            - orgn_odno: 원주문번호
            - sll_buy_dvsn_cd: 매도매수구분코드 (01:매도, 02:매수)
            - sll_buy_dvsn_cd_name: 매도매수구분명
            - rvse_cncl_dvsn: 정정취소구분
            - rvse_cncl_dvsn_name: 정정취소구분명
            - pdno: 상품번호(종목코드)
            - prdt_name: 상품명
            - ft_ord_qty: 주문수량
            - ft_ord_unpr3: 주문단가
            - ft_ccld_qty: 체결수량
            - nccs_qty: 미체결수량
            - ft_ccld_unpr3: 체결단가
            - ft_ccld_amt3: 체결금액
            - prcs_stat_name: 처리상태명
            - rjct_rson: 거부사유
            - ord_dt: 주문일자
            - ord_tmd: 주문시각
        """
        await self._parent._ensure_token()

        # 계좌번호 확인
        if not self._settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        # 미체결 조회는 실전/모의 구분 없이 동일한 TR_ID 사용
        tr_id = constants.OVERSEAS_ORDER_INQUIRY_TR

        all_orders = []
        ctx_area_fk200 = ""
        ctx_area_nk200 = ""
        tr_cont = ""  # 연속조회 구분: 최초 조회 시 공백, 연속 조회 시 "N"
        page = 1
        max_pages = 10  # 최대 페이지 수 제한

        logging.info(f"해외주식 미체결 주문 조회 시작 - exchange: {exchange_code}")

        while page <= max_pages:
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
                "tr_cont": tr_cont,  # 연속조회 여부 (첫 조회: "", 이후: "N")
            }

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "OVRS_EXCG_CD": exchange_code,  # 해외거래소코드 (NASD는 미국 전체 조회)
                "SORT_SQN": "DS",  # 정렬순서 (DS:정순, AS:역순)
                "CTX_AREA_FK200": ctx_area_fk200,  # 연속조회검색조건200
                "CTX_AREA_NK200": ctx_area_nk200,  # 연속조회키200
            }

            logging.info(
                f"페이지 {page} 조회 (tr_cont: '{tr_cont}', NK200: '{ctx_area_nk200[:20] if ctx_area_nk200 else 'empty'}...')"
            )

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_ORDER_INQUIRY_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_orders",
                tr_id=tr_id,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue

                error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                logging.error(f"미체결 주문 조회 실패: {error_msg}")
                raise RuntimeError(error_msg)

            # output: 미체결 주문 목록
            orders = js.get("output", [])

            if not orders:
                logging.info(f"페이지 {page}에서 더 이상 주문이 없음")
                break

            all_orders.extend(orders)
            logging.info(
                f"페이지 {page}: {len(orders)}건 조회 (누적: {len(all_orders)}건)"
            )

            # 다음 페이지 키 확인
            new_ctx_area_fk200 = js.get("ctx_area_fk200", "")
            new_ctx_area_nk200 = js.get("ctx_area_nk200", "")

            logging.info(
                f"  반환된 FK200: '{new_ctx_area_fk200[:20] if new_ctx_area_fk200 else 'empty'}...'"
            )
            logging.info(
                f"  반환된 NK200: '{new_ctx_area_nk200[:20] if new_ctx_area_nk200 else 'empty'}...'"
            )

            # 연속조회 키가 없거나 이전과 동일하면 마지막 페이지
            if not new_ctx_area_nk200 or new_ctx_area_nk200 == ctx_area_nk200:
                logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
                break

            # 다음 페이지를 위한 설정
            ctx_area_fk200 = new_ctx_area_fk200
            ctx_area_nk200 = new_ctx_area_nk200
            tr_cont = "N"  # 두 번째 페이지부터는 "N" 설정

            page += 1
            await asyncio.sleep(0.1)  # API 호출 제한 방지

        logging.info(f"미체결 주문 조회 완료: 총 {len(all_orders)}건")

        return all_orders

    async def cancel_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        is_mock: bool = False,
    ) -> dict:
        """
        해외주식 주문 취소

        Args:
            order_number: 취소할 원주문번호
            symbol: 종목 심볼
            exchange_code: 거래소 코드
            quantity: 주문 수량
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            취소 결과 딕셔너리
            - odno: 주문번호
            - ord_tmd: 주문시각
            - msg: 응답메시지
        """
        await self._parent._ensure_token()

        # 계좌번호 확인
        if not self._settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        # Normalize exchange code to KIS format
        normalized_exchange_code = _normalize_kis_exchange_code(exchange_code)

        tr_id = (
            constants.OVERSEAS_ORDER_CANCEL_TR_MOCK
            if is_mock
            else constants.OVERSEAS_ORDER_CANCEL_TR
        )

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalized_exchange_code,  # 해외거래소코드
            "PDNO": to_kis_symbol(symbol),  # 상품번호(종목코드) (DB형식 . -> KIS형식 /)
            "ORGN_ODNO": order_number,  # 원주문번호
            "RVSE_CNCL_DVSN_CD": "02",  # 정정취소구분코드 (01:정정, 02:취소)
            "ORD_QTY": str(quantity),  # 주문수량
            "OVRS_ORD_UNPR": "0",  # 해외주문단가 (취소 시 0)
            "MGCO_APTM_ODNO": "",  # 운용사지정주문번호
            "ORD_SVR_DVSN_CD": "0",  # 주문서버구분코드
        }

        logging.info(
            f"해외주식 주문 취소 - symbol: {symbol}, "
            f"주문번호: {order_number}, 거래소: {normalized_exchange_code}"
        )

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.OVERSEAS_ORDER_CANCEL_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="cancel_overseas_order",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.cancel_overseas_order(
                    order_number, symbol, exchange_code, quantity, is_mock
                )

            error_msg = (
                f"KIS cancel order failed: {js.get('msg_cd')} {js.get('msg1')} "
                f"(order_id={order_number}, symbol={symbol}, "
                f"exchange={normalized_exchange_code})"
            )
            logging.error(f"주문 취소 실패: {error_msg}")
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO"),  # 주문번호
            "ord_tmd": output.get("ORD_TMD"),  # 주문시각
            "msg": js.get("msg1"),  # 응답메시지
        }

        logging.info(
            f"주문 취소 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )

        return result

    async def inquire_daily_order_overseas(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "%",
        exchange_code: str = "NASD",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict]:
        """
        해외주식 일별 체결조회 (주문 히스토리)

        Args:
            start_date: 조회 시작일자 (YYYYMMDD)
            end_date: 조회 종료일자 (YYYYMMDD)
            symbol: 종목 심볼 (%: 전체 조회 시 필터링)
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            side: 매도매수구분 (00:전체, 01:매도, 02:매수)
            order_number: 주문번호 (해외주식은 미지원으로 무시됨)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            체결 주문 목록 (list of dict)
            각 항목:
            - odno: 주문번호
            - orgn_odno: 원주문번호
            - sll_buy_dvsn_cd: 매도매수구분코드 (01:매도, 02:매수)
            - sll_buy_dvsn_cd_name: 매도매수구분명
            - rvse_cncl_dvsn_cd: 정정취소구분코드
            - rvse_cncl_dvsn_name: 정정취소구분명
            - pdno: 상품번호(종목코드)
            - prdt_name: 상품명
            - ft_ord_qty: 주문수량
            - ft_ord_unpr3: 주문단가
            - ft_ccld_qty: 체결수량
            - ft_ccld_unpr3: 체결단가
            - ft_ccld_amt3: 체결금액
            - prcs_stat_name: 처리상태명
            - ord_dt: 주문일자
            - ord_tmd: 주문시각
        """
        await self._parent._ensure_token()

        if not self._settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        tr_id = (
            constants.OVERSEAS_DAILY_ORDER_TR_MOCK
            if is_mock
            else constants.OVERSEAS_DAILY_ORDER_TR
        )

        all_orders = []
        ctx_area_fk200 = ""
        ctx_area_nk200 = ""
        tr_cont = ""
        page = 1
        max_pages = 10
        transient_retry_count = 0

        logging.info(f"해외주식 체결조회 시작 - {start_date} ~ {end_date}")

        while page <= max_pages:
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
                "tr_cont": tr_cont,
            }

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "PDNO": to_kis_symbol(symbol) if symbol != "%" else "",
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": side,
                "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": exchange_code,
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_FK200": ctx_area_fk200,
                "CTX_AREA_NK200": ctx_area_nk200,
            }

            logging.info(
                f"페이지 {page} 조회 (tr_cont: '{tr_cont}', NK200: '{ctx_area_nk200[:20] if ctx_area_nk200 else 'empty'}...')"
            )

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_DAILY_ORDER_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_daily_order_overseas",
                tr_id=tr_id,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue

                if js.get("msg_cd") in constants.RETRYABLE_MSG_CODES:
                    transient_retry_count += 1
                    if transient_retry_count < constants.RETRYABLE_MAX_ATTEMPTS:
                        logging.warning(
                            "해외주식 체결조회 transient 에러 (시도 %d/%d): %s %s",
                            transient_retry_count,
                            constants.RETRYABLE_MAX_ATTEMPTS,
                            js.get("msg_cd"),
                            js.get("msg1"),
                        )
                        await asyncio.sleep(
                            constants.RETRYABLE_BASE_DELAY * transient_retry_count
                        )
                        continue

                error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                logging.error(f"해외주식 체결조회 실패: {error_msg}")
                raise RuntimeError(error_msg)

            # KIS 해외주식 체결조회 API may return data in 'output' or 'output1' key
            orders = js.get("output1") or js.get("output", [])

            if not orders:
                logging.info(f"페이지 {page}에서 더 이상 주문이 없음")
                break

            all_orders.extend(orders)
            logging.info(
                f"페이지 {page}: {len(orders)}건 조회 (누적: {len(all_orders)}건)"
            )

            new_ctx_area_fk200 = js.get("ctx_area_fk200", "")
            new_ctx_area_nk200 = js.get("ctx_area_nk200", "")

            if not new_ctx_area_nk200 or new_ctx_area_nk200 == ctx_area_nk200:
                logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
                break

            ctx_area_fk200 = new_ctx_area_fk200
            ctx_area_nk200 = new_ctx_area_nk200
            tr_cont = "N"

            page += 1
            await asyncio.sleep(0.1)

        logging.info(f"해외주식 체결조회 완료: 총 {len(all_orders)}건")
        return all_orders

    async def modify_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        new_price: float,
        is_mock: bool = False,
    ) -> dict:
        """
        해외주식 주문 정정 (가격/수량 변경)

        Args:
            order_number: 정정할 원주문번호
            symbol: 종목 심볼
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            quantity: 새 주문수량
            new_price: 새 주문단가
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            정정 결과 딕셔너리
            - odno: 주문번호
            - ord_tmd: 주문시각
            - msg: 응답메시지
        """
        await self._parent._ensure_token()

        if not self._settings.kis_account_no:
            raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")

        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )

        cano = account_no[:8]
        acnt_prdt_cd = account_no[8:10]

        tr_id = (
            constants.OVERSEAS_ORDER_CANCEL_TR_MOCK
            if is_mock
            else constants.OVERSEAS_ORDER_CANCEL_TR
        )

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange_code,
            "PDNO": to_kis_symbol(symbol),
            "ORGN_ODNO": order_number,
            "RVSE_CNCL_DVSN_CD": "01",  # 정정취소구분코드 (01:정정, 02:취소)
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(new_price),
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }

        logging.info(
            f"해외주식 주문 정정 - symbol: {symbol}, 거래소: {exchange_code}, 주문번호: {order_number}"
        )

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.OVERSEAS_ORDER_CANCEL_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="modify_overseas_order",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.modify_overseas_order(
                    order_number, symbol, exchange_code, quantity, new_price, is_mock
                )

            error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
            logging.error(f"주문 정정 실패: {error_msg}")
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO"),
            "ord_tmd": output.get("ORD_TMD"),
            "msg": js.get("msg1"),
        }

        logging.info(
            f"주문 정정 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )
        return result
