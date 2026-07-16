"""Manual/single-shot CLI runner for the Toss fill-to-ledger reconcile poller.

ROB-925: Toss has no broker websocket, so a filled order only lands in
``execution_ledger`` when something calls ``toss_reconcile_orders`` (MCP) or
the equivalent service kernel. The only in-repo automated path today is the
TaskIQ task ``toss_live.poll_fills_periodic``
(``app/tasks/toss_live_reconcile_tasks.py``), which requires the TaskIQ
worker + scheduler to be running. This script wraps the *same* kernels
(``TossFillPollerService.discover_external_orders`` +
``toss_reconcile_orders_impl``) as a standalone process so an operator can
run manual reps now, and promote it to launchd later (see
``docs/runbooks/toss-fill-reconcile-poller.md``) without touching TaskIQ.

No new broker mutation path is introduced: this only ever calls the existing
read/reconcile kernels. Order place/modify/cancel code is not touched.

Two modes:

* Preview (default, no ``--commit``) — DB-only, zero broker calls. Lists the
  current open ``toss_live`` ledger rows that a real run would scan.
* ``--commit`` — runs the real discover + reconcile pass (broker calls,
  ledger writes), gated by ``TOSS_FILL_POLL_ENABLED`` and the KR/US market
  session window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from app.core.config import settings
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl
from app.services.brokers.toss import TossReadClient
from app.services.toss_fill_poller_service import TossFillPollerService
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService
from app.tasks.toss_live_reconcile_tasks import _toss_fill_poll_market_gate

logger = logging.getLogger(__name__)


async def _preview_targets(*, market: str | None) -> dict[str, Any]:
    """DB-only scan-target listing. Makes no broker calls."""
    async with _order_session_factory()() as db:
        rows = await TossLiveOrderLedgerService(db).list_open(
            market=market, limit=settings.TOSS_FILL_POLL_RECONCILE_LIMIT
        )
    targets = [
        {
            "ledger_id": row.id,
            "broker_order_id": row.broker_order_id,
            "client_order_id": row.client_order_id,
            "market": row.market,
            "symbol": row.symbol,
            "operation_kind": row.operation_kind,
            "status": row.status,
        }
        for row in rows
    ]
    return {"target_count": len(targets), "targets": targets}


async def _invalidate_sellable_cache(booked_symbols: list[str]) -> None:
    if not booked_symbols:
        return
    try:
        from app.services.toss_sellable_cache import get_shared_sellable_cache

        await get_shared_sellable_cache().invalidate_many(booked_symbols)
    except Exception as exc:  # noqa: BLE001 — reconcile result remains authoritative
        logger.warning(
            "Toss fill poller: sellable-cache invalidation failed symbols=%s: %s",
            booked_symbols,
            exc,
        )


async def _run_commit(*, market: str | None) -> dict[str, Any]:
    client = TossReadClient.from_settings()
    try:
        async with _order_session_factory()() as db:
            discover = await TossFillPollerService(
                db, client=client
            ).discover_external_orders(
                dry_run=False,
                lookback_days=settings.TOSS_FILL_POLL_LOOKBACK_DAYS,
                closed_page_cap=settings.TOSS_FILL_POLL_CLOSED_PAGE_CAP,
            )
        reconcile = await toss_reconcile_orders_impl(
            dry_run=False,
            market=market,
            limit=settings.TOSS_FILL_POLL_RECONCILE_LIMIT,
        )
        booked_symbols = sorted(
            {
                str(outcome["symbol"])
                for outcome in reconcile.get("reconciled", [])
                if outcome.get("action") == "booked" and outcome.get("symbol")
            }
        )
        await _invalidate_sellable_cache(booked_symbols)
        return {
            "discover": discover,
            "reconcile": reconcile,
            "booked_symbols": booked_symbols,
        }
    finally:
        await client.aclose()


async def run_poll(*, dry_run: bool, market: str | None = None) -> dict[str, Any]:
    """Single pass of the Toss fill-reconcile poller.

    Kill-switch (``TOSS_FILL_POLL_ENABLED``) and the market-session gate are
    both checked before any broker call — a disabled flag or an outside-hours
    gate makes this a zero-broker-call no-op regardless of ``dry_run``.
    """
    if not settings.TOSS_FILL_POLL_ENABLED:
        return {
            "status": "disabled",
            "message": "TOSS_FILL_POLL_ENABLED is False",
            "dry_run": dry_run,
        }

    gate = _toss_fill_poll_market_gate()
    if not gate["active"]:
        return {
            "status": "skipped",
            "message": "outside Toss fill poll market window",
            "gate": gate,
            "dry_run": dry_run,
        }

    if dry_run:
        preview = await _preview_targets(market=market)
        return {
            "status": "preview",
            "success": True,
            "dry_run": True,
            "gate": gate,
            **preview,
        }

    result = await _run_commit(market=market)
    return {
        "status": "ran",
        "success": True,
        "dry_run": False,
        "gate": gate,
        **result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual/single-shot runner for the Toss fill-to-ledger reconcile "
            "poller (ROB-925). Default is a DB-only preview; pass --commit to "
            "run the real discover+reconcile pass."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--market",
        choices=["kr", "us"],
        default=None,
        help="Optional market filter for the preview/reconcile scope.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    dry_run = not bool(args.commit)
    try:
        result = await run_poll(dry_run=dry_run, market=args.market)
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured CLI failure
        logger.exception("Toss fill poller run failed")
        print(
            json.dumps(
                {
                    "status": "error",
                    "success": False,
                    "dry_run": dry_run,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
