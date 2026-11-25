"""The only writer of the two Upbit intraday shadow tables (hk 6036).

``research.upbit_shadow_candidates`` and ``research.upbit_shadow_virtual_trades``
are insert-only: every insert is ``ON CONFLICT DO NOTHING``, so a re-run never
changes a row or moves a first-receipt time. Neither table is a fill ledger and
nothing here reads or writes one. The caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.upbit_intraday_shadow import contracts as K

_CANDIDATE_COLUMNS = (
    "rules_version", "rules_sha256", "market", "family", "config_id", "n_bars",
    "x_multiple", "direction", "selected_config", "signal_bar_open_at",
    "signal_time", "first_received_at", "disposition", "exclusion_reasons",
    "open_price", "high_price", "low_price", "close_price", "quote_volume",
    "ref_level", "breakout_distance", "median_quote_volume", "volume_ratio",
    "quote_volume_24h", "liquidity_bucket", "fee_rate_per_side",
    "roundtrip_cost_rate", "signal_tick_krw", "market_event_state",
    "market_event_caution_types", "market_event_observed_at", "run_id",
)  # fmt: skip

_TRADE_COLUMNS = (
    "candidate_id", "disposition", "horizon_bars", "entry_bar_open_at",
    "exit_bar_open_at", "entry_source", "virtual_entry_price", "exit_basis",
    "virtual_exit_price", "entry_tick_krw", "exit_tick_krw", "gross_return",
    "roundtrip_cost_rate", "net_return", "mae", "mfe", "missing_reason",
    "outcome_received_at", "run_id",
)  # fmt: skip


def _insert_sql(table: str, columns: Sequence[str], conflict: str) -> str:
    names = ", ".join(columns)
    values = ", ".join(f":{c}" for c in columns)
    return (
        f"INSERT INTO {table} ({names}) VALUES ({values}) "
        f"ON CONFLICT ({conflict}) DO NOTHING RETURNING id"
    )


INSERT_CANDIDATE_SQL = _insert_sql(
    "research.upbit_shadow_candidates",
    _CANDIDATE_COLUMNS,
    "rules_version, market, config_id, signal_bar_open_at",
)
INSERT_TRADE_SQL = _insert_sql(
    "research.upbit_shadow_virtual_trades",
    _TRADE_COLUMNS,
    "candidate_id, horizon_bars",
)

PENDING_SQL = """
SELECT c.id, c.market, c.config_id, c.direction, c.disposition,
       c.signal_bar_open_at, c.roundtrip_cost_rate,
       array_remove(array_agg(t.horizon_bars), NULL) AS resolved
FROM research.upbit_shadow_candidates AS c
LEFT JOIN research.upbit_shadow_virtual_trades AS t ON t.candidate_id = c.id
WHERE c.rules_version = :rules_version
GROUP BY c.id
HAVING count(t.horizon_bars) < :horizons
ORDER BY c.signal_bar_open_at, c.id
"""


async def insert_candidates(
    session: AsyncSession, candidates: Sequence[K.Candidate], *, run_id: str
) -> int:
    """Insert new rows; return how many were new."""
    statement = text(INSERT_CANDIDATE_SQL)
    inserted = 0
    for candidate in candidates:
        params = asdict(candidate) | {"run_id": run_id}
        params["exclusion_reasons"] = list(candidate.exclusion_reasons)
        params["market_event_caution_types"] = list(
            candidate.market_event_caution_types
        )
        result = await session.execute(statement, params)
        inserted += result.scalar_one_or_none() is not None
    return inserted


async def load_pending(
    session: AsyncSession, markets: Sequence[str] | None = None
) -> list[K.PendingCandidate]:
    """Recorded candidates that still lack a horizon row."""
    result = await session.execute(
        text(PENDING_SQL),
        {"rules_version": K.RULES_VERSION, "horizons": len(K.HORIZONS)},
    )
    wanted = None if markets is None else set(markets)
    return [
        K.PendingCandidate(
            candidate_id=row.id,
            market=row.market,
            config_id=row.config_id,
            direction=row.direction,
            disposition=row.disposition,
            signal_bar_open_at=row.signal_bar_open_at,
            roundtrip_cost_rate=row.roundtrip_cost_rate,
            resolved_horizons=frozenset(row.resolved),
        )
        for row in result
        if wanted is None or row.market in wanted
    ]


async def insert_virtual_trades(
    session: AsyncSession, trades: Sequence[K.VirtualTrade], *, run_id: str
) -> int:
    """Insert new virtual rows; return how many were new."""
    statement = text(INSERT_TRADE_SQL)
    inserted = 0
    for trade in trades:
        if trade.candidate_id is None:
            raise ValueError("a virtual trade needs a recorded candidate")
        params = asdict(trade) | {"run_id": run_id}
        result = await session.execute(
            statement, {c: params[c] for c in _TRADE_COLUMNS}
        )
        inserted += result.scalar_one_or_none() is not None
    return inserted
