"""hk 6067 — Binance Spot Demo read-only stream collector (manual CLI).

Default off: requires ``BINANCE_SPOT_DEMO_STREAMS_ENABLED=true`` plus
``--symbols`` (or ``BINANCE_SPOT_DEMO_STREAMS_SYMBOLS``). Subscribes to the
Spot Demo market streams and the user data stream and writes only Redis
streams (``book:binance_spot_demo:<SYM>``, ``fills:binance_spot_demo``) and an
optional JSONL recording. No orders, no ledger writes, no scheduler. There is
no host/URL flag by design.

    BINANCE_SPOT_DEMO_STREAMS_ENABLED=true \\
      uv run python -m scripts.binance_spot_demo_streams --symbols BTCUSDT,ETHUSDT \\
      [--record-dir DIR] [--no-user-stream] [--duration-s N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from app.services.brokers.binance.demo.errors import BinanceDemoCredentialError
from app.services.brokers.binance.spot_demo_streams.collector import (
    JsonlRecorder,
    SpotDemoStreamCollector,
    SpotDemoStreamsDisabled,
    StreamSettings,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--symbols", default=None, help="comma-separated, e.g. BTCUSDT,ETHUSDT"
    )
    p.add_argument(
        "--record-dir", type=Path, default=None, help="append JSONL recordings here"
    )
    p.add_argument("--no-user-stream", action="store_true", help="market streams only")
    p.add_argument(
        "--duration-s", type=float, default=None, help="stop after N seconds"
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    import redis.asyncio as aioredis

    from app.core.config import settings as app_settings

    stream_settings = StreamSettings.from_env(
        os.environ, symbols=args.symbols, enable_user_stream=not args.no_user_stream
    )
    client = aioredis.from_url(app_settings.get_redis_url(), decode_responses=True)
    collector = SpotDemoStreamCollector(
        stream_settings,
        client,
        recorder=JsonlRecorder(args.record_dir) if args.record_dir else None,
    )
    try:
        if args.duration_s:
            asyncio.get_running_loop().call_later(args.duration_s, collector.stop.set)
        task = asyncio.create_task(collector.run())
        if args.duration_s:
            await asyncio.wait({task}, timeout=args.duration_s + 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await task
    finally:
        await client.aclose()
        print(collector.stats)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SpotDemoStreamsDisabled as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (BinanceDemoCredentialError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
