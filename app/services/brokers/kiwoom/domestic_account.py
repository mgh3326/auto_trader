# app/services/brokers/kiwoom/domestic_account.py
"""Kiwoom domestic account/order-history queries.

All methods delegate to the parent client's ``post_api`` and never log the
account number or token. The exact body field names mirror Kiwoom REST docs;
they are passed through untransformed for the parent project to consume.
"""

from __future__ import annotations

import re
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
            # ROB-1088 (2026-07-28, independent-verification fix) — Official
            # Kiwoom REST docs
            # (https://openapi.kiwoom.com/m/guide/apiguide?apiId=kt00009&jobTp=FS_JOB_TP&jobTpCode=08)
            # list kt00009's request body as five Required=Y fields:
            #   stk_bond_tp   "0:전체, 1:주식, 2:채권"        (ROB-418)
            #   mrkt_tp       "0:전체, 1:코스피, 2:코스닥, 3:OTCBB, 4:ECN"  (ROB-1111)
            #   sell_tp       "0:전체, 1:매도, 2:매수"        (ROB-1088)
            #   qry_tp        "0:전체, 1:체결"                (ROB-1088)
            #   dmst_stex_tp  "%:(전체), KRX, NXT, SOR"       (ROB-1088)
            # An earlier revision of this fix (PR #1708 initial version) sent
            # only the first two, having treated sell_tp/qry_tp/dmst_stex_tp as
            # unproven speculation sourced only from a third-party REST client
            # (bamjun/kiwoom-rest-api). Independent verification opened the
            # official doc directly and found all five Required=Y — that
            # revision was a contract mismatch, not caution. All five values
            # below are read directly off the official table, not guessed.
            # dmst_stex_tp uses "KRX" (not "%"): kiwoom_mock is KRX-only
            # (MOCK_REJECTED_EXCHANGES={"NXT","SOR"}), and "%"(전체) would blend
            # NXT/SOR results into a fail-closed-only surface, undermining that
            # boundary even though the docs allow it as a value.
            # NOTE: return_code 0 for this exact five-field body has NOT been
            # confirmed via a real mockapi.kiwoom.com call in this codebase —
            # only unit tests exercise it (mutation/live-call is out of scope
            # for this fix). The 07-29 08:50 KR-B1 P0-3 smoke is the first real
            # verification opportunity.
            body={
                "stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT,
                "mrkt_tp": constants.ACCOUNT_ORDER_MRKT_TP_DEFAULT,
                "sell_tp": constants.ACCOUNT_ORDER_SELL_TP_DEFAULT,
                "qry_tp": constants.ACCOUNT_ORDER_QRY_TP_DEFAULT,
                "dmst_stex_tp": constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_order_detail(
        self,
        *,
        order_date: str | None = None,
        qry_tp: str = constants.ACCOUNT_ORDER_DETAIL_QRY_TP_DEFAULT,
        symbol: str | None = None,
        from_order_no: str | None = None,
        dmst_stex_tp: str = constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """Read kt00007 (계좌별주문체결내역상세요청) with the official body.

        ROB-1155 — the previous implementation sent ``{"ord_no": ...}``: a field
        that does not exist in kt00007's official request table, while omitting
        all four Required=Y fields. Official body (verified 2026-07-29 against
        https://openapi.kiwoom.com/m/guide/apiguide?apiId=kt00007&jobTp=FS_JOB_TP&jobTpCode=08
        and the local extraction of the same doc) is exactly these 7 fields:

            ord_dt (N) · qry_tp (Y) · stk_bond_tp (Y) · sell_tp (Y)
            stk_cd (N) · fr_ord_no (N) · dmst_stex_tp (Y)

        Optional fields are sent as empty strings, matching the official Request
        Example (which spells out ``"ord_dt": ""`` rather than omitting the key);
        key-omission and empty-string are not documented as equivalent.

        ``from_order_no`` maps to ``fr_ord_no``, whose official semantics are
        "start order number — earlier orders are excluded". It is a lower bound,
        NOT an exact match, so single-order lookups must filter the response rows
        on ``ord_no`` locally (see ``kiwoom_mock_get_order_detail``).

        ``dmst_stex_tp`` accepts only ACCOUNT_READ_VENUE_ALLOWLIST ({KRX, NXT}).
        This is a read-only observation argument: the order path (kt10000-kt10003)
        is untouched and stays KRX-pinned.
        """
        venue = str(dmst_stex_tp).strip().upper()
        if venue not in constants.ACCOUNT_READ_VENUE_ALLOWLIST:
            raise ValueError(
                "kt00007 read venue must be one of "
                f"{sorted(constants.ACCOUNT_READ_VENUE_ALLOWLIST)}; got {dmst_stex_tp!r}"
            )
        query_type = str(qry_tp).strip()
        if query_type not in constants.ACCOUNT_ORDER_DETAIL_QRY_TYPES:
            raise ValueError(
                "kt00007 qry_tp must be one of "
                f"{sorted(constants.ACCOUNT_ORDER_DETAIL_QRY_TYPES)}; got {qry_tp!r}"
            )
        ord_dt = "" if order_date is None else str(order_date).strip()
        if ord_dt and not re.fullmatch(r"[0-9]{8}", ord_dt):
            raise ValueError(f"kt00007 order_date must be YYYYMMDD; got {order_date!r}")
        stk_cd = "" if symbol is None else normalize_krx_symbol(symbol)
        fr_ord_no = "" if from_order_no is None else str(from_order_no).strip()
        if fr_ord_no and not re.fullmatch(r"[0-9]+", fr_ord_no):
            raise ValueError(
                f"kt00007 from_order_no must be numeric; got {from_order_no!r}"
            )
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_DETAIL_API_ID,
            path=ACCOUNT_PATH,
            body={
                "ord_dt": ord_dt,
                "qry_tp": query_type,
                "stk_bond_tp": constants.ACCOUNT_ORDER_DETAIL_STK_BOND_TP_DEFAULT,
                "sell_tp": constants.ACCOUNT_ORDER_DETAIL_SELL_TP_DEFAULT,
                "stk_cd": stk_cd,
                "fr_ord_no": fr_ord_no,
                "dmst_stex_tp": venue,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )
