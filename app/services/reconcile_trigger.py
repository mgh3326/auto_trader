"""Reconnect-driven reconcile trigger (fillwire P0, spec §3 P0).

When the websocket tap reconnects it may have missed fills while the socket
was down. This module is the thin, *dedupe-guarded* front door that asks an
existing reconcile kernel to backfill that gap. It adds no reconcile or
booking logic of its own — KR goes to ``kis_live_reconcile_orders_impl``,
US and crypto go to ``live_reconcile_orders_impl`` — and it keeps the
kernels' ``dry_run`` default and fill-evidence gates untouched.

Dedupe is deliberately process-local: a bounded ``{market: started_at}`` map
on a monotonic clock, no schema, no Redis, no scheduler. A reconnect storm
(the failure this guards against) happens inside one monitor process, and a
missed dedupe is merely a second idempotent reconcile, never a double booking.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MARKETS: tuple[str, ...] = ("kr", "us", "crypto")

#: Response/message text we echo back is truncated to this many characters so
#: a broker or driver string can never become an unbounded response body.
MAX_MESSAGE_CHARS = 300


def _truncate(value: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def count_backfilled(kernel_result: Any, *, dry_run: bool) -> int:
    """Count fills the kernel actually *booked* in this run.

    Both kernels label a committed booking with an ``action`` of ``booked``
    (US/crypto) or ``booked_filled``/``booked_partial`` (KR); a dry run only
    ever produces ``would_book*``. A dry run therefore reports ``0`` — an
    uncommitted plan must never be reported as backfilled.
    """
    if dry_run or not isinstance(kernel_result, dict):
        return 0
    if kernel_result.get("success") is not True:
        return 0
    rows = kernel_result.get("reconciled")
    if not isinstance(rows, list):
        return 0
    booked = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = row.get("action")
        if isinstance(action, str) and action.startswith("booked"):
            booked += 1
    return booked


def project_kernel_result(kernel_result: Any) -> dict[str, Any] | None:
    """Bounded, diagnosable projection of a kernel result.

    Keeps the verdict counts and coverage a reconnect operator needs while
    dropping the unbounded per-row ``reconciled`` list (which carries symbols,
    journal ids and broker strings) and truncating free text.
    """
    if not isinstance(kernel_result, dict):
        return None
    projection: dict[str, Any] = {
        "success": bool(kernel_result.get("success")),
        "dry_run": bool(kernel_result.get("dry_run")),
        "reconciled_rows": len(kernel_result.get("reconciled") or []),
    }
    counts = kernel_result.get("counts")
    if isinstance(counts, dict):
        projection["counts"] = {str(k): int(v) for k, v in counts.items()}
    scan = kernel_result.get("candidate_scan")
    if isinstance(scan, dict):
        projection["candidate_scan"] = scan
    if kernel_result.get("message") is not None:
        projection["message"] = _truncate(kernel_result.get("message"))
    if kernel_result.get("error") is not None:
        projection["error"] = _truncate(kernel_result.get("error"))
    return projection


async def _run_kr(*, dry_run: bool) -> dict[str, Any]:
    from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl

    return await kis_live_reconcile_orders_impl(dry_run=dry_run)


async def _run_us(*, dry_run: bool) -> dict[str, Any]:
    from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

    return await live_reconcile_orders_impl(market="us", broker="kis", dry_run=dry_run)


async def _run_crypto(*, dry_run: bool) -> dict[str, Any]:
    from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

    return await live_reconcile_orders_impl(
        market="crypto", broker="upbit", dry_run=dry_run
    )


#: market -> existing reconcile kernel. Nothing else may be dispatched here.
KERNELS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "kr": _run_kr,
    "us": _run_us,
    "crypto": _run_crypto,
}


@dataclass(frozen=True)
class ReconcileTriggerOutcome:
    market: str
    dry_run: bool
    status: str
    deduped: bool
    backfilled: int
    kernel: dict[str, Any] | None = None
    error: str | None = None


class ReconcileTriggerCoordinator:
    """Serializes reconnect reconciles per market inside one process."""

    def __init__(
        self,
        *,
        window_seconds: float,
        clock: Callable[[], float] | None = None,
        kernels: dict[str, Callable[..., Awaitable[dict[str, Any]]]] | None = None,
    ) -> None:
        self._window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._kernels = kernels or KERNELS
        # Bounded by construction: at most one entry per known market.
        self._started_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    async def _claim(self, market: str) -> bool:
        """Atomically claim the market for a run, or report a dedupe hit.

        The stamp is taken at *start*, so concurrent callers can never both
        enter the kernel: whoever loses the lock sees a fresh stamp.
        """
        async with self._lock:
            now = self._clock()
            previous = self._started_at.get(market)
            if previous is not None and (now - previous) < self._window_seconds:
                return False
            self._started_at[market] = now
            # Drop stamps that can no longer dedupe anything.
            for key in [
                k
                for k, stamp in self._started_at.items()
                if (now - stamp) >= self._window_seconds
            ]:
                self._started_at.pop(key, None)
            return True

    async def trigger(
        self, *, market: str, dry_run: bool = True, reason: str = "reconnect"
    ) -> ReconcileTriggerOutcome:
        kernel = self._kernels.get(market)
        if kernel is None:
            return ReconcileTriggerOutcome(
                market=market,
                dry_run=dry_run,
                status="failed",
                deduped=False,
                backfilled=0,
                error="unknown_market",
            )

        if not await self._claim(market):
            logger.info(
                "Reconcile trigger deduped: market=%s reason=%s window_s=%s",
                market,
                reason,
                self._window_seconds,
            )
            return ReconcileTriggerOutcome(
                market=market,
                dry_run=dry_run,
                status="deduped",
                deduped=True,
                backfilled=0,
            )

        try:
            result = await kernel(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - surfaced as a bounded error
            logger.exception("Reconcile trigger failed: market=%s", market)
            return ReconcileTriggerOutcome(
                market=market,
                dry_run=dry_run,
                status="failed",
                deduped=False,
                backfilled=0,
                error=_truncate(exc.__class__.__name__),
            )

        backfilled = count_backfilled(result, dry_run=dry_run)
        projection = project_kernel_result(result)
        succeeded = bool(isinstance(result, dict) and result.get("success"))
        logger.info(
            "Reconcile trigger completed: market=%s dry_run=%s reason=%s "
            "success=%s backfilled=%s",
            market,
            dry_run,
            reason,
            succeeded,
            backfilled,
        )
        return ReconcileTriggerOutcome(
            market=market,
            dry_run=dry_run,
            status="executed" if succeeded else "failed",
            deduped=False,
            backfilled=backfilled,
            kernel=projection,
            error=None if succeeded else (projection or {}).get("error"),
        )


_coordinator: ReconcileTriggerCoordinator | None = None


def get_reconcile_trigger_coordinator() -> ReconcileTriggerCoordinator:
    """Process-wide coordinator so the dedupe window actually dedupes."""
    global _coordinator
    if _coordinator is None:
        from app.core.config import settings

        _coordinator = ReconcileTriggerCoordinator(
            window_seconds=settings.EXECUTION_LEDGER_RECONCILE_TRIGGER_DEDUPE_SECONDS
        )
    return _coordinator


def reset_reconcile_trigger_coordinator() -> None:
    """Test seam: forget the process-wide coordinator."""
    global _coordinator
    _coordinator = None
