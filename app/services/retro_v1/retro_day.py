"""``retro_day`` — one market's chain for one KST trading date."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retro_v1.chain import build_chain, data_as_of, load_chain
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    Unknowns,
    build_response,
    check_limit,
    decode_cursor,
    kst_window,
    params_digest,
    parse_date,
    read_snapshot,
)
from app.services.retro_v1.market import resolve_market


async def read_retro_day(
    session: AsyncSession,
    *,
    market: str,
    date: Any,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    info = resolve_market(market)
    day = parse_date(date)
    digest = params_digest(op="retro_day", market=info.key, day=day)
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(day, day)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    async with read_snapshot(session):
        data = await load_chain(
            session, unknowns, info, symbols=None, lo=lo, hi=hi, include_lots=True
        )
    rows = build_chain(data, info, include_lots=True)
    return build_response(
        as_of=as_of,
        data_as_of=data_as_of(data),
        rows=rows,
        total=None if data.incomplete else len(rows),
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
