# app/services/brokers/kiwoom/domestic_account.py
"""Kiwoom domestic account/order-history queries.

All methods delegate to the parent client's ``post_api`` and never log the
account number or token. The exact body field names mirror Kiwoom REST docs;
they are passed through untransformed for the parent project to consume.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.validation import normalize_krx_symbol

ACCOUNT_PATH = "/api/dostk/acnt"


class _SupportsPostApi(Protocol):
    account_no: str

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]: ...


class KiwoomDomesticAccountClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def get_orderable_amount(
        self,
        *,
        symbol: str,
        side: str | None = None,
        price: int | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        # ROB-891 — Official kt00010 body fields: stk_cd, trde_tp, uv.
        # Both side and price are required (trde_tp + uv are mandatory).
        # dmst_stex_tp is NOT in the official kt00010 docs.
        #
        # ROB-904 — kt00010 (주문인출가능금액) is unsupported by
        # mockapi.kiwoom.com: a 2026-07-16 wire-body smoke sent this exact
        # official-contract body across 4 field variants and got
        # return_code=20 ("[2000](RC7006:모의투자 조회실패)") every time (same
        # class of gap as US ust31490). No caller in this codebase invokes
        # this method anymore — buy preflight and the orderable-cash MCP tool
        # both fall back to kt00001 (get_deposit/normalize_deposit) instead.
        # This method is kept, unmodified, as the correct implementation
        # against Kiwoom's official contract for when/if live trading (or a
        # future mock fix) needs it. Do NOT "fix" it by tweaking body fields —
        # the contract is already correct; the gap is server-side.
        canonical_symbol = normalize_krx_symbol(symbol)
        if side not in ("buy", "sell"):
            raise ValueError(
                f"kt00010 symbol path requires side='buy'|'sell'; got {side!r}"
            )
        if price is None or type(price) is not int or price <= 0:
            raise ValueError(
                f"kt00010 symbol path requires a positive int price; got {price!r}"
            )
        body: dict[str, Any] = {
            "stk_cd": canonical_symbol,
            "trde_tp": (
                constants.TRADE_TYPE_BUY if side == "buy" else constants.TRADE_TYPE_SELL
            ),
            "uv": str(price),
        }
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
            path=ACCOUNT_PATH,
            body=body,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_deposit(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        # ROB-891 — Official kt00001 body is exactly {"qry_tp": "2"}.
        # "2" = 일반조회 (current orderable cash). dmst_stex_tp is NOT in
        # the official kt00001 docs.
        return await self._client.post_api(
            api_id=constants.ACCOUNT_DEPOSIT_API_ID,
            path=ACCOUNT_PATH,
            body={"qry_tp": constants.ACCOUNT_DEPOSIT_QRY_TP_DEFAULT},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_balance(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_BALANCE_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00018 requires qry_tp; omitting it returns return_code 2
            # (필수입력 파라미터=qry_tp). Value is convention-default, smoke-confirmed.
            # ROB-460 — kt00018 ALSO requires dmst_stex_tp (국내거래소구분); 2026-06-09
            # live returned return_code 2 (필수입력 파라미터=dmst_stex_tp) via
            # get_positions/get_orderable_cash. Value "KRX" proven by order endpoints.
            body={
                "qry_tp": constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT,
                "dmst_stex_tp": constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_order_status(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00009 requires stk_bond_tp; omitting it returns
            # return_code 2 (필수입력 파라미터=stk_bond_tp).
            # ROB-1111 — kt00009 ALSO requires mrkt_tp; omitting it returns
            # return_code 2 (필수입력 파라미터=mrkt_tp).
            #
            # ROB-1088 — mrkt_tp="0" (시장구분: 전체) 값의 근거: 공식 Kiwoom REST
            # 문서는 이 세션에서 직접 확보하지 못했다(apiportal.kiwoom.com 접근 불가,
            # 일반 웹검색으로 kt00009 명세 미발견). 대신 kt00009와 필드가 거의 겹치는
            # kt00007/kt00018/kt00010 body를 실제 필드명 그대로 문서화한 서드파티
            # REST 클라이언트(bamjun/kiwoom-rest-api, MIT, PyPI 배포)의
            # account_order_execution_status_request_kt00009 시그니처를 대조 근거로
            # 사용했다: stk_bond_tp(0:전체/1:주식/2:채권), mrkt_tp(0:전체/1:코스피/
            # 2:코스닥/3:OTCBB/4:ECN), sell_tp(0:전체/1:매도/2:매수), qry_tp(0:전체/
            # 1:체결), dmst_stex_tp(%:전체/KRX/NXT/SOR) 5개를 전부 위치 인자(문서상
            # optional 표기 없음)로 요구한다.
            # 이 라이브러리의 kt00007/kt00018 필드명·구조가 이 코드베이스의 실측
            # 확정값(ROB-418/ROB-460/ROB-891)과 100% 일치해 신뢰도가 있다.
            # 다만 sell_tp/qry_tp/dmst_stex_tp는 이 서드파티 문서에만 근거하며
            # 이 코드베이스의 실측(return_code 2)으로 아직 증명되지 않았다 — 추측
            # 파라미터를 여기서 추가하지 않는다(작동 중인 stk_bond_tp+mrkt_tp 조합을
            # 회귀시키지 않기 위함). 07-29 08:50 스모크에서
            # `필수입력 파라미터=sell_tp|qry_tp|dmst_stex_tp` 형태의 return_code 2가
            # 재현되면 그 파라미터명을 근거로 후속 수정을 연다.
            body={
                "stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT,
                "mrkt_tp": constants.ACCOUNT_ORDER_MRKT_TP_DEFAULT,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_order_detail(
        self,
        *,
        order_no: str,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_DETAIL_API_ID,
            path=ACCOUNT_PATH,
            body={"ord_no": str(order_no).strip()},
            cont_yn=cont_yn,
            next_key=next_key,
        )
