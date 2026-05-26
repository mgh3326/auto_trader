"""US dual-paper premarket preview smoke (ROB-326). Default-disabled, read-only.

Never prints secret values — only env key NAMES on missing creds.
Exit codes: 0 success / disabled no-op; 1 config or credential problem;
2 operational/runtime failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter
from app.services.us_dual_paper.capability_matrix import get_capability_matrix


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def _run_preflight() -> int:
    _emit({"step": "capability_matrix", "matrix": get_capability_matrix()})
    any_missing = False
    for adapter in _adapters():
        missing = adapter.missing_env_keys()
        enabled = adapter.is_enabled()
        any_missing = any_missing or not enabled
        _emit(
            {
                "step": "broker_preflight",
                "account_scope": adapter.account_scope,
                "enabled": enabled,
                "missing_env_keys": missing,  # NAMES only, never values
            }
        )
    return 1 if any_missing else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="US dual-paper premarket preview smoke"
    )
    parser.add_argument(
        "--mode", required=True, choices=["preflight"]
    )  # 'preview' added in PR2
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not _truthy(os.environ.get("US_DUAL_PAPER_PREVIEW_ENABLED")):
        _emit(
            {
                "step": "disabled",
                "hint": "set US_DUAL_PAPER_PREVIEW_ENABLED=true to opt in",
            }
        )
        return 0
    try:
        if args.mode == "preflight":
            return asyncio.run(_run_preflight())
        return 2
    except Exception as exc:  # noqa: BLE001
        _emit({"step": "error", "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    sys.exit(main())
