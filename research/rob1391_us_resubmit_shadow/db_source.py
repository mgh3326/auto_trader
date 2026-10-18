"""Parent source rows from the production database -- SELECT only.

Every US BUY path that reaches a real broker writes one of these ledgers:

* ``review.toss_live_order_ledger``  -- Toss live US (``market='us'``)
* ``review.live_order_ledger``       -- KIS live US (ROB-407,
  ``broker='kis' AND account_scope='kis_live' AND market='us'``)
* ``review.kis_live_order_ledger``   -- KR-domestic ledger; ``equity_us`` rows
  must not exist by contract. They are read only so that an unexpected one is
  surfaced as an exclusion instead of being invisible.

Approved budgets come from ``review.order_proposal_rungs`` joined to
``review.order_proposals`` (``market='equity_us'``) by broker order id; listing
facts from ``us_symbol_universe``.

Safety (prereg §4, brief §격리):

* the DSN is read from the one environment variable named by
  ``--database-url-env``; its value is never printed or chained into errors;
* the connection opens ``REPEATABLE READ, READ ONLY`` before any query, so a
  write would be rejected by PostgreSQL itself;
* ``_assert_select`` refuses any statement that is not a single SELECT/WITH
  without a write keyword, before it is sent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|TRUNCATE|ALTER|DROP|CREATE|GRANT|REVOKE|"
    r"COPY|CALL|DO|LOCK|VACUUM|REINDEX|CLUSTER|COMMENT|SECURITY|REFRESH|NOTIFY|"
    r"PREPARE|EXECUTE|SET\s+ROLE|FOR\s+UPDATE|FOR\s+SHARE|FOR\s+NO\s+KEY|FOR\s+KEY|INTO|"
    r"NEXTVAL|SETVAL|SET_CONFIG|PG_\w+|DBLINK\w*|LO_\w+|TXID_\w+|CURRVAL)\b",
    re.IGNORECASE,
)

READ_ONLY_BEGIN = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

SQL_TOSS_US_BUYS = """
SELECT 'toss_live_order_ledger' AS source, t.id::text AS source_id,
       t.broker_order_id, t.symbol, t.market, t.side, t.order_type,
       t.operation_kind, t.time_in_force, t.quantity, t.price, t.currency,
       t.status, t.requires_manual_review, t.trade_date AS ordered_at,
       t.expired_at, t.filled_qty, t.avg_fill_price, NULL::text AS exchange
FROM review.toss_live_order_ledger AS t
WHERE t.market = 'us' AND t.side = 'buy' AND t.trade_date >= :since
"""

SQL_KIS_US_BUYS = """
SELECT 'live_order_ledger' AS source, l.id::text AS source_id,
       l.order_no AS broker_order_id, l.symbol, l.market, l.side,
       l.order_kind AS order_type, 'place' AS operation_kind,
       NULL::text AS time_in_force, l.quantity, l.price, l.currency, l.status,
       false AS requires_manual_review, l.trade_date AS ordered_at,
       NULL::timestamptz AS expired_at, l.filled_qty, l.avg_fill_price, l.exchange
FROM review.live_order_ledger AS l
WHERE l.broker = 'kis' AND l.account_scope = 'kis_live' AND l.market = 'us'
  AND l.side = 'buy' AND l.trade_date >= :since
"""

SQL_KIS_KR_TABLE_US_BUYS = """
SELECT 'kis_live_order_ledger' AS source, k.id::text AS source_id,
       k.order_no AS broker_order_id, k.symbol, 'us' AS market, k.side,
       k.order_type, 'place' AS operation_kind, NULL::text AS time_in_force,
       k.quantity, k.price, k.currency, k.status, false AS requires_manual_review,
       k.trade_date AS ordered_at, NULL::timestamptz AS expired_at,
       k.filled_qty, k.avg_fill_price, NULL::text AS exchange
FROM review.kis_live_order_ledger AS k
WHERE k.instrument_type = 'equity_us' AND k.side = 'buy' AND k.trade_date >= :since
"""

SQL_US_BUY_RUNGS = """
SELECT r.broker_order_id, r.rung_index, r.quantity AS rung_quantity,
       r.limit_price AS rung_limit_price, r.notional AS rung_notional,
       p.root_proposal_id::text AS proposal_root, p.account_mode,
       p.source_asof -> 'auto_approved' ->> 'policy_version' AS order_policy_version
FROM review.order_proposal_rungs AS r
JOIN review.order_proposals AS p ON p.id = r.proposal_pk
WHERE p.market = 'equity_us' AND r.side = 'buy'
  AND r.broker_order_id IS NOT NULL AND r.created_at >= :since
