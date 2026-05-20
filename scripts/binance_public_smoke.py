"""Binance Public Market Data Smoke

ROB-285: 운영 서버에서 Binance 공개 REST + WebSocket 핸드셰이크가 정상
동작하는지 빠르게 검증한다. API key 사용 안 함. DB write 안 함
(``--dry-run`` 기본값). 호스트 allowlist + rate-limit 헤더 출력으로
end-to-end 가시화.

Exit codes:
    0   - 모든 smoke 성공
    1   - 예기치 못한 예외
    2   - REST exchangeInfo 실패
    3   - REST klines backfill 실패
    4   - WebSocket connect 실패
    5   - 호스트 allowlist rejection 동작 실패 (defense-in-depth 검증)
    130 - SIGINT (Ctrl-C)

사용법:
    uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --dry-run
    uv run python -m scripts.binance_public_smoke \\
        --symbols BTCUSDT,ETHUSDT --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.rest_client import BinancePublicRestClient
from app.services.brokers.binance.ws_client import BinancePublicWSClient


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default=None, help="single symbol shortcut")
    p.add_argument(
        "--symbols",
        default="BTCUSDT,ETHUSDT,SOLUSDT",
        help="comma-separated WS symbols",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=15,
        help="WS subscribe duration in seconds",
    )
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    log = logging.getLogger("binance_public_smoke")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    symbol = args.symbol or args.symbols.split(",")[0]

    # 1. REST exchangeInfo
    try:
        async with BinancePublicRestClient() as rest:
            info = await rest.exchange_info(symbol)
            log.info(
                f"exchangeInfo OK: {info.symbol} {info.status} "
                f"{info.base_asset}/{info.quote_asset}"
            )
    except Exception as exc:
        log.error(f"exchangeInfo FAIL: {exc}")
        return 2

    # 2. REST klines backfill (last 5 minutes)
    try:
        async with BinancePublicRestClient() as rest:
            klines = await rest.klines(
                symbol,
                "1m",
                start_time=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=5),
                limit=10,
            )
            log.info(f"klines OK: {len(klines)} rows")
    except Exception as exc:
        log.error(f"klines FAIL: {exc}")
        return 3

    # 3. Allowlist defense-in-depth: rejecting fapi.binance.com should raise.
    try:
        async with BinancePublicRestClient() as rest:
            try:
                await rest._send("GET", "https://fapi.binance.com/fapi/v1/ping")  # noqa: SLF001
            except BinanceLiveHostBlocked:
                log.info("allowlist OK: fapi.binance.com correctly rejected")
            else:
                log.error("allowlist FAIL: fapi.binance.com was NOT rejected")
                return 5
    except BinanceLiveHostBlocked:
        # If the rejection bubbled out of the context manager's exit path,
        # it's still a correct rejection. Treat as PASS.
        log.info("allowlist OK: fapi.binance.com correctly rejected")
    except Exception as exc:
        log.error(f"allowlist check FAIL: {exc}")
        return 5

    # 4. WebSocket connect + receive at least one kline event
    syms = "/".join(f"{s.lower()}@kline_1m" for s in args.symbols.split(","))
    url = f"wss://stream.binance.com:9443/stream?streams={syms}"
    received = 0
    try:
        async with BinancePublicWSClient(url=url) as ws:
            loop = asyncio.get_event_loop()
            stop_at = loop.time() + args.duration
            async for event in ws.events():
                log.info(f"ws event: {event}")
                received += 1
                if loop.time() >= stop_at:
                    break
                if received >= 3:
                    break
    except Exception as exc:
        log.error(f"WS FAIL: {exc}")
        return 4

    log.info(
        f"smoke OK (dry_run={args.dry_run}; received {received} WS events)"
    )
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.error(f"unexpected: {exc}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
