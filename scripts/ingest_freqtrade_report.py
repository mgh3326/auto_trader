#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception, init_sentry
from app.services.research_ingestion_service import ingest_summary_payload

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest normalized freqtrade summary")
    parser.add_argument("--input", required=True, help="Path to summary JSON file")
    parser.add_argument("--runner", default="mac", help="Runner identity (mac|pi)")
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--minimum-trade-count", type=int, default=20)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.2)
    parser.add_argument("--maximum-drawdown", type=float, default=0.25)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("summary payload must be a JSON object")
    return parsed


async def ingest_file(
    input_path: Path,
    *,
    gate_config: dict[str, Any],
    runner: str,
    idempotency_key: str | None = None,
) -> str:
    payload = _load_json(input_path)
    async with AsyncSessionLocal() as session:
        return await ingest_summary_payload(
            session,
            payload,
            gate_config=gate_config,
            source_file=str(input_path),
            runner=runner,
            idempotency_key=idempotency_key,
        )


async def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="research-ingestion")

    gate_config = {
        "minimum_trade_count": args.minimum_trade_count,
        "minimum_profit_factor": args.minimum_profit_factor,
        "maximum_drawdown": args.maximum_drawdown,
    }

    input_path = Path(args.input)
    try:
        run_id = await ingest_file(
            input_path,
            gate_config=gate_config,
            runner=args.runner,
            idempotency_key=args.idempotency_key,
        )
    except Exception as exc:
        capture_exception(exc, process="ingest_freqtrade_report")
        logger.error("Research summary ingestion failed: %s", exc, exc_info=True)
        return 1

    logger.info("Research summary ingested: run_id=%s input=%s", run_id, input_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