"""

SQL_US_UNIVERSE = """
SELECT u.symbol, u.exchange, u.name_en, u.is_active, u.is_common_stock,
       u.security_type, u.is_common_share, u.listing_status, u.leverage_factor
FROM us_symbol_universe AS u
WHERE u.symbol = ANY(:symbols)
"""

ORDER_QUERIES = (SQL_TOSS_US_BUYS, SQL_KIS_US_BUYS, SQL_KIS_KR_TABLE_US_BUYS)
ALL_QUERIES = (*ORDER_QUERIES, SQL_US_BUY_RUNGS, SQL_US_UNIVERSE)


class ReadOnlyViolation(RuntimeError):
    pass


def _assert_select(sql: str) -> str:
    body = sql.strip()
    if ";" in body.rstrip(";"):
        raise ReadOnlyViolation("multiple statements are not allowed")
    if not re.match(r"^(SELECT|WITH)\b", body, re.IGNORECASE):
        raise ReadOnlyViolation("only SELECT/WITH statements are allowed")
    if _WRITE_KEYWORDS.search(body):
        raise ReadOnlyViolation("statement contains a write keyword")
    return body


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    order_rows: list[dict[str, Any]]
    rung_rows: list[dict[str, Any]]
    universe_rows: list[dict[str, Any]]


class ParentSource(Protocol):
    def fetch(self, since: datetime) -> SourceSnapshot: ...


class Connection(Protocol):
    """The tiny surface ``DbParentSource`` needs (a SQLAlchemy Connection)."""

    def exec_driver_sql(self, sql: str) -> Any: ...

    def execute(self, statement: Any, params: dict[str, Any]) -> Any: ...


def database_url_from_env(env_name: str) -> str:
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise ValueError("--database-url-env must be an environment variable name")
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise ValueError(f"environment variable {env_name} is not set or empty")
    from sqlalchemy.engine import make_url

    try:
        url = make_url(raw.strip())
    except Exception:
        raise ValueError(
            f"environment variable {env_name} is not a database URL"
        ) from None
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError(f"environment variable {env_name} is not a PostgreSQL URL")
    return url.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


def run_read_only(conn: Connection, since: datetime) -> SourceSnapshot:
    """Run every query inside one read-only snapshot on ``conn``."""

    from sqlalchemy import text

    conn.exec_driver_sql(READ_ONLY_BEGIN)

    def select(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        result = conn.execute(text(_assert_select(sql)), params)
        return [dict(row._mapping) for row in result]

    orders: list[dict[str, Any]] = []
    for sql in ORDER_QUERIES:
        orders.extend(select(sql, {"since": since}))
    rungs = select(SQL_US_BUY_RUNGS, {"since": since})
    symbols = sorted(
        {str(r["symbol"]).strip().upper() for r in orders if r.get("symbol")}
    )
    universe = select(SQL_US_UNIVERSE, {"symbols": symbols}) if symbols else []
    return SourceSnapshot(order_rows=orders, rung_rows=rungs, universe_rows=universe)


class DbParentSource:
    def __init__(self, env_name: str) -> None:
        self._env_name = env_name

    def fetch(self, since: datetime) -> SourceSnapshot:
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine

        url = database_url_from_env(self._env_name)

        async def _fetch() -> SourceSnapshot:
            engine = create_async_engine(url, pool_pre_ping=False)
            try:
                async with engine.connect() as conn:
                    transaction = await conn.begin()
                    try:
                        return await conn.run_sync(run_read_only, since)
                    finally:
                        # Nothing to keep: the transaction is READ ONLY.
                        await transaction.rollback()
            finally:
                await engine.dispose()

        return asyncio.run(_fetch())


class FileParentSource:
    """Offline source: a JSON file with the three row lists (replays, tests)."""

    def __init__(self, path: Any) -> None:
        self._path = path

    def fetch(self, since: datetime) -> SourceSnapshot:
        import json
        from pathlib import Path

        raw = json.loads(Path(self._path).read_text(encoding="utf-8"))
        orders = [
            r
            for r in raw["order_rows"]
            if _ts(r.get("ordered_at")) is None or _ts(r["ordered_at"]) >= since
        ]
        return SourceSnapshot(
            order_rows=orders,
            rung_rows=list(raw.get("rung_rows", [])),
            universe_rows=list(raw.get("universe_rows", [])),
        )


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return out if out.tzinfo else None
