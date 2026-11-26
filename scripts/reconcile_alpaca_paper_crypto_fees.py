"""Preview or record linked Alpaca paper crypto fee activities; never submits orders."""

from __future__ import annotations

import argparse
import asyncio
import json


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--commit", action="store_true", help="persist fee evidence")
    result.add_argument("--limit", type=int, default=100)
    return result


async def run(*, commit: bool, limit: int) -> dict:
    from app.core.db import AsyncSessionLocal
    from app.services.alpaca_paper_account_modes import (
        ALPACA_PAPER_CRYPTO_ACCOUNT_MODE,
    )
    from app.services.alpaca_paper_crypto_fee_reconcile import (
        AlpacaPaperCryptoFeeReconcileService,
    )
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
    from app.services.brokers.alpaca.config import AlpacaPaperSettings
    from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    settings = AlpacaPaperSettings.from_app_settings(profile="clean")
    if (
        not settings.expected_account_id_suffix
        or not settings.expected_account_number_suffix
    ):
        raise ValueError("clean Alpaca expected account identity required")
    broker = AlpacaPaperBrokerService(settings=settings, profile="clean")
    try:
        account = await broker.get_account()
        if not (
            account.id.endswith(settings.expected_account_id_suffix)
            and account.account_number
            and account.account_number.endswith(settings.expected_account_number_suffix)
        ):
            raise ValueError("clean Alpaca account identity mismatch")
        async with AsyncSessionLocal() as db:
            ledger = AlpacaPaperLedgerService(
                db, account_mode=ALPACA_PAPER_CRYPTO_ACCOUNT_MODE
            )
            return await AlpacaPaperCryptoFeeReconcileService(ledger, broker).reconcile(
                commit=commit, limit=limit
            )
    finally:
        await broker.aclose()


def main() -> None:
    args = parser().parse_args()
    print(
        json.dumps(
            asyncio.run(run(commit=args.commit, limit=args.limit)), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
