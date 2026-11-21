"""KIS 국내 선물 모의(주간) 주문 수동 CLI (hk 5946).

미리보기가 기본이다. ``--confirm`` 을 줘야 모의 주문 **한 건**이 나간다.
수량(1계약)·명목 상한·손실 한도·가드 폭은 인자로 바꿀 수 없다(잠금 상수).
MCP 도구도 스케줄러도 없다. 환경 게이트: ``KIS_FUTURES_MOCK_ENABLED=true``(전송),
``KIS_FUTURES_READ_ENABLED=true``(마스터·잔고 읽기), ``KIS_FUTURES_MOCK_ACCOUNT_NO``.

사용 예 (런북 docs/runbooks/kis-futures-mock-order.md):
    uv run python -m scripts.kis_futures_mock_order place --symbol A05612 \
        --side buy --price 400.00 --strategy operator-smoke
    uv run python -m scripts.kis_futures_mock_order place ... --confirm
    uv run python -m scripts.kis_futures_mock_order cancel --broker-order-id 0000012345 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.kis_futures_mock.place import (
    PlaceRequest,
    cancel_futures_mock_order,
    place_futures_mock_order,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_REJECTED = 3
EXIT_UNCERTAIN = 4


def _price(text: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("가격은 10진수여야 합니다") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("가격은 양의 유한값이어야 합니다")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KIS 국내 선물 모의(주간) 주문 — 기본은 미리보기"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    place = sub.add_parser("place", help="미니 KOSPI200 선물 지정가 1계약")
    place.add_argument("--symbol", required=True, help="KIS 단축코드 (예: A05612)")
    place.add_argument("--side", required=True, choices=("buy", "sell"))
    place.add_argument("--price", required=True, type=_price)
    place.add_argument("--strategy", help="귀속 strategy (--confirm 시 필수)")
    place.add_argument("--signal-source", help="신호 출처 (기본값 사용 가능)")
    place.add_argument("--correlation-id")
    place.add_argument("--rung", help="같은 날 같은 내용의 별도 주문 구분자")
    place.add_argument(
        "--confirm", action="store_true", help="모의 주문 1건을 실제로 보낸다"
    )

    cancel = sub.add_parser("cancel", help="오늘 이 레인이 보낸 주문 하나 전량 취소")
    cancel.add_argument("--broker-order-id", required=True)
    cancel.add_argument("--confirm", action="store_true", help="취소를 실제로 보낸다")
    return parser


def exit_code(result: dict[str, Any]) -> int:
    status = result.get("status")
    if status in ("preview", "accepted"):
        return EXIT_OK
    if status == "refused":
        return EXIT_REFUSED
    if status == "rejected":
        return EXIT_REJECTED
    if status == "uncertain":
        return EXIT_UNCERTAIN
    return EXIT_ERROR


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "place":
        return await place_futures_mock_order(
            PlaceRequest(
                broker_symbol=args.symbol,
                side=args.side,
                price=args.price,
                quantity=1,
                strategy=args.strategy,
                signal_source=args.signal_source,
                correlation_id=args.correlation_id,
                rung=args.rung,
            ),
            confirm=args.confirm,
        )
    return await cancel_futures_mock_order(
        broker_order_id=args.broker_order_id, confirm=args.confirm
    )


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return await run(args)
    finally:
        from app.core.db import engine

        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001 - 예외 문자열(요청 세부) 대신 클래스만 낸다
        print(json.dumps({"status": "error", "error_class": type(exc).__name__}))
        return EXIT_ERROR
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
