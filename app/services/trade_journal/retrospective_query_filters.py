"""Shared SQL predicates for retrospective and canonical-action readers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.review import TradeRetrospective

VALID_OUTCOME_FILTERS: frozenset[str] = frozenset({"win", "loss", "decided"})
_KST = ZoneInfo("Asia/Seoul")


def kst_day_start(date_str: str) -> datetime:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(day.year, day.month, day.day, tzinfo=_KST)


def kst_day_end(date_str: str) -> datetime:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=_KST)


def sql_is_decided() -> ColumnElement[bool]:
    return or_(
        TradeRetrospective.realized_pnl.isnot(None),
        TradeRetrospective.pnl_pct.isnot(None),
    )


def sql_is_win() -> ColumnElement[bool]:
    return or_(
        TradeRetrospective.realized_pnl > 0,
        and_(
            TradeRetrospective.realized_pnl.is_(None),
            TradeRetrospective.pnl_pct > 0,
        ),
    )


def sql_is_loss() -> ColumnElement[bool]:
    return or_(
        and_(
            TradeRetrospective.realized_pnl.isnot(None),
            TradeRetrospective.realized_pnl <= 0,
        ),
        and_(
            TradeRetrospective.realized_pnl.is_(None),
            TradeRetrospective.pnl_pct.isnot(None),
            TradeRetrospective.pnl_pct <= 0,
        ),
    )


def outcome_filter_predicate(value: str) -> ColumnElement[bool]:
    if value == "win":
        return sql_is_win()
    if value == "loss":
        return sql_is_loss()
    if value == "decided":
        return sql_is_decided()
    raise ValueError(
        f"invalid outcome_filter: {value} (allowed: {sorted(VALID_OUTCOME_FILTERS)})"
    )
