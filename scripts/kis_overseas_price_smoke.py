#!/usr/bin/env python3
"""KIS 해외 현재가(HHDFS00000300) live smoke (ROB-471).

``get_quote(market="us")``의 KIS-primary 전환(ROB-471)을 **라이브** 검증한다.
조회 TR 한정 — **broker/order/watch mutation 없음, READ-ONLY**. 라이브 KIS
creds(``KIS_APP_KEY`` / ``KIS_APP_SECRET``)가 필요하다.

핵심 목적
---------
1. 실 HHDFS00000300 ``output`` **필드명**(``last`` / ``base`` / ``tvol``)을
   확인한다. 운영 코드가 이 셋을 각각 close / previous_close / volume 으로 매핑
   하므로, 라이브 응답 필드가 다르면 가격이 빈값→Yahoo fallback 으로 떨어진다
   (이게 "필드 매핑 수정 필요" 신호). 이 스모크가 그 사실을 즉시 드러낸다.
2. 운영 파서(``_build_overseas_price_frame``)를 라이브 raw 응답에 그대로 적용해
   close / previous_close / volume 파싱 결과를 보여준다.
3. (``--via-get-quote``) ``_fetch_quote_equity_us`` 전체 경로를 태워
   ``source == "kis_overseas"`` / ``delayed`` 정직 메타를 점검한다.

모드/옵션
---------
* ``--symbol``           조회 심볼 (기본 AAPL). DB 점(.) 표기 허용(BRK.B).
* ``--exchange``         NASD/NYSE/AMEX (기본 NASD).
* ``--resolve-exchange`` ``--exchange`` 무시, ``get_us_exchange_by_symbol``(DB)
                         로 거래소 해석(운영 get_quote 경로와 동일).
* ``--via-get-quote``    추가로 ``_fetch_quote_equity_us`` 실행 → source/price/delayed.

안전
----
라이브 read-only 조회. 별도 enable 플래그 없음(주문 mutation이 없으므로). 라이브
creds 미설정 시 누락 **env 키 NAME만** 보고하고 종료(값 출력 없음). 호스트는
``KIS_BASE_URL`` 라이브 그대로.

Exit codes
----------
    0  - last(현재가) 파싱 성공(price>0) + 필드 매핑 일치 (KIS-primary 라이브 OK)
    2  - last 부재/0 → 필드명 불일치 의심 또는 비거래/세션 (raw output 확인 필요)
    3  - 라이브 KIS creds 미설정 / 거래소 미해석
    1  - 예기치 못한 예외

Usage
-----
    uv run python -m scripts.kis_overseas_price_smoke --symbol AAPL --exchange NASD
    uv run python -m scripts.kis_overseas_price_smoke --symbol BRK.B --resolve-exchange --via-get-quote
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

EXPECTED_FIELDS: tuple[str, ...] = ("last", "base", "tvol")
_REQUIRED_KIS_ENV: tuple[str, ...] = ("KIS_APP_KEY", "KIS_APP_SECRET")


def missing_kis_cred_names() -> list[str]:
    """Return the env-var NAMES (never values) of unset live KIS creds."""
    return [name for name in _REQUIRED_KIS_ENV if not os.environ.get(name)]


def evaluate_field_presence(output: Mapping[str, Any]) -> dict[str, bool]:
    """Map each expected HHDFS00000300 field to present-and-non-blank."""
    return {
        field: output.get(field) not in (None, "") for field in EXPECTED_FIELDS
    }


def decide_exit_code(price: float | None) -> int:
    """0 when a usable current price was parsed, else 2 (inspect raw output)."""
    if price is not None and price > 0:
        return 0
    return 2


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def run_smoke(
    *,
    symbol: str,
    exchange_code: str,
    resolve_exchange: bool,
    via_get_quote: bool,
) -> int:
    if missing_kis_cred_names():
        missing = ", ".join(missing_kis_cred_names())
        print(
            f"[smoke] 라이브 KIS creds 미설정: {missing} "
            "(.env 또는 환경변수에 설정 후 재실행). 값은 출력하지 않음."
        )
        return 3

    # Lazy imports: keep the module importable (and the pure helpers testable)
    # without live creds — the Settings singleton requires KIS_APP_KEY/SECRET.
    from app.core.symbol import to_db_symbol, to_kis_symbol
    from app.services.brokers.kis import constants
    from app.services.brokers.kis.client import KISClient

    normalized = str(symbol or "").strip().upper()

    if resolve_exchange:
        from app.services.us_symbol_universe_service import (
            get_us_exchange_by_symbol,
        )

        try:
            exchange_code = await get_us_exchange_by_symbol(to_db_symbol(normalized))
            print(f"[smoke] resolved exchange via us_symbol_universe: {exchange_code}")
        except Exception as exc:  # noqa: BLE001 — 진단 출력 후 종료
            print(f"[smoke] 거래소 해석 실패({type(exc).__name__}): {exc} → exit 3")
            return 3

    client = KISClient()
    market_data = client._market_data
    excd = constants.get_exchange_code_3digit(exchange_code)
    symb = to_kis_symbol(normalized)
    print(
        f"[smoke] HHDFS00000300 요청 — symbol={normalized} exchange={exchange_code} "
        f"EXCD={excd} SYMB={symb} host={constants.OVERSEAS_PRICE_URL}"
    )

    js = await market_data._request_with_token_retry(
        tr_id=constants.OVERSEAS_PRICE_TR,
        url=market_data._kis_url(constants.OVERSEAS_PRICE_URL),
        params={"AUTH": "", "EXCD": excd, "SYMB": symb},
        timeout=10,
        api_name="kis_overseas_price_smoke",
    )
    output = js.get("output") or {}

    print("[smoke] RAW KIS output dict (필드명 확인용):")
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    presence = evaluate_field_presence(output)
    checks = " ".join(
        f"{field}={'OK' if present else 'MISSING'}"
        for field, present in presence.items()
    )
    print(f"[smoke] 필드명 체크 (운영 파서 매핑): {checks}")
    if not all(presence.values()):
        unexpected = sorted(set(output) - set(EXPECTED_FIELDS))
        print(
            "[smoke] ⚠️ 일부 기대 필드 누락 — 라이브 응답의 실제 키를 확인하고 "
            f"필요 시 파서 매핑 조정. 응답에 존재하는 키: {unexpected or list(output)}"
        )

    # Same raw response through the PRODUCTION parser (no second request).
    frame = market_data._build_overseas_price_frame(output)
    if frame.empty:
        price: float | None = None
        print("[smoke] 파싱 결과: EMPTY frame (현재가 없음/0) → Yahoo fallback 대상")
    else:
        row = frame.iloc[0].to_dict()
        price = _to_float(row.get("close"))
        print(
            "[smoke] 파싱 결과(운영 _build_overseas_price_frame): "
            f"close={row.get('close')} previous_close={row.get('previous_close')} "
            f"volume={row.get('volume')}"
        )

    if via_get_quote:
        from app.mcp_server.tooling.market_data_quotes import _fetch_quote_equity_us

        try:
            quote = await _fetch_quote_equity_us(normalized)
            print(
                "[smoke] get_quote(US) 전체 경로: "
                f"source={quote.get('source')} price={quote.get('price')} "
                f"previous_close={quote.get('previous_close')} "
                f"delayed={quote.get('delayed')}"
            )
            if quote.get("source") != "kis_overseas":
                print(
                    "[smoke] ⚠️ source가 kis_overseas가 아님 — KIS 경로가 빈값/에러로 "
                    "Yahoo fallback 되었음. 위 RAW output/필드 체크를 확인."
                )
        except Exception as exc:  # noqa: BLE001 — 진단 출력
            print(f"[smoke] get_quote(US) 예외({type(exc).__name__}): {exc}")

    code = decide_exit_code(price)
    verdict = "OK (KIS-primary 라이브 현재가 확인)" if code == 0 else (
        "현재가 없음/0 — raw output 필드명/세션 확인 필요"
    )
    print(f"[smoke] verdict: exit={code} — {verdict}")
    return code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kis_overseas_price_smoke",
        description="KIS 해외 현재가(HHDFS00000300) 라이브 read-only 스모크 (ROB-471)",
    )
    parser.add_argument("--symbol", default="AAPL", help="조회 심볼 (기본 AAPL)")
    parser.add_argument(
        "--exchange",
        default="NASD",
        choices=["NASD", "NYSE", "AMEX"],
        help="거래소 코드 (기본 NASD; --resolve-exchange 시 무시)",
    )
    parser.add_argument(
        "--resolve-exchange",
        action="store_true",
        help="us_symbol_universe(DB)로 거래소 해석 (운영 get_quote 경로와 동일)",
    )
    parser.add_argument(
        "--via-get-quote",
        action="store_true",
        help="_fetch_quote_equity_us 전체 경로 실행 → source/delayed 점검",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(
            run_smoke(
                symbol=args.symbol,
                exchange_code=args.exchange,
                resolve_exchange=args.resolve_exchange,
                via_get_quote=args.via_get_quote,
            )
        )
    except KeyboardInterrupt:
        print("[smoke] interrupted")
        return 1
    except Exception as exc:  # noqa: BLE001 — 최상위 진단
        print(f"[smoke] 예기치 못한 예외({type(exc).__name__}): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
