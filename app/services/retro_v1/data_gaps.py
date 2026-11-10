"""``data_gaps`` — what is missing or stale, from the checks that already exist.

No new probes: each signal below is an existing read-only assessment, reused as
is. A datum that is fresh produces no row; a gap row says which dataset, when it
last updated and why it counts as a gap. A signal that itself failed to read is
an ``unknowns`` entry, never "no gap".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.execution_ledger.kis_lots import compute_freshness
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.krx_after_market_health import load_krx_after_market_health
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    Row,
    Unknowns,
    build_response,
    check_limit,
    decode_cursor,
    guarded,
    iso,
    latest,
    params_digest,
    read_snapshot,
)
from app.services.trade_journal.negative_class import load_negative_class_health

NEGATIVE_CLASS_MARKETS = ("kr", "us", "crypto")
_HEALTH_REASON = {
    "missing": "missing_data",
    "stale": "stale",
    "unknown": "incomplete_evidence",
}


def _parse(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def gap_row(
    *,
    dataset: str,
    symbol: str | None,
    last_updated_at: datetime | None,
    source: str | None,
    venue: str | None,
    missing_reason: str,
    basis: str = "inference",
) -> Row:
    # ``adjusted`` has no meaning for these datasets: null, not "false".
    hints = {"adjusted": "not_applicable"}
    if symbol is None:
        hints["symbol"] = "not_applicable"
    if venue is None:
        hints["venue"] = "not_applicable"
    if source is None:
        hints["source"] = "not_applicable"
    row = {
        "dataset": dataset,
        "symbol": symbol,
        "last_updated_at": iso(last_updated_at),
        "source": source,
        "venue": venue,
        "adjusted": None,
        "missing_reason": missing_reason,
        "basis": basis,
    }
    return row, hints


def krx_after_market_gap(health: dict[str, Any]) -> tuple[Row | None, datetime | None]:
    """From ``assess_krx_after_market_health``: only a non-fresh list is a gap."""
    newest = _parse(health.get("newest_asof"))
    status = str(health.get("status"))
    if status == "fresh":
        return None, newest
    return (
        gap_row(
            dataset="krx_after_market_eligibility",
            symbol=None,
            last_updated_at=newest,
            source="operator_import",
            venue="KRX",
            missing_reason=_HEALTH_REASON.get(status, "incomplete_evidence"),
        ),
        newest,
    )


def reconcile_gaps(
    latest_runs: dict[str, Any], now: datetime
) -> tuple[list[Row], datetime | None]:
    """KIS reconcile freshness (the 90-minute rule of ``kis_lots``).

    Only KIS has a freshness rule in code; another broker's run is neither
    judged stale nor reported, because no existing check defines "stale" for it.
    """
    run = latest_runs.get("kis")
    freshness = compute_freshness(run.finished_at if run else None, now)
    if freshness.state == "fresh":
        return [], freshness.last_reconcile_finished_at
    reason = "missing_data" if freshness.state == "missing" else "stale"
    return (
        [
            gap_row(
                dataset="execution_ledger_reconcile",
                symbol=None,
                last_updated_at=freshness.last_reconcile_finished_at,
                source="kis",
                venue=None,
                missing_reason=reason,
            )
        ],
        freshness.last_reconcile_finished_at,
    )


def negative_class_gap(health: dict[str, Any]) -> tuple[Row | None, datetime | None]:
    status = str(health.get("status"))
    last = _parse(health.get("last_recorded_at"))
    if status == "ok":
        return None, last
    reason = "missing_data" if status == "never_recorded" else "stale"
    return (
        gap_row(
            dataset=f"negative_class_recording.{health.get('market')}",
            symbol=None,
            last_updated_at=last,
            source=str(health.get("last_source"))
            if health.get("last_source")
            else None,
            venue=None,
            missing_reason=reason,
        ),
        last,
    )


async def read_data_gaps(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    digest = params_digest(op="data_gaps")
    offset = decode_cursor(cursor, digest)
    as_of = datetime.now(UTC)
    unknowns = Unknowns()
    rows: list[Row] = []
    seen: list[datetime | None] = []
    signals_failed = False

    async def add(
        field: str,
        read: Callable[[], Awaitable[Any]],
        project: Callable[[Any], tuple[list[Row], datetime | None]],
    ) -> None:
        nonlocal signals_failed
        value, ok = await guarded(session, unknowns, field, read, None)
        if not ok:
            signals_failed = True
            return
        found, newest = project(value)
        rows.extend(found)
        seen.append(newest)

    async with read_snapshot(session):

        async def krx() -> Any:
            return await load_krx_after_market_health(session, now=as_of)

        await add(
            "source.krx_after_market_eligibility",
            krx,
            lambda h: _one(krx_after_market_gap(h)),
        )

        async def runs() -> Any:
            return await ExecutionLedgerRepository(session).latest_run_per_broker()

        await add(
            "source.execution_ledger_reconcile",
            runs,
            lambda r: reconcile_gaps(r, as_of),
        )
        for market in NEGATIVE_CLASS_MARKETS:

            async def negative(market: str = market) -> Any:
                health = await load_negative_class_health(
                    session, market=market, now=as_of
                )
                return health.to_dict()

            await add(
                f"source.negative_class_recording.{market}",
                negative,
                lambda h: _one(negative_class_gap(h)),
            )

    # A failed signal means the gap list is incomplete, so no total is claimed.
    total = None if signals_failed else len(rows)
    return build_response(
        as_of=as_of,
        data_as_of=latest(seen),
        rows=rows,
        total=total,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )


def _one(pair: tuple[Row | None, datetime | None]) -> tuple[list[Row], datetime | None]:
    row, newest = pair
    return ([row] if row is not None else []), newest
