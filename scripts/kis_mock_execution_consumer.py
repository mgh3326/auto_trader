"""ROB-404 — operator CLI for the kis_mock execution-event consumer.

Default-disabled by design: actual broker mutation only when
KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED=true. ``preflight`` forces dry-run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="kis_mock execution-event consumer")
    parser.add_argument(
        "mode",
        choices=["preflight", "run"],
        help="preflight: force dry-run reconcile; run: honor the env gate",
    )
    return parser


async def _amain(mode: str) -> int:
    # Lazy import so --help runs without Settings/secret env.
    from app.services.kis_mock_execution_consumer import KISMockExecutionConsumer

    consumer = KISMockExecutionConsumer(force_dry_run=(mode == "preflight"))
    await consumer.run()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
