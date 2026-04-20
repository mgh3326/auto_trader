# pyright: reportAttributeAccessIssue=false, reportImplicitStringConcatenation=false, reportMissingTypeArgument=false
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from . import constants
from .base import _log_kis_api_failure

if TYPE_CHECKING:
    from .protocols import KISClientProtocol


def extract_domestic_cash_summary_from_integrated_margin(
    margin_data: dict[str, Any],
) -> dict[str, Any]:
    def safe_float(val: Any, default: float = 0.0) -> float:
        if val in ("", None):
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def optional_float(val: Any) -> float | None:
        if val in ("", None):
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def first_available_float(*candidates: Any, default: float = 0.0) -> float:
        for candidate in candidates:
            parsed = optional_float(candidate)
            if parsed is not None:
                return parsed
        return default

    def first_usable_positive_float(*candidates: Any, default: float = 0.0) -> float:
        first_numeric: float | None = None
        for candidate in candidates:
            parsed = optional_float(candidate)
            if parsed is None:
                continue
            if first_numeric is None:
                first_numeric = parsed
            if parsed > 0:
                return parsed
        if first_numeric is not None:
            return first_numeric
        return default

    raw = margin_data.get("raw")
    raw_payload = raw if isinstance(raw, dict) else margin_data

    return {
        "balance": first_available_float(
            margin_data.get("stck_cash_objt_amt"),
            raw_payload.get("stck_cash_objt_amt"),
        ),
        "orderable": first_usable_positive_float(
            margin_data.get("stck_cash100_max_ord_psbl_amt"),
            raw_payload.get("stck_cash100_max_ord_psbl_amt"),
            margin_data.get("stck_itgr_cash100_ord_psbl_amt"),
            raw_payload.get("stck_itgr_cash100_ord_psbl_amt"),
            margin_data.get("stck_cash_ord_psbl_amt"),
            raw_payload.get("stck_cash_ord_psbl_amt"),
            margin_data.get("stck_cash_objt_amt"),
            raw_payload.get("stck_cash_objt_amt"),
        ),
        "raw": raw_payload,
    }


