"""Read-only Alpaca capability probe for the canonical clean-account route.

This command only reads ``/v2/assets``. Its output is capability evidence, not
an order allowlist; survivor-universe approval remains a separate operator
decision. It never imports an order submitter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

_CANDIDATES = ("BTC/USD", "ETH/USD", "SOL/USD")


async def _probe() -> dict[str, object]:
    service = AlpacaPaperBrokerService(profile="clean")
    assets = await service.list_assets(status="active", asset_class="crypto")
    available = {asset.symbol.upper() for asset in assets}
    return {
        "manifest": "alpaca-paper-spot-capability",
        "version": datetime.now(UTC).strftime("%Y-%m-%d.v1"),
        "provider": "alpaca_paper",
        "read_only_probe": True,
        "authorization": {
            "orders_allowed_by_manifest": False,
            "exact_survivor_universe_approval_required": True,
        },
        "candidate_symbols": list(_CANDIDATES),
        "provider_supported_symbols": [
            symbol for symbol in _CANDIDATES if symbol in available
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--read-only",
        action="store_true",
        required=True,
        help="required acknowledgement; this command has no mutation path",
    )
    args = parser.parse_args()
    if not args.read_only:  # pragma: no cover - argparse enforces this
        raise SystemExit("read-only acknowledgement required")
    print(json.dumps(asyncio.run(_probe()), sort_keys=True))


if __name__ == "__main__":
    main()
