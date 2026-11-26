"""수동 Upbit 주문 가능 여부 확인. 어떤 경로도 주문을 생성하지 않습니다."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.upbit_tick import is_on_upbit_krw_tick


def _upbit_client():
    from app.services.brokers.upbit import client

    return client


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} 값을 읽을 수 없습니다") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label}은 양수여야 합니다")
    return result


def _fee_rate(value: Any, *, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} 값을 읽을 수 없습니다") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} 값을 읽을 수 없습니다")
    return result


def _market_currencies(market: str) -> tuple[str, str]:
    parts = market.split("-")
    if len(parts) != 2 or not all(part.isalnum() for part in parts):
        raise ValueError("--market 형식은 KRW-ETH 이어야 합니다")
    return parts[0].upper(), parts[1].upper()


def _account_summary(account: Any) -> tuple[Decimal, Decimal]:
    if not isinstance(account, dict):
        raise ValueError("계좌 잔고 정보를 읽을 수 없습니다")
    balance = Decimal(str(account.get("balance", "0")))
    locked = Decimal(str(account.get("locked", "0")))
    if not balance.is_finite() or not locked.is_finite() or balance < 0 or locked < 0:
        raise ValueError("계좌 잔고 정보를 읽을 수 없습니다")
    return balance, locked


def _local_verdict(
    chance: dict[str, Any], market: str, side: str, price: Decimal, volume: Decimal
) -> tuple[list[str], dict[str, str]]:
    quote_currency, base_currency = _market_currencies(market)
    if quote_currency != "KRW":
        raise ValueError("현재 CLI는 KRW 마켓만 지원합니다")
    errors: list[str] = []
    if not is_on_upbit_krw_tick(price):
        errors.append("price_tick_invalid")
    market_info = chance.get("market")
    if not isinstance(market_info, dict):
        raise ValueError("마켓 주문 조건을 읽을 수 없습니다")
    side_info = market_info.get(side)
    if not isinstance(side_info, dict):
        raise ValueError("마켓 주문 조건을 읽을 수 없습니다")
    minimum = _decimal(side_info.get("min_total"), label="최소 주문 금액")
    total = price * volume
    if total < minimum:
        errors.append("min_total_not_met")
    maximum = _decimal(market_info.get("max_total"), label="최대 주문 금액")
    if total > maximum:
        errors.append("max_total_exceeded")

    bid_fee = _fee_rate(chance.get("bid_fee"), label="매수 수수료율")
    maker_bid_fee = _fee_rate(chance.get("maker_bid_fee"), label="메이커 매수 수수료율")

    bid_balance, bid_locked = _account_summary(chance.get("bid_account"))
    ask_balance, ask_locked = _account_summary(chance.get("ask_account"))
    balance = bid_balance if side == "bid" else ask_balance
    required = (
        total * (Decimal("1") + max(bid_fee, maker_bid_fee))
        if side == "bid"
        else volume
    )
    if balance < required:
        errors.append("available_balance_insufficient")
    shown = {
        quote_currency: {"balance": str(bid_balance), "locked": str(bid_locked)},
        base_currency: {"balance": str(ask_balance), "locked": str(ask_locked)},
    }
    return errors, shown


def _error_name(exc: httpx.HTTPStatusError) -> str | None:
    try:
        data = exc.response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    name = error.get("name") if isinstance(error, dict) else None
    return name if isinstance(name, str) and name.isidentifier() else None


def _is_order_validation_error(exc: httpx.HTTPStatusError) -> bool:
    name = _error_name(exc)
    return (exc.response.status_code == 400 and name is not None) or (
        exc.response.status_code == 403 and name == "market_offline"
    )


async def _run(args: argparse.Namespace) -> int:
    market = args.market.upper()
    try:
        price = _decimal(args.price, label="가격")
        volume = _decimal(args.volume, label="수량")
        quote_currency, _ = _market_currencies(market)
        if quote_currency != "KRW":
            raise ValueError("현재 CLI는 KRW 마켓만 지원합니다")
        if args.side not in {"bid", "ask"}:
            raise ValueError("--side는 bid 또는 ask여야 합니다")
        client = _upbit_client()
        chance = await client.order_chance(market)
        errors, balances = _local_verdict(chance, market, args.side, price, volume)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "verdict": f"unavailable:{type(exc).__name__}",
                    "order_created": False,
                },
                ensure_ascii=False,
            )
        )
        return 2

    result: dict[str, Any] = {
        "market": market,
        "balances": balances,
        "local_checks": "pass" if not errors else errors,
    }
    if errors:
        result["verdict"] = "refused"
    elif args.confirm_test and os.environ.get("UPBIT_ORDER_TEST_ENABLED") != "true":
        result["verdict"] = "unavailable:OrderTestDisabled"
    elif args.confirm_test:
        try:
            await client.order_test(market, args.side, "limit", str(price), str(volume))
            result["verdict"] = "accepted_by_test"
        except httpx.HTTPStatusError as exc:
            name = _error_name(exc)
            result["verdict"] = (
                f"rejected:{name}"
                if name and _is_order_validation_error(exc)
                else "unavailable:HTTPStatusError"
            )
        except Exception as exc:
            result["verdict"] = f"unavailable:{type(exc).__name__}"
    else:
        result["verdict"] = "local_checks_pass"
    result["order_created"] = False
    result["statement"] = (
        "검증 통과는 주문 생성, 주문 수락 또는 체결을 뜻하지 않습니다."
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["verdict"] in {"accepted_by_test", "local_checks_pass"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upbit 주문 조건을 조회하고 주문 생성 없이 검증합니다."
    )
    parser.add_argument("--market", required=True)
    parser.add_argument("--side", choices=("bid", "ask"), required=True)
    parser.add_argument("--price", required=True)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--confirm-test", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
