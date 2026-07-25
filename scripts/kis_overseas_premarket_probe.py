#!/usr/bin/env python3
"""KIS 해외 프리마켓(HHDFS00000300) 실측 프로브 (ROB-922).

미국 프리마켓 창(21:00~22:25 KST, 즉 08:00~09:25 ET 서머타임 기준)에 KIS
HHDFS00000300이 실제로 어떤 가격을 반환하는지 — **프리마켓 실가격**인지
**전일종가 그대로**인지 — 실측하기 위한 read-only 진단 스크립트다.

같은 심볼에 대해 Yahoo prepost(``fetch_prepost_quote``, ROB-922)를 대조군으로
나란히 출력한다 — 두 소스가 얼마나 다른지 비교해서 KIS 프리마켓 가격의
신뢰도를 판단하는 근거로 쓴다.

READ-ONLY: ``KISClient().inquire_overseas_price`` (조회 TR)만 호출한다.
주문/계좌 TR은 절대 호출하지 않는다. 브로커/주문/워치 mutation 없음.

Usage
-----
    uv run python -m scripts.kis_overseas_premarket_probe --symbols AAPL,NVDA
    uv run python -m scripts.kis_overseas_premarket_probe --symbols AAPL --json

Exit codes
----------
    0  - 모든 심볼 조회 완료(가격 유무와 무관 — 이 스크립트는 진단용이며
         "프리마켓가 vs 전일종가" 판정은 사람이 출력을 보고 내린다)
    3  - 라이브 KIS creds 미설정
    1  - 예기치 못한 예외
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

_REQUIRED_KIS_ENV: tuple[str, ...] = ("KIS_APP_KEY", "KIS_APP_SECRET")
_DEFAULT_SYMBOLS: tuple[str, ...] = ("AAPL", "NVDA")


def missing_kis_cred_names() -> list[str]:
    """Return the env-var NAMES (never values) of unset live KIS creds."""
    return [name for name in _REQUIRED_KIS_ENV if not os.environ.get(name)]


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def probe_symbol(symbol: str) -> dict[str, Any]:
    """Fetch the KIS overseas price + Yahoo prepost quote for one symbol.

    Read-only: ``inquire_overseas_price`` (HHDFS00000300) and
    ``fetch_prepost_quote`` (Yahoo prepost 1m bar) only — no order/account TR.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.core.symbol import to_db_symbol
    from app.mcp_server.tooling.market_session import us_market_session
    from app.services.brokers.kis.client import KISClient
    from app.services.brokers.yahoo.client import fetch_prepost_quote
    from app.services.us_symbol_universe_service import (
        USSymbolInactiveError,
        USSymbolNotRegisteredError,
        USSymbolUniverseEmptyError,
        get_us_exchange_by_symbol,
    )

    normalized = str(symbol or "").strip().upper()
    now_utc = dt.datetime.now(dt.UTC)
    now_kst = now_utc.astimezone(ZoneInfo("Asia/Seoul"))
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    session_label = us_market_session(now_utc)

    result: dict[str, Any] = {
        "symbol": normalized,
        "now_kst": now_kst.isoformat(),
        "now_et": now_et.isoformat(),
        "us_market_session": session_label,
    }

    try:
        exchange_code = await get_us_exchange_by_symbol(to_db_symbol(normalized))
    except (
        USSymbolNotRegisteredError,
        USSymbolInactiveError,
        USSymbolUniverseEmptyError,
    ) as exc:
        result["kis"] = {"error": f"{type(exc).__name__}: {exc}"}
        exchange_code = None
    else:
        result["venue"] = exchange_code

    if exchange_code is not None:
        try:
            df = await KISClient().inquire_overseas_price(normalized, exchange_code)
        except Exception as exc:  # noqa: BLE001 — 진단 출력, 다음 심볼로 계속
            result["kis"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            if df.empty:
                result["kis"] = {"price": None, "note": "empty frame (no price)"}
            else:
                row = df.iloc[0].to_dict()
                result["kis"] = {
                    "price": _to_float(row.get("close")),
                    "previous_close": _to_float(row.get("previous_close")),
                    "volume": row.get("volume"),
                    "quote_asof": row.get("quote_asof"),
                }

    try:
        prepost = await fetch_prepost_quote(normalized)
    except Exception as exc:  # noqa: BLE001 — 진단 출력
        result["yahoo_prepost"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        result["yahoo_prepost"] = prepost if prepost is not None else {"price": None}

    return result


def _print_human(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(f"[probe] symbol={row['symbol']}")
        print(
            f"        now_kst={row['now_kst']} now_et={row['now_et']} "
            f"us_market_session={row['us_market_session']}"
        )
        if "venue" in row:
            print(f"        venue={row['venue']}")
        kis = row.get("kis") or {}
        print(f"        kis(HHDFS00000300)      = {kis}")
        yahoo = row.get("yahoo_prepost") or {}
        print(f"        yahoo_prepost(대조군)     = {yahoo}")
        print()


async def run_probe(*, symbols: list[str], as_json: bool) -> int:
    if missing_kis_cred_names():
        missing = ", ".join(missing_kis_cred_names())
        print(
            f"[probe] 라이브 KIS creds 미설정: {missing} "
            "(.env 또는 환경변수에 설정 후 재실행). 값은 출력하지 않음."
        )
        return 3

    rows = [await probe_symbol(symbol) for symbol in symbols]

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(rows)
        print(
            "[probe] 판정은 사람 몫: kis.price 가 전일종가(previous_close)와 "
            "동일하면 '전일종가 그대로' 의심, yahoo_prepost.price 와 근접하면 "
            "'프리마켓 실가격' 근거."
        )

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kis_overseas_premarket_probe",
        description=(
            "KIS 해외 프리마켓(HHDFS00000300) 실측 프로브 — 21:00~22:25 KST 창에서 "
            "실행해 프리마켓 실가격 여부를 확인한다 (ROB-922, read-only)"
        ),
    )
    parser.add_argument(
        "--symbols",
        default=",".join(_DEFAULT_SYMBOLS),
        help=f"콤마 구분 심볼 목록 (기본: {','.join(_DEFAULT_SYMBOLS)})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="사람이 읽는 출력 대신 JSON 배열 출력",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    try:
        return asyncio.run(run_probe(symbols=symbols, as_json=args.json))
    except KeyboardInterrupt:
        print("[probe] interrupted")
        return 1
    except Exception as exc:  # noqa: BLE001 — 최상위 진단
        print(f"[probe] 예기치 못한 예외({type(exc).__name__}): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