class AccountClient:
    """Client for KIS account-related operations.

    Handles balance inquiries, holdings, and margin information.
    """

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self) -> Any:
        return self._parent._settings

    def _resolve_account_parts(self) -> tuple[str, str]:
        """Parse account number into CANO (8-digit) and ACNT_PRDT_CD (2-digit)."""
        if not self._settings.kis_account_no:
            raise ValueError(
                "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다. 계좌번호를 .env 파일에 추가해주세요."
            )
        account_no = self._settings.kis_account_no.replace("-", "")
        if len(account_no) < 10:
            raise ValueError(
                f"계좌번호 형식이 올바르지 않습니다: {self._settings.kis_account_no}"
            )
        return account_no[:8], account_no[8:10]

    def _build_balance_request_config(
        self, *, is_overseas: bool, is_mock: bool
    ) -> dict[str, str]:
        if is_overseas:
            tr_id = (
                constants.OVERSEAS_BALANCE_TR_MOCK
                if is_mock
                else constants.OVERSEAS_BALANCE_TR
            )
            return {
                "tr_id": tr_id,
                "url": constants.OVERSEAS_BALANCE_URL,
                "ctx_key_fk": "CTX_AREA_FK200",
                "ctx_key_nk": "CTX_AREA_NK200",
            }
        tr_id = (
            constants.DOMESTIC_BALANCE_TR_MOCK
            if is_mock
            else constants.DOMESTIC_BALANCE_TR
        )
        return {
            "tr_id": tr_id,
            "url": constants.DOMESTIC_BALANCE_URL,
            "ctx_key_fk": "CTX_AREA_FK100",
            "ctx_key_nk": "CTX_AREA_NK100",
        }

    def _filter_nonzero_holdings(
        self, stocks: list[dict], *, is_overseas: bool
    ) -> list[dict]:
        qty_key = "ovrs_cblc_qty" if is_overseas else "hldg_qty"
        return [s for s in stocks if int(s.get(qty_key, 0)) > 0]

    def _parse_margin_response(self, output: dict[str, Any]) -> dict[str, Any]:
        """Parse raw integrated margin API output into a normalized dict."""

        def safe_float(val: Any, default: float = 0.0) -> float:
            if val in ("", None):
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        dnca_tot_amt = safe_float(
            output.get("dnca_tot_amt") or output.get("stck_cash_objt_amt")
        )
        stck_cash_objt_amt = safe_float(output.get("stck_cash_objt_amt"))
        stck_cash100_max_ord_psbl_amt = safe_float(
            output.get("stck_cash100_max_ord_psbl_amt")
        )
        stck_itgr_cash100_ord_psbl_amt = safe_float(
            output.get("stck_itgr_cash100_ord_psbl_amt")
        )
        stck_cash_ord_psbl_amt = safe_float(
            output.get("stck_cash_ord_psbl_amt")
            or output.get("stck_itgr_cash100_ord_psbl_amt")
            or output.get("ord_psbl_cash")
            or output.get("dnca_tot_amt")
        )
        usd_ord_psbl_amt = safe_float(
            output.get("usd_ord_psbl_amt")
            or output.get("frcr_ord_psbl_amt")
            or output.get("USD_ORD_PSBL_AMT")
            or output.get("FRCR_ORD_PSBL_AMT")
        )
        usd_balance = safe_float(
            output.get("usd_balance")
            or output.get("frcr_dncl_amt_2")
            or output.get("FRCR_DNCL_AMT_2")
        )

        return {
            "dnca_tot_amt": dnca_tot_amt,
            "stck_cash_objt_amt": stck_cash_objt_amt,
            "stck_cash100_max_ord_psbl_amt": stck_cash100_max_ord_psbl_amt,
            "stck_itgr_cash100_ord_psbl_amt": stck_itgr_cash100_ord_psbl_amt,
            "stck_cash_ord_psbl_amt": stck_cash_ord_psbl_amt,
            "usd_ord_psbl_amt": usd_ord_psbl_amt,
            "usd_balance": usd_balance,
            "raw": output,
        }

    async def fetch_my_stocks(
        self,
        is_mock: bool = False,
        is_overseas: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict]:
        """
        보유 주식 목록 조회 (Upbit의 fetch_my_coins와 유사한 기능)
        연속조회(pagination)를 지원하여 모든 종목을 조회합니다.

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
        await self._parent._ensure_token()

        cano, acnt_prdt_cd = self._resolve_account_parts()

        config = self._build_balance_request_config(
            is_overseas=is_overseas, is_mock=is_mock
        )
        tr_id = config["tr_id"]
        url = config["url"]
        ctx_key_fk = config["ctx_key_fk"]
        ctx_key_nk = config["ctx_key_nk"]

        all_stocks = []
        ctx_area_fk = ""
        ctx_area_nk = ""
        tr_cont = ""
        page = 1
        max_pages = 10

        logging.info(
            f"{'해외' if is_overseas else '국내'}주식 잔고 조회 시작 - "
            f"{'거래소: ' + exchange_code if is_overseas else ''}"
        )

        while page <= max_pages:
            token_retry_count = 0
            max_token_retries = 3
            transient_retry_count = 0
            page_fetched = False
            js: dict = {}

            while not page_fetched:
                if is_overseas:
                    params = {
                        "CANO": cano,
                        "ACNT_PRDT_CD": acnt_prdt_cd,
                        "OVRS_EXCG_CD": exchange_code,
                        "TR_CRCY_CD": currency_code,
                        ctx_key_fk: ctx_area_fk,
                        ctx_key_nk: ctx_area_nk,
                    }
                else:
                    params = {
                        "CANO": cano,
                        "ACNT_PRDT_CD": acnt_prdt_cd,
                        "AFHR_FLPR_YN": "N",
                        "OFL_YN": "",
                        "INQR_DVSN": "00",
                        "UNPR_DVSN": "01",
                        "FUND_STTL_ICLD_YN": "N",
                        "FNCG_AMT_AUTO_RDPT_YN": "N",
                        "PRCS_DVSN": "01",
                        ctx_key_fk: ctx_area_fk,
                        ctx_key_nk: ctx_area_nk,
                    }

                hdr = self._parent._hdr_base | {
                    "authorization": f"Bearer {self._settings.kis_access_token}",
                    "tr_id": tr_id,
                    "tr_cont": tr_cont,
                }

                logging.info(
                    f"페이지 {page} 조회 (tr_cont: '{tr_cont}', "
                    f"{ctx_key_nk}: '{ctx_area_nk[:20] if ctx_area_nk else 'empty'}...')"
                )

                js = await self._parent._request_with_rate_limit(
                    "GET",
                    f"{constants.BASE}{url}",
                    headers=hdr,
                    params=params,
                    timeout=5,
                    api_name=(
                        "fetch_my_stocks_overseas"
                        if is_overseas
                        else "fetch_my_stocks_domestic"
                    ),
                    tr_id=tr_id,
                )

                if js.get("rt_cd") != "0":
                    if js.get("msg_cd") in [
                        "EGW00123",
                        "EGW00121",
                    ]:
                        token_retry_count += 1
                        if token_retry_count >= max_token_retries:
                            error_msg = f"{js.get('msg_cd')} {js.get('msg1')} (token retry limit exceeded)"
                            logging.error(
                                f"{'해외' if is_overseas else '국내'}주식 잔고 조회 실패: {error_msg}"
                            )
                            raise RuntimeError(error_msg)
                        await self._parent._token_manager.clear_token()
                        await self._parent._ensure_token()
                        continue

                    if js.get("msg_cd") in constants.RETRYABLE_MSG_CODES:
                        transient_retry_count += 1
                        if transient_retry_count < constants.RETRYABLE_MAX_ATTEMPTS:
                            logging.warning(
                                f"{'해외' if is_overseas else '국내'}주식 잔고조회 transient 에러 (시도 {transient_retry_count}/{constants.RETRYABLE_MAX_ATTEMPTS}): {js.get('msg_cd')} {js.get('msg1')}"
                            )
                            await asyncio.sleep(
                                constants.RETRYABLE_BASE_DELAY * transient_retry_count
                            )
                            continue

                    error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                    logging.error(
                        f"{'해외' if is_overseas else '국내'}주식 잔고 조회 실패: {error_msg}"
                    )
                    raise RuntimeError(error_msg)

                page_fetched = True

            # output1: 종목별 보유 내역
            stocks = js.get("output1", [])

            if not stocks:
                logging.info(f"페이지 {page}에서 더 이상 종목이 없음")
                break

            all_stocks.extend(stocks)
            logging.info(
                f"페이지 {page}: {len(stocks)}건 조회 (누적: {len(all_stocks)}건)"
            )

            new_ctx_area_fk = js.get(ctx_key_fk, "")
            new_ctx_area_nk = js.get(ctx_key_nk, "")

            logging.info(
                f"  반환된 {ctx_key_fk}: '{new_ctx_area_fk[:20] if new_ctx_area_fk else 'empty'}...'"
            )
            logging.info(
                f"  반환된 {ctx_key_nk}: '{new_ctx_area_nk[:20] if new_ctx_area_nk else 'empty'}...'"
            )

            if not new_ctx_area_nk or new_ctx_area_nk == ctx_area_nk:
                logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
                break

            ctx_area_fk = new_ctx_area_fk
            ctx_area_nk = new_ctx_area_nk
            tr_cont = "N"

            page += 1

            await asyncio.sleep(0.1)

        all_stocks = self._filter_nonzero_holdings(all_stocks, is_overseas=is_overseas)

        logging.info(
            f"{'해외' if is_overseas else '국내'}주식 잔고 조회 완료: "
            f"총 {len(all_stocks)}건 (보유수량 > 0)"
        )

        return all_stocks

    async def inquire_domestic_cash_balance(self, is_mock: bool = False) -> dict:
        """
        국내주식 현금 잔고(예수금/주문가능현금) 조회

        Args:
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            국내 현금 잔고 딕셔너리
            - dnca_tot_amt: 국내 예수금
            - stck_cash_ord_psbl_amt: 국내 주문가능현금
            - raw: 원본 output2 첫 항목
        """
        await self._parent._ensure_token()

        cano, acnt_prdt_cd = self._resolve_account_parts()

        tr_id = (
            constants.DOMESTIC_BALANCE_TR_MOCK
            if is_mock
            else constants.DOMESTIC_BALANCE_TR
        )
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "00",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        logging.info("국내 현금 잔고 조회 (inquire-balance)")

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_BALANCE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_domestic_cash_balance",
            tr_id=tr_id,
        )
        if js.get("rt_cd") != "0":
            msg_cd = js.get("msg_cd", "")
            msg1 = js.get("msg1", "")
            _log_kis_api_failure(
                api_name="inquire_domestic_cash_balance",
                endpoint=constants.DOMESTIC_BALANCE_URL,
                tr_id=tr_id,
                request_keys=list(params.keys()),
                msg_cd=msg_cd,
                msg1=msg1,
            )
            if msg_cd in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.inquire_domestic_cash_balance(is_mock)
            raise RuntimeError(f"{msg_cd} {msg1}")

        output2 = js.get("output2", [])
        raw = output2[0] if output2 else {}

        def safe_float(val: Any, default: float = 0.0) -> float:
            if val in ("", None):
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def optional_float(val: Any) -> float | None:
            if val in ("", None):
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        dnca_tot_amt = safe_float(raw.get("dnca_tot_amt"))
        orderable_candidates = (
            raw.get("stck_cash_ord_psbl_amt"),
            raw.get("ord_psbl_cash"),
            raw.get("dnca_tot_amt"),
        )
        stck_cash_ord_psbl_amt: float | None = None
        for candidate in orderable_candidates:
            parsed = optional_float(candidate)
            if parsed is not None:
                stck_cash_ord_psbl_amt = parsed
                break
        if stck_cash_ord_psbl_amt is None:
            stck_cash_ord_psbl_amt = 0.0

        return {
            "dnca_tot_amt": dnca_tot_amt,
            "stck_cash_ord_psbl_amt": stck_cash_ord_psbl_amt,
            "raw": raw,
        }

    async def inquire_overseas_margin(self, is_mock: bool = False) -> list[dict]:
        """
        해외증거금 통화별 조회

        Args:
            is_mock: True면 모의투자, False면 실전투자

        Returns:
            통화별 증거금 정보 리스트
            - crcy_cd: 통화코드
            - frcr_dncl_amt_2: 외화예수금액(보유현금)
            - frcr_ord_psbl_amt: 외화주문가능금액
            - frcr_buy_amt_smtl: 외화매수금액합계
            - tot_evlu_pfls_amt: 총평가손익금액
            - ovrs_tot_pfls: 해외총손익금액
        """
        await self._parent._ensure_token()

        cano, acnt_prdt_cd = self._resolve_account_parts()

        tr_id = (
            constants.OVERSEAS_MARGIN_TR_MOCK
            if is_mock
            else constants.OVERSEAS_MARGIN_TR
        )
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
        }

        logging.info("해외증거금 통화별 조회")

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.OVERSEAS_MARGIN_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_overseas_margin",
            tr_id=tr_id,
        )
        if js.get("rt_cd") != "0":
            if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.inquire_overseas_margin(is_mock)
            raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

        output = js.get("output", [])

        def safe_float(val: Any, default: float = 0.0) -> float:
            if val in ("", None):
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        results: list[dict] = []
        for item in output:
            result = {
                "natn_name": item.get("natn_name"),
                "crcy_cd": item.get("crcy_cd"),
                "frcr_dncl_amt1": safe_float(
                    item.get("frcr_dncl_amt1") or item.get("frcr_dncl_amt_2")
                ),
                "frcr_ord_psbl_amt1": safe_float(
                    item.get("frcr_ord_psbl_amt1") or item.get("frcr_ord_psbl_amt")
                ),
                "frcr_gnrl_ord_psbl_amt": safe_float(
                    item.get("frcr_gnrl_ord_psbl_amt")
                ),
                "itgr_ord_psbl_amt": safe_float(item.get("itgr_ord_psbl_amt")),
                "frcr_buy_amt_smtl": safe_float(item.get("frcr_buy_amt_smtl")),
                "tot_evlu_pfls_amt": safe_float(item.get("tot_evlu_pfls_amt")),
                "ovrs_tot_pfls": safe_float(item.get("ovrs_tot_pfls")),
            }
            results.append(result)

        usd_rows = [
            row for row in results if str(row.get("crcy_cd", "")).upper() == "USD"
        ]
        logging.debug("해외증거금 USD 행 개수: %s", len(usd_rows))
        us_row = next(
            (
                row
                for row in usd_rows
                if str(row.get("natn_name", "")).strip() in {"미국", "US", "USA"}
            ),
            None,
        )
        if us_row:
            logging.debug(
                "해외증거금 미국행 - frcr_dncl_amt1=%s, frcr_gnrl_ord_psbl_amt=%s, "
                "frcr_ord_psbl_amt1=%s, itgr_ord_psbl_amt=%s",
                us_row.get("frcr_dncl_amt1"),
                us_row.get("frcr_gnrl_ord_psbl_amt"),
                us_row.get("frcr_ord_psbl_amt1"),
                us_row.get("itgr_ord_psbl_amt"),
            )

        return results

    async def inquire_integrated_margin(
        self,
        is_mock: bool = False,
        cma_evlu_amt_icld_yn: str = "N",
        wcrc_frcr_dvsn_cd: str = "01",
        fwex_ctrt_frcr_dvsn_cd: str = "01",
    ) -> dict:
        """
        통합증거금 조회 (원화 + 외화 예수금)

        Args:
            is_mock: True면 모의투자, False면 실전투자
            cma_evlu_amt_icld_yn: CMA 평가금액 포함 여부 ("N": 미포함, "Y": 포함)
                                  기본값 "N", OPSQ2001 오류 시 "Y"로 자동 재시도
            wcrc_frcr_dvsn_cd: 원화외화구분코드 (기본값 "01")
            fwex_ctrt_frcr_dvsn_cd: 선도환계약외화구분코드 (기본값 "01")

        Returns:
            통합 증거금 정보
            - dnca_tot_amt: 원화 예수금
            - stck_cash_objt_amt: 국내 주식 현금 대상 금액
            - stck_cash100_max_ord_psbl_amt: 국내 주식 현금 100% 최대 주문가능금액
            - stck_itgr_cash100_ord_psbl_amt: 국내 주식 100% 통합 현금 주문가능금액
            - stck_cash_ord_psbl_amt: 원화 주문가능금액
            - usd_ord_psbl_amt: 달러 주문가능금액
            - usd_balance: 달러 예수금
        """
        await self._parent._ensure_token()

        cano, acnt_prdt_cd = self._resolve_account_parts()

        tr_id = (
            constants.INTEGRATED_MARGIN_TR_MOCK
            if is_mock
            else constants.INTEGRATED_MARGIN_TR
        )
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": tr_id,
        }
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "CMA_EVLU_AMT_ICLD_YN": cma_evlu_amt_icld_yn,
            "WCRC_FRCR_DVSN_CD": wcrc_frcr_dvsn_cd,
            "FWEX_CTRT_FRCR_DVSN_CD": fwex_ctrt_frcr_dvsn_cd,
        }

        logging.info("통합증거금 조회 (CMA_EVLU_AMT_ICLD_YN=%s)", cma_evlu_amt_icld_yn)

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.INTEGRATED_MARGIN_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_integrated_margin",
            tr_id=tr_id,
        )
        if js.get("rt_cd") != "0":
            msg_cd = js.get("msg_cd", "")
            msg1 = js.get("msg1", "")
            _log_kis_api_failure(
                api_name="inquire_integrated_margin",
                endpoint=constants.INTEGRATED_MARGIN_URL,
                tr_id=tr_id,
                request_keys=list(params.keys()),
                msg_cd=msg_cd,
                msg1=msg1,
            )
            # 토큰 만료 시 재발급 후 재시도
            if msg_cd in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self.inquire_integrated_margin(
                    is_mock=is_mock,
                    cma_evlu_amt_icld_yn=cma_evlu_amt_icld_yn,
                    wcrc_frcr_dvsn_cd=wcrc_frcr_dvsn_cd,
                    fwex_ctrt_frcr_dvsn_cd=fwex_ctrt_frcr_dvsn_cd,
                )
            # msg1 타입 안전 처리 (None 또는 비문자열 대응)
            msg1_text = str(msg1 or "")
            # OPSQ2001 + CMA_EVLU_AMT_ICLD_YN 오류 시 "Y"로 1회 재시도
            if (
                msg_cd == "OPSQ2001"
                and "CMA_EVLU_AMT_ICLD_YN" in msg1_text
                and cma_evlu_amt_icld_yn == "N"
            ):
                logging.info("OPSQ2001 CMA_EVLU_AMT_ICLD_YN 오류 발생, Y로 재시도")
                return await self.inquire_integrated_margin(
                    is_mock=is_mock,
                    cma_evlu_amt_icld_yn="Y",
                    wcrc_frcr_dvsn_cd=wcrc_frcr_dvsn_cd,
                    fwex_ctrt_frcr_dvsn_cd=fwex_ctrt_frcr_dvsn_cd,
                )
            raise RuntimeError(f"{msg_cd} {msg1_text}")

        output = js.get("output1") or js.get("output") or {}
        if isinstance(output, list):
            output = output[0] if output else {}

        return self._parse_margin_response(output)

    async def fetch_my_overseas_stocks(
        self,
        is_mock: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
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
            currency_code=currency_code,
        )

    async def fetch_my_us_stocks(
        self, is_mock: bool = False, exchange: str = "NASD"
    ) -> list[dict]:
        """
        미국 보유 주식 목록 조회 편의 메서드

        Args:
            is_mock: True면 모의투자, False면 실전투자
            exchange: 거래소 (NASD : 미국전체, NAS : 나스닥, NYSE : 뉴욕, AMEX : 아멕스)

        Returns:
            미국 보유 주식 목록
        """
        return await self.fetch_my_overseas_stocks(
            is_mock=is_mock, exchange_code=exchange, currency_code="USD"
        )
