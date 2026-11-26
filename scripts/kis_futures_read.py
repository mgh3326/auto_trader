"""모의 선물 계좌 읽기 미리보기 (주문 전송 없음)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.kis_futures_read.view import open_futures_view


def number(row: dict[str, Any], key: str) -> dict[str, str | None]:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        return {"value": None, "reason": "값 없음 또는 형식 오류"}
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return {"value": None, "reason": "10진수 판독 실패"}
    if not parsed.is_finite():
        return {"value": None, "reason": "유한하지 않은 값"}
    return {"value": str(parsed), "reason": None}


def numbers(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: number(row, key) for key in keys}


async def preview(
    account: str, symbol: str | None, side: str, price: str, order_type: str
) -> dict[str, Any]:
    view = open_futures_view(is_mock=True)
    result: dict[str, Any] = {"계좌": "[MASKED]", "상품코드": "03"}
    try:
        try:
            balance = await view.futures_balance(account)
            result["잔고"] = numbers(
                balance["summary"],
                (
                    "dnca_cash",
                    "tot_dncl_amt",
                    "cash_mgna",
                    "mgna_tota",
                    "ord_psbl_cash",
                ),
            )
            result["포지션"] = [
                {
                    "종목": row.get("shtn_pdno"),
                    "방향": row.get("sll_buy_dvsn_name"),
                    **numbers(
                        row,
                        ("cblc_qty", "excc_unpr", "ccld_avg_unpr1", "evlu_pfls_amt"),
                    ),
                }
                for row in balance["positions"]
            ]
        except Exception as exc:  # noqa: BLE001 - no account or provider message in output
            result["잔고"] = result["포지션"] = {
                "value": None,
                "reason": type(exc).__name__,
            }
        try:
            deposit = await view.futures_deposit(account)
            result["예수금"] = numbers(
                deposit,
                ("dnca_tota", "brkg_mgna_cash", "add_mgna_tota", "ord_psbl_cash"),
            )
        except Exception as exc:  # noqa: BLE001
            from app.services.brokers.kis.futures_read import FuturesReadResponseError

            result["예수금"] = {
                "value": None,
                "reason": type(exc).__name__,
                "rt_cd": exc.rt_cd
                if isinstance(exc, FuturesReadResponseError)
                else None,
                "msg_cd": exc.msg_cd
                if isinstance(exc, FuturesReadResponseError)
                else None,
            }
        if symbol:
            try:
                orderable = await view.futures_orderable(
                    account, symbol, side, price, order_type
                )
                result["주문가능"] = numbers(
                    orderable,
                    ("tot_psbl_qty", "lqd_psbl_qty1", "ord_psbl_qty", "bass_idx"),
                )
            except Exception as exc:  # noqa: BLE001
                result["주문가능"] = {"value": None, "reason": type(exc).__name__}
        else:
            result["주문가능"] = {"value": None, "reason": "종목 미지정"}
    finally:
        await view.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS 모의 선물 잔고 읽기 미리보기")
    parser.add_argument("--symbol", help="주문가능 조회 종목코드")
    parser.add_argument(
        "--side", choices=("01", "02"), default="02", help="01 매도, 02 매수"
    )
    parser.add_argument("--price", help="주문가능 조회 가격 (--symbol 지정 시 필수)")
    parser.add_argument("--order-type", default="01", help="KIS 주문구분 코드")
    args = parser.parse_args()
    if args.symbol and not args.price:
        parser.error("--symbol 지정 시 --price가 필요합니다")
    account = os.environ.get("KIS_FUTURES_MOCK_ACCOUNT_NO", "")
    try:
        result = asyncio.run(
            preview(account, args.symbol, args.side, args.price or "", args.order_type)
        )
    except Exception as exc:  # noqa: BLE001
        detail: dict[str, Any] = {"오류": type(exc).__name__}
        missing = getattr(exc, "missing", None)
        if missing:
            # env 이름만 보고한다 (값은 절대 출력하지 않는다).
            detail["missing_env"] = list(missing)
        print(json.dumps(detail, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
