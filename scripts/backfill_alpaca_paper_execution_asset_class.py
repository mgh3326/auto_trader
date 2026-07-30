"""ROB-1152: service-layer backfill for alpaca_paper_order_ledger.execution_asset_class.

AGENTS.md #5 requires ledger writes to go through the service layer, never a
bare SQL string. This script is the operator entry point that calls
``AlpacaPaperLedgerService.backfill_execution_asset_class_from_instrument_type``
(ORM ``update(...).values(...)``, no raw SQL) instead of a migration
``op.execute("UPDATE ...")``.

Derivation basis (ROB-1152 investigation, 2026-07-29): every row's
``instrument_type`` (NOT NULL, DB enum) is verified 1:1 with
``execution_asset_class`` across all 38 existing rows with 0 exceptions, and
this mapping is already enforced as an invariant in
``app/services/paper_approval_packet.py`` (``expected_instrument_type``).
Only rows whose ``instrument_type`` is 'crypto' or 'equity_us' are touched;
any other value is left NULL and reported, never guessed.

Modes:
  Dry-run (default, no DB write):
      uv run python scripts/backfill_alpaca_paper_execution_asset_class.py
    Prints the row counts that WOULD be updated, grouped by target
    execution_asset_class, plus any NULL rows left untouched because their
    instrument_type falls outside the verified mapping.

  Apply (BOTH gates required, commits the update):
      ROB1152_BACKFILL_ALLOW_WRITE=1 \\
          uv run python scripts/backfill_alpaca_paper_execution_asset_class.py \\
          --confirm-backfill
    Calls the service with dry_run=False, which commits exactly one UPDATE
    per instrument_type via the ORM (still routed through
    AlpacaPaperLedgerService, never a direct connection.execute(text(...))).

Either gate alone is rejected. This script performs no migration, no schema
change, and is not registered with any scheduler (AGENTS.md #6) -- an
operator runs it manually and reviews the printed counts before rerunning
with --confirm-backfill.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.services.alpaca_paper_ledger_service import (  # noqa: E402
    AlpacaPaperLedgerService,
)

ENV_GATE = "ROB1152_BACKFILL_ALLOW_WRITE"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-backfill",
        action="store_true",
        help=f"Apply the backfill (also requires {ENV_GATE}=1 in the environment).",
    )
    return parser.parse_args()


def _both_gates_set(args: argparse.Namespace) -> bool:
    return bool(args.confirm_backfill) and os.environ.get(ENV_GATE) == "1"


async def _run(*, dry_run: bool) -> dict[str, object]:
    async with AsyncSessionLocal() as db:
        # account_mode is irrelevant to this backfill (it spans every
        # account_mode sharing this table); the service instance's own
        # account_mode scoping does not apply to this cross-account method.
        service = AlpacaPaperLedgerService(db)
        return await service.backfill_execution_asset_class_from_instrument_type(
            dry_run=dry_run
        )


def main() -> int:
    args = _parse_args()
    env_gate_set = os.environ.get(ENV_GATE) == "1"
    if args.confirm_backfill != env_gate_set:
        print(
            f"Refusing: both --confirm-backfill AND {ENV_GATE}=1 are required "
            "to apply the backfill. Running in dry-run mode instead.",
            file=sys.stderr,
        )

    dry_run = not _both_gates_set(args)
    result = asyncio.run(_run(dry_run=dry_run))

    print(f"mode: {'DRY-RUN (no write)' if dry_run else 'APPLIED (committed)'}")
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
