"""``retro_funnel`` — per-stage counts and denominators for one cohort.

Stages are population counts over the same cohort (one market, one KST date
range). They are not strict descendants of each other (the candidate→proposal
link is inferred, see ``chain``), so every row is ``basis=inference``. A rate is
only stated when its numerator and denominator were both counted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.execution_ledger.supersede import normalize_order_id
from app.services.retro_v1.chain import ChainData, data_as_of, load_chain
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_span,
    dec,
    decode_cursor,
    kst_window,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
)
from app.services.retro_v1.market import MarketInfo, resolve_market

_RATE_SCALE = Decimal("0.0001")


def _rate(count: int | None, denominator: int | None) -> Decimal | None:
    if count is None or not denominator:
        return None
    return (Decimal(count) / Decimal(denominator)).quantize(
        _RATE_SCALE, rounding=ROUND_HALF_EVEN
    )


def stage_counts(data: ChainData) -> list[tuple[str, int | None]]:
    """``(stage, count)``; ``None`` when that stage's source was not fully read."""
    inc = data.incomplete
    order_ids = {
        (p.symbol.upper(), normalize_order_id(r.broker_order_id))
        for p in data.proposals
        for r in data.rungs
        if r.proposal_pk == p.id and r.broker_order_id
    }
    filled = {
        (f.symbol.upper(), normalize_order_id(f.broker_order_id)) for f in data.fills
    } & order_ids

    def known(name: str, value: int, *depends: str) -> int | None:
        return None if inc & {name, *depends} else value

    return [
        ("candidate", known("candidates", len(data.candidates))),
        ("proposal", known("proposals", len(data.proposals))),
        ("rung", known("rungs", len(data.rungs), "proposals")),
        ("order", known("rungs", len(order_ids), "proposals")),
        ("fill", known("fills", len(filled), "rungs", "proposals")),
    ]


def funnel_rows(data: ChainData, cohort: str) -> list[Row]:
    counts = stage_counts(data)
    rows: list[Row] = []
    previous: int | None = None
    for index, (stage, count) in enumerate(counts):
        denominator = previous if index else None
        rate = _rate(count, denominator)
        if index == 0:
            reason, hint = None, "not_applicable"
        elif count is None or denominator is None:
            reason, hint = "incomplete_evidence", "incomplete_evidence"
        elif denominator == 0:
            reason, hint = "not_applicable", "not_applicable"
        else:
            reason, hint = None, "not_applicable"
        rows.append(
            (
                {
                    "stage": stage,
                    "cohort": cohort,
                    "count": count,
                    "denominator": denominator,
                    "fill_rate": dec(rate),
                    "unit": "events",
                    "basis": "inference",
                    "missing_reason": reason,
                },
                {
                    "denominator": hint,
                    "fill_rate": hint,
                    "count": "incomplete_evidence",
                },
            )
        )
        previous = count
    rejected = None if "forecasts" in data.incomplete else len(data.forecasts)
    rows.append(
        (
            {
                "stage": "rejected_forecast",
                "cohort": cohort,
                "count": rejected,
                "denominator": None,
                "fill_rate": None,
                "unit": "events",
                "basis": "inference",
                "missing_reason": None
                if rejected is not None
                else "incomplete_evidence",
            },
            {"denominator": "not_applicable", "fill_rate": "not_applicable"},
        )
    )
    return rows


def _cohort(info: MarketInfo, start: Any, end: Any) -> str:
    return f"{info.key}:{start.isoformat()}..{end.isoformat()}"


async def read_retro_funnel(
    session: AsyncSession,
    *,
    market: str,
    to: Any,
    from_: Any = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Contract ``retro_funnel``; ``from`` is ``from_`` or the literal ``from`` key."""
    start = resolve_from(from_, extra)
    end = parse_date(to)
    check_span(start, end)
    check_limit(limit)
    info = resolve_market(market)
    digest = params_digest(op="retro_funnel", market=info.key, start=start, end=end)
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(start, end)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    async with read_snapshot(session):
        data = await load_chain(
            session, unknowns, info, symbols=None, lo=lo, hi=hi, include_lots=False
        )
    rows = funnel_rows(data, _cohort(info, start, end))
    return build_response(
        as_of=as_of,
        data_as_of=data_as_of(data),
        rows=rows,
        total=len(rows),
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
