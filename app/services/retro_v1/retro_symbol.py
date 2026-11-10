"""``retro_symbol`` — one symbol's chain and lot events over a KST date range."""

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
    check_span,
    check_symbol,
    decode_cursor,
    kst_window,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
)
from app.services.retro_v1.market import infer_market, normalize_symbol


async def read_retro_symbol(
    session: AsyncSession,
    *,
    symbol: str,
    to: Any,
    from_: Any = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Contract ``retro_symbol``; ``from`` is ``from_`` or the literal ``from`` key."""
    start = resolve_from(from_, extra)
    end = parse_date(to)
    check_span(start, end)
    check_limit(limit)
    clean = check_symbol(symbol)
    info = infer_market(clean)
    normalized = normalize_symbol(clean, info)
    digest = params_digest(op="retro_symbol", symbol=normalized, start=start, end=end)
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(start, end)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    async with read_snapshot(session):
        data = await load_chain(
            session,
            unknowns,
            info,
            symbols=[normalized],
            lo=lo,
            hi=hi,
            include_lots=True,
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
