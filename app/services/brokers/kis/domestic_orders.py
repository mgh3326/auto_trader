# pyright: reportAttributeAccessIssue=false, reportImplicitStringConcatenation=false, reportMissingTypeArgument=false
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, cast

from . import constants
from .base import _log_kis_api_failure

if TYPE_CHECKING:
    from .protocols import KISClientProtocol


class DomesticOrderClient:
    """Client for KIS domestic (Korean) stock order operations.

    Handles buy/sell orders, cancellations, modifications, and order history.
    """

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self) -> Any:
        return self._parent._settings

    @staticmethod
    def _extract_korea_order_orgno(order: dict[str, Any]) -> str | None:
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
        target_order_number = order_number.strip()
        target_stock_code = stock_code.strip() if stock_code is not None else None

        parent_inquire_orders = getattr(self._parent, "inquire_korea_orders", None)
        if callable(parent_inquire_orders):
            result = cast(Any, parent_inquire_orders)(is_mock=is_mock)
            if inspect.isawaitable(result):
                open_orders = await result
            else:
                open_orders = result
        else:
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

    async def inquire_korea_orders(
        self,
        is_mock: bool = False,
    ) -> list[dict]:
        """
        국내주식 정정취소가능주문 조회 (모든 페이지 조회)

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

        # 정정취소가능주문 조회는 실전/모의 구분 없이 동일한 TR_ID 사용
        tr_id = constants.DOMESTIC_ORDER_INQUIRY_TR

        all_orders = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""  # 연속조회 구분: 최초 조회 시 공백, 연속 조회 시 "N"
        page = 1
        max_pages = 10  # 최대 페이지 수 제한

        logging.info("국내주식 미체결 주문 조회 시작")

        while page <= max_pages:
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
                "tr_cont": tr_cont,  # 연속조회 여부 (첫 조회: "", 이후: "N")
            }

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "CTX_AREA_FK100": ctx_area_fk100,  # 연속조회검색조건100
                "CTX_AREA_NK100": ctx_area_nk100,  # 연속조회키100
                "INQR_DVSN_1": "0",  # 조회구분1 (0:조회순서, 1:주문순, 2:종목순)
                "INQR_DVSN_2": "0",  # 조회구분2 (0:전체, 1:매도, 2:매수)
            }

            logging.info(
                f"페이지 {page} 조회 (tr_cont: '{tr_cont}', NK100: '{ctx_area_nk100[:20] if ctx_area_nk100 else 'empty'}...')"
            )

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_ORDER_INQUIRY_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_korea_orders",
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
            new_ctx_area_fk100 = js.get("ctx_area_fk100", "")
            new_ctx_area_nk100 = js.get("ctx_area_nk100", "")

            logging.info(
                f"  반환된 FK100: '{new_ctx_area_fk100[:20] if new_ctx_area_fk100 else 'empty'}...'"
            )
            logging.info(
                f"  반환된 NK100: '{new_ctx_area_nk100[:20] if new_ctx_area_nk100 else 'empty'}...'"
            )

            # 연속조회 키가 없거나 이전과 동일하면 마지막 페이지
            if not new_ctx_area_nk100 or new_ctx_area_nk100 == ctx_area_nk100:
                logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
                break

            # 다음 페이지를 위한 설정
            ctx_area_fk100 = new_ctx_area_fk100
            ctx_area_nk100 = new_ctx_area_nk100
            tr_cont = "N"  # 두 번째 페이지부터는 "N" 설정

            page += 1
            await asyncio.sleep(0.1)  # API 호출 제한 방지

        logging.info(f"미체결 주문 조회 완료: 총 {len(all_orders)}건")

        return all_orders

    async def order_korea_stock(
        self,
        stock_code: str,
        order_type: str,  # "buy" 또는 "sell"
        quantity: int,
        price: int = 0,  # 0이면 시장가
        is_mock: bool = False,
    ) -> dict:
        """
        국내주식 주문 (매수/매도)

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

        # TR_ID 선택
        if order_type.lower() == "buy":
            tr_id = (
                constants.DOMESTIC_ORDER_BUY_TR_MOCK
                if is_mock
                else constants.DOMESTIC_ORDER_BUY_TR
            )
            order_type_korean = "매수"
        elif order_type.lower() == "sell":
            tr_id = (
                constants.DOMESTIC_ORDER_SELL_TR_MOCK
                if is_mock
                else constants.DOMESTIC_ORDER_SELL_TR
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

        # 주문 구분: 00(지정가), 01(시장가)
        ord_dvsn = "01" if price == 0 else "00"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": stock_code,  # 종목코드
            "ORD_DVSN": ord_dvsn,  # 주문구분 (00:지정가, 01:시장가)
            "ORD_QTY": str(quantity),  # 주문수량
            "ORD_UNPR": str(price),  # 주문단가 (시장가일 경우 0)
            "EXCG_ID_DVSN_CD": "SOR",
        }

        logging.info(
            f"국내주식 {order_type_korean} 주문 - stock_code: {stock_code}, "
            f"수량: {quantity}주, 가격: {price if price > 0 else '시장가'}, "
            f"tr_id: {tr_id}, routing: {body['EXCG_ID_DVSN_CD']}"
        )

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.DOMESTIC_ORDER_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="order_korea_stock",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            msg_cd = js.get("msg_cd", "")
            msg1 = js.get("msg1", "")
            _log_kis_api_failure(
                api_name="order_korea_stock",
                endpoint=constants.DOMESTIC_ORDER_URL,
                tr_id=tr_id,
                request_keys=list(body.keys()),
                msg_cd=msg_cd,
                msg1=msg1,
            )
            if msg_cd in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.order_korea_stock(
                    stock_code, order_type, quantity, price, is_mock
                )

            error_msg = f"{msg_cd} {msg1}"
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO") or output.get("ORD_NO"),  # 주문번호
            "ord_tmd": output.get("ORD_TMD"),  # 주문시각
            "msg": js.get("msg1"),  # 응답메시지
        }

        logging.info(
            f"국내주식 주문 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )

        return result

    async def sell_korea_stock(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,  # 0이면 시장가
        is_mock: bool = False,
    ) -> dict:
        """
        국내주식 매도 주문 편의 메서드

        Args:
            stock_code: 종목코드
            quantity: 매도 수량
            price: 매도 가격 (0이면 시장가)
            is_mock: 모의투자 여부

        Returns:
            주문 결과
        """
        return await self.order_korea_stock(
            stock_code, "sell", quantity, price, is_mock
        )

    async def cancel_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,  # "buy" 또는 "sell"
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """
        국내주식 주문 취소

        Args:
            order_number: 취소할 원주문번호
            stock_code: 종목코드
            quantity: 주문 수량
            price: 주문 단가
            order_type: "buy"(매수) 또는 "sell"(매도)
            is_mock: True면 모의투자, False면 실전투자
            krx_fwdg_ord_orgno: 한국거래소전송주문조직번호

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

        tr_id = (
            constants.DOMESTIC_ORDER_CANCEL_TR_MOCK
            if is_mock
            else constants.DOMESTIC_ORDER_CANCEL_TR
        )

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }

        # 주문구분: 00(지정가)
        ord_dvsn = "00"

        # 매도매수구분: 매도(01), 매수(02) - 취소할 주문과 동일하게 (검증용)
        if order_type.lower() not in ("sell", "buy"):
            raise ValueError(
                f"order_type은 'buy' 또는 'sell'이어야 합니다: {order_type}"
            )

        resolved_kis_orgno = None
        if krx_fwdg_ord_orgno is not None:
            explicit_orgno = str(krx_fwdg_ord_orgno).strip()
            if explicit_orgno:
                resolved_kis_orgno = explicit_orgno

        if resolved_kis_orgno is None:
            resolved_kis_orgno = await self._resolve_korea_order_orgno(
                order_number=order_number,
                stock_code=stock_code,
                is_mock=is_mock,
            )

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": resolved_kis_orgno,  # 한국거래소전송주문직번호
            "ORGN_ODNO": order_number,  # 원주문번호
            "ORD_DVSN": ord_dvsn,  # 주문구분
            "RVSE_CNCL_DVSN_CD": "02",  # 정정취소구분코드 (01:정정, 02:취소)
            "ORD_QTY": str(quantity),  # 주문수량
            "ORD_UNPR": str(price),  # 주문단가
            "QTY_ALL_ORD_YN": "N",  # 잔량전부주문여부 (Y:전부취소, N:일부취소)
            "EXCG_ID_DVSN_CD": "SOR",
        }

        logging.info(
            f"국내주식 주문 취소 - stock_code: {stock_code}, 주문번호: {order_number}, "
            f"tr_id: {tr_id}, routing: {body['EXCG_ID_DVSN_CD']}, "
            f"KRX_FWDG_ORD_ORGNO: {body['KRX_FWDG_ORD_ORGNO']}"
        )

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.DOMESTIC_ORDER_CANCEL_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="cancel_korea_order",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.cancel_korea_order(
                    order_number,
                    stock_code,
                    quantity,
                    price,
                    order_type,
                    is_mock,
                    resolved_kis_orgno,
                )

            error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
            logging.error(f"주문 취소 실패: {error_msg}")
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO") or output.get("ORD_NO"),  # 주문번호
            "ord_tmd": output.get("ORD_TMD"),  # 주문시각
            "msg": js.get("msg1"),  # 응답메시지
        }

        logging.info(
            f"주문 취소 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )

        return result

    async def inquire_daily_order_domestic(
        self,
        start_date: str,
        end_date: str,
        stock_code: str = "",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict]:
        """
        국내주식 일별 체결조회 (주문 히스토리)

        Args:
            start_date: 조회 시작일자 (YYYYMMDD)
            end_date: 조회 종료일자 (YYYYMMDD)
            stock_code: 종목코드 (6자리), 공백이면 전체 조회
            side: 매도매수구분 (00:전체, 01:매도, 02:매수)
            order_number: 주문번호 (특정 주문만 조회 시)
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            체결 주문 목록 (list of dict)
            각 항목:
            - ord_no: 주문번호
            - orgn_ord_no: 원주문번호
            - sll_buy_dvsn_cd: 매도매수구분코드 (01:매도, 02:매수)
            - sll_buy_dvsn_cd_name: 매도매수구분명
            - rvse_cncl_dvsn_cd: 정정취소구분코드
            - rvse_cncl_dvsn_name: 정정취소구분명
            - pdno: 상품번호(종목코드)
            - prdt_name: 상품명
            - ord_qty: 주문수량
            - ord_unpr: 주문단가
            - ccld_qty: 체결수량
            - ccld_unpr: 체결단가
            - ccld_amt: 체결금액
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
            constants.DOMESTIC_DAILY_ORDER_TR_MOCK
            if is_mock
            else constants.DOMESTIC_DAILY_ORDER_TR
        )

        all_orders = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        page = 1
        max_pages = 10
        token_retry_count = 0
        max_token_retries = 3
        transient_retry_count = 0

        logging.info(f"국내주식 체결조회 시작 - {start_date} ~ {end_date}")

        while page <= max_pages:
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
                "tr_cont": tr_cont,
            }

            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": side,
                "PDNO": stock_code,
                "CCLD_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "ORD_GNO_BRNO": "",
                "ODNO": order_number,
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
            }

            logging.info(
                f"페이지 {page} 조회 (tr_cont: '{tr_cont}', NK100: '{ctx_area_nk100[:20] if ctx_area_nk100 else 'empty'}...')"
            )

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_DAILY_ORDER_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_daily_order_domestic",
                tr_id=tr_id,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    token_retry_count += 1
                    if token_retry_count >= max_token_retries:
                        error_msg = f"{js.get('msg_cd')} {js.get('msg1')} (token retry limit exceeded)"
                        logging.error(f"국내주식 체결조회 실패: {error_msg}")
                        raise RuntimeError(error_msg)
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue

                if js.get("msg_cd") in constants.RETRYABLE_MSG_CODES:
                    transient_retry_count += 1
                    if transient_retry_count < constants.RETRYABLE_MAX_ATTEMPTS:
                        logging.warning(
                            "국내주식 체결조회 transient 에러 (시도 %d/%d): %s %s",
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
                logging.error(f"국내주식 체결조회 실패: {error_msg}")
                raise RuntimeError(error_msg)

            orders = js.get("output", [])

            if not orders:
                logging.info(f"페이지 {page}에서 더 이상 주문이 없음")
                break

            all_orders.extend(orders)
            logging.info(
                f"페이지 {page}: {len(orders)}건 조회 (누적: {len(all_orders)}건)"
            )

            new_ctx_area_fk100 = js.get("ctx_area_fk100", "")
            new_ctx_area_nk100 = js.get("ctx_area_nk100", "")

            if not new_ctx_area_nk100 or new_ctx_area_nk100 == ctx_area_nk100:
                logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
                break

            ctx_area_fk100 = new_ctx_area_fk100
            ctx_area_nk100 = new_ctx_area_nk100
            tr_cont = "N"

            page += 1
            await asyncio.sleep(0.1)

        logging.info(f"국내주식 체결조회 완료: 총 {len(all_orders)}건")
        return all_orders

    async def modify_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        new_price: int,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """
        국내주식 주문 정정 (가격/수량 변경)

        Args:
            order_number: 정정할 원주문번호
            stock_code: 종목코드 (6자리)
            quantity: 새 주문수량
            new_price: 새 주문단가
            is_mock: True면 모의투자, False면 실전투자
            krx_fwdg_ord_orgno: 한국거래소전송주문조직번호

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
            constants.DOMESTIC_ORDER_CANCEL_TR_MOCK
            if is_mock
            else constants.DOMESTIC_ORDER_CANCEL_TR
        )

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }

        resolved_kis_orgno = None
        if krx_fwdg_ord_orgno is not None:
            explicit_orgno = str(krx_fwdg_ord_orgno).strip()
            if explicit_orgno:
                resolved_kis_orgno = explicit_orgno

        if resolved_kis_orgno is None:
            resolved_kis_orgno = await self._resolve_korea_order_orgno(
                order_number=order_number,
                stock_code=stock_code,
                is_mock=is_mock,
            )

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": resolved_kis_orgno,
            "ORGN_ODNO": order_number,
            "ORD_DVSN": "00",  # 주문구분 (00:지정가)
            "RVSE_CNCL_DVSN_CD": "01",  # 정정취소구분코드 (01:정정, 02:취소)
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(new_price),
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "SOR",
        }

        logging.info(
            f"국내주식 주문 정정 - stock_code: {stock_code}, 주문번호: {order_number}, "
            f"tr_id: {tr_id}, routing: {body['EXCG_ID_DVSN_CD']}, "
            f"KRX_FWDG_ORD_ORGNO: {body['KRX_FWDG_ORD_ORGNO']}"
        )

        js = await self._parent._request_with_rate_limit(
            "POST",
            f"{constants.BASE}{constants.DOMESTIC_ORDER_CANCEL_URL}",
            headers=hdr,
            json_body=body,
            timeout=10,
            api_name="modify_korea_order",
            tr_id=tr_id,
        )

        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.modify_korea_order(
                    order_number,
                    stock_code,
                    quantity,
                    new_price,
                    is_mock,
                    resolved_kis_orgno,
                )

            error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
            logging.error(f"주문 정정 실패: {error_msg}")
            raise RuntimeError(error_msg)

        output = js.get("output", {})

        result = {
            "odno": output.get("ODNO") or output.get("ORD_NO"),
            "ord_tmd": output.get("ORD_TMD"),
            "msg": js.get("msg1"),
        }

        logging.info(
            f"주문 정정 완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}"
        )
        return result
