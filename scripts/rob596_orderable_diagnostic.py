#!/usr/bin/env python3
"""ROB-596 — KIS 국내 live 주문가능(orderable) 진단 (READ-ONLY).

증상: `kis_live_place_order`(KR 매수) 사전 잔고체크가 브로커가 받아줄 매수를
"Insufficient KRW balance: 0 KRW < N KRW" 로 거부한다. 가설: `get_cash_balance_impl`
이 이미 net 인 브로커 주문가능금액(stck_cash100_max_ord_psbl_amt)에서 미체결 매수
(`_get_kis_domestic_pending_buy_amount`)를 한 번 더 빼는 double-count.

이 스크립트는 **읽기 전용**이다. 주문/정정/취소 등 어떤 mutation 도 하지 않는다.
운영자가 **미체결 매수 주문이 걸려 있는 순간**(예: 매수 래더 거치 직후) 1회 실행하면,
프로덕션과 동일 경로로 다음을 한 화면에 보여 준다:

  1) inquire_integrated_margin 의 orderable 후보 필드 전부 + 어느 필드가 선택됐는지
     (= raw_orderable). 한투 앱의 "주문가능" 과 비교해 net 여부 확정.
  2) inquire_korea_orders 의 미체결 매수 row 별 (ord_unpr, nccs_qty, ord_qty) 와
     nccs_qty=0→ord_qty 폴백으로 인한 과대합산 여부.
  3) 미체결 매수 합산 = pending_buy_amount (ROB-596 이전 double-count 차감분, 이제 비차감).
  4) 실제 MCP orderable(수정 후) = get_cash_balance_impl.orderable ← precheck 가 보는 값.
     ROB-596 수정으로 pending 을 빼지 않으므로 raw_orderable 과 같아야 한다.
     비교용으로 구(舊) 공식 max(0, raw - pending) 도 함께 출력한다.
  5) get_available_capital_impl 의 orderable (동일 소스인지 교차확인).

해석 가이드:
  * raw_orderable ≈ 한투 앱 주문가능  AND  (raw - pending) << 앱 주문가능
        → double-count 확정 (브로커 필드가 이미 net).
  * raw_orderable << 한투 앱 주문가능
        → 선택된 필드가 미정산 매도대금을 제외한 현금-only 일 가능성 (필드 선택 이슈).
  * pending_buy_amount ≈ raw_orderable
        → orderable 가 정확히 0 으로 floor 되는 이유 (관측된 "0 KRW" 설명).
  * 미체결 매수 row 중 nccs_qty 가 0/빈값인데 ord_qty>0 인 게 있으면
        → 완전체결분이 still-pending 으로 과대합산 (latent 폴백 버그).

Exit codes:
    0  - 조회 성공
    1  - 예기치 못한 예외
    4  - KIS 자격증명 미설정

사용법:
    uv run python -m scripts.rob596_orderable_diagnostic
    uv run python -m scripts.rob596_orderable_diagnostic --json   # 기계가독 JSON 만
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.core.config import settings
from app.mcp_server.tooling.portfolio_cash import (
    get_available_capital_impl,
    get_cash_balance_impl,
)
from app.services.brokers.kis import (
    KISClient,
    extract_domestic_cash_summary_from_integrated_margin,
)

# extract_domestic_cash_summary_from_integrated_margin 의 orderable 후보 우선순위
# (app/services/brokers/kis/account.py:63-72 와 동일 순서로 유지).
_ORDERABLE_CANDIDATES = (
    "stck_cash100_max_ord_psbl_amt",
    "stck_itgr_cash100_ord_psbl_amt",
    "stck_cash_ord_psbl_amt",
    "stck_cash_objt_amt",
)
_BALANCE_FIELD = "stck_cash_objt_amt"


def _to_float(val: Any, default: float = 0.0) -> float:
    if val in ("", None):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _selected_orderable_field(payload: dict[str, Any]) -> tuple[str | None, float]:
    """first_usable_positive_float 와 동일 규칙으로 선택 필드/값을 재현."""
    first_numeric: float | None = None
    first_numeric_field: str | None = None
    for field in _ORDERABLE_CANDIDATES:
        raw = payload.get(field)
        if raw in ("", None):
            continue
        try:
            parsed = float(raw)
        except (ValueError, TypeError):
            continue
        if first_numeric is None:
            first_numeric = parsed
            first_numeric_field = field
        if parsed > 0:
            return field, parsed
    if first_numeric is not None:
        return first_numeric_field, first_numeric
    return None, 0.0


async def _collect() -> dict[str, Any]:
    kis = KISClient()  # live (is_mock=False)

    # --- 1) integrated margin: orderable 후보 필드 전부 노출 ---
    margin_data = await kis.inquire_integrated_margin()
    raw = margin_data.get("raw")
    raw_payload = raw if isinstance(raw, dict) else margin_data

    candidate_values: dict[str, dict[str, float | None]] = {}
    for field in (_BALANCE_FIELD, *_ORDERABLE_CANDIDATES):
        candidate_values[field] = {
            "top_level": _to_float(margin_data.get(field), default=None)  # type: ignore[arg-type]
            if margin_data.get(field) not in ("", None)
            else None,
            "raw": _to_float(raw_payload.get(field), default=None)  # type: ignore[arg-type]
            if raw_payload.get(field) not in ("", None)
            else None,
        }

    summary = extract_domestic_cash_summary_from_integrated_margin(margin_data)
    raw_orderable = float(summary.get("orderable", 0) or 0)
    balance = float(summary.get("balance", 0) or 0)
    sel_field_top, _ = _selected_orderable_field(margin_data)
    sel_field_raw, _ = _selected_orderable_field(raw_payload)
    selected_field = sel_field_top or sel_field_raw

    # --- 2) 미체결 매수 row 들 (ord_unpr / nccs_qty / ord_qty) ---
    open_orders = await kis.inquire_korea_orders()
    open_buys: list[dict[str, Any]] = []
    for order in open_orders:
        if str(order.get("sll_buy_dvsn_cd", "")).strip() != "02":
            continue
        nccs = order.get("nccs_qty")
        ord_qty = order.get("ord_qty")
        price = _to_float(order.get("ord_unpr"))
        nccs_f = _to_float(nccs)
        ordq_f = _to_float(ord_qty)
        # 프로덕션과 동일: nccs_qty or ord_qty
        used_qty = _to_float(nccs or ord_qty)
        open_buys.append(
            {
                "pdno": order.get("pdno"),
                "ord_unpr": price,
                "nccs_qty": nccs_f,
                "ord_qty": ordq_f,
                "used_qty(nccs_or_ord)": used_qty,
                "row_amount": price * used_qty,
                "fallback_overcount": (nccs in ("", None) or nccs_f == 0.0)
                and ordq_f > 0,
            }
        )

    # --- 3) 미체결 매수 합산 (구 double-count 차감분; ROB-596 이후 비차감) ---
    pending_buy_amount = sum(r["row_amount"] for r in open_buys)

    # --- 4) 최종 MCP orderable (= precheck 가 보는 값) ---
    computed_orderable = max(0.0, raw_orderable - pending_buy_amount)
    cash_balance = await get_cash_balance_impl(account="kis_domestic")
    kis_dom = next(
        (
            a
            for a in cash_balance.get("accounts", [])
            if a.get("account") == "kis_domestic"
        ),
        None,
    )

    # --- 5) 계획 도구 교차확인 ---
    capital = await get_available_capital_impl(account="kis_domestic")

    return {
        "integrated_margin": {
            "selected_orderable_field": selected_field,
            "raw_orderable": raw_orderable,
            "balance(settled, stck_cash_objt_amt)": balance,
            "candidate_fields": candidate_values,
        },
        "open_buy_orders": {
            "count": len(open_buys),
            "rows": open_buys,
            "any_fallback_overcount": any(r["fallback_overcount"] for r in open_buys),
        },
        "pending_buy_amount": pending_buy_amount,
        "mcp_computed_orderable(max0_raw_minus_pending)": computed_orderable,
        "get_cash_balance_impl.orderable": (kis_dom or {}).get("orderable"),
        "get_available_capital_impl.orderable": next(
            (
                a.get("orderable")
                for a in capital.get("accounts", [])
                if a.get("account") == "kis_domestic"
            ),
            None,
        ),
        "errors": {
            "cash_balance": cash_balance.get("errors"),
            "available_capital": capital.get("errors"),
        },
    }


def _print_human(d: dict[str, Any]) -> None:
    im = d["integrated_margin"]
    raw_orderable = im["raw_orderable"]
    pending = d["pending_buy_amount"]
    computed = d["mcp_computed_orderable(max0_raw_minus_pending)"]

    print("=" * 72)
    print("ROB-596 KIS 국내 live orderable 진단 (READ-ONLY, 주문 없음)")
    print("=" * 72)
    print(f"선택된 orderable 필드      : {im['selected_orderable_field']}")
    print(f"raw_orderable (브로커)     : {raw_orderable:,.0f} KRW")
    print(
        f"  settled 예수금           : {im['balance(settled, stck_cash_objt_amt)']:,.0f} KRW"
    )
    print("  orderable 후보 필드들:")
    for field, vals in im["candidate_fields"].items():
        print(f"    {field:<34} top={vals['top_level']}  raw={vals['raw']}")
    print("-" * 72)
    ob = d["open_buy_orders"]
    print(f"미체결 매수 주문 수        : {ob['count']}")
    for r in ob["rows"]:
        flag = "  ⚠ FALLBACK-OVERCOUNT" if r["fallback_overcount"] else ""
        print(
            f"    {str(r['pdno']):<8} @{r['ord_unpr']:,.0f} x "
            f"nccs={r['nccs_qty']:.0f}/ord={r['ord_qty']:.0f} "
            f"= {r['row_amount']:,.0f}{flag}"
        )
    print(f"pending_buy_amount (합산)  : {pending:,.0f} KRW")
    print("-" * 72)
    actual = d["get_cash_balance_impl.orderable"]
    print(
        f"구 double-count 공식 max(0,raw-pending) : {computed:,.0f} KRW   (ROB-596 이전 precheck 값, 참고)"
    )
    print(f"실제 MCP orderable (수정 후)            : {actual}   ← precheck 가 보는 값")
    print(
        f"get_available_capital_impl.orderable    : {d['get_available_capital_impl.orderable']}"
    )
    print("=" * 72)
    print("해석:")
    if ob["count"] == 0:
        print("  · 미체결 매수가 0건 → 수정 효과(차감 안 함)를 대조할 수 없음.")
        print("    매수 래더/미체결 매수가 걸린 순간 다시 실행하세요.")
    else:
        print(f"  · raw_orderable({raw_orderable:,.0f}) 를 한투 앱 '주문가능' 과 비교:")
        print(
            "      ≈ 같으면 → 브로커 필드가 이미 net → pending 재차감은 double-count 였음."
        )
        print("      << 앱 → 선택 필드가 미정산 매도대금 제외(현금-only) 가능성.")
        if isinstance(actual, (int, float)) and raw_orderable > 0:
            if abs(actual - raw_orderable) < 1.0:
                print(
                    f"  · ✅ 수정 확인: 실제 orderable({actual:,.0f}) == raw_orderable "
                    f"→ pending({pending:,.0f}) 을 차감하지 않음."
                )
            else:
                print(
                    f"  · ⚠ 실제 orderable({actual:,.0f}) != raw_orderable({raw_orderable:,.0f}) "
                    "→ 예상과 다름, 조사 필요."
                )
        if ob["any_fallback_overcount"]:
            print(
                "  · ⚠ nccs_qty=0→ord_qty 폴백 과대합산 row 존재 (완전체결분이 pending 으로 합산됨)."
            )
    if d["errors"]["cash_balance"] or d["errors"]["available_capital"]:
        print(f"  · errors: {json.dumps(d['errors'], ensure_ascii=False)}")


async def _run(*, as_json: bool) -> int:
    data = await _collect()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, default=str, indent=2))
    else:
        _print_human(data)
        print()
        print(json.dumps(data, ensure_ascii=False, default=str, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ROB-596 KIS 국내 live orderable 진단 (read-only)"
    )
    parser.add_argument("--json", action="store_true", help="기계가독 JSON 만 출력")
    args = parser.parse_args()

    if not getattr(settings, "kis_app_key", None) or not getattr(
        settings, "kis_app_secret", None
    ):
        print(
            "KIS 자격증명(KIS_APP_KEY/KIS_APP_SECRET)이 설정되지 않았습니다.",
            file=sys.stderr,
        )
        return 4

    try:
        return asyncio.run(_run(as_json=args.json))
    except Exception as exc:  # noqa: BLE001
        print(f"진단 실패: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
