"""ROB-1470 strategy sleeves: one strategy's money and results inside a real account.

A sleeve is identity only (rule, account mode, market, currency). Every amount,
the state and the ROB-1468 pre-registration live in append-only versions: the operator sets budget, loss limit
and state through ``scripts/strategy_sleeves.py``; the dispatch gate may append
exactly one kind of version itself, ``live -> stopped`` when the loss limit is
reached. Nothing in code or policy chooses an amount.

All writes go through ``app.services.strategy_sleeves.store``. The DB triggers
in ``alembic/versions/20261102_rob1470_strategy_sleeves.py`` refuse UPDATE,
DELETE and TRUNCATE on both tables and an out-of-sequence version number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base

SLEEVE_STATES: tuple[str, ...] = ("shadow", "live", "paused", "stopped")
SLEEVE_ACCOUNT_MARKETS: tuple[tuple[str, str], ...] = (
    ("kis_live", "equity_kr"),
    ("kis_live", "equity_us"),
    ("toss_live", "equity_kr"),
    ("toss_live", "equity_us"),
    ("upbit", "crypto"),
)
SLEEVE_CURRENCY_BY_MARKET: dict[str, str] = {
    "equity_kr": "KRW",
    "equity_us": "USD",
    "crypto": "KRW",
}

_STATES_SQL = ",".join(f"'{s}'" for s in SLEEVE_STATES)
ACCOUNT_MARKET_SQL = " OR ".join(
    f"(account_mode = '{mode}' AND market = '{market}')"
    for mode, market in SLEEVE_ACCOUNT_MARKETS
)
CURRENCY_SQL = " OR ".join(
    f"(market = '{market}' AND currency = '{currency}')"
    for market, currency in SLEEVE_CURRENCY_BY_MARKET.items()
)


class StrategySleeve(Base):
    """Immutable identity of one strategy's slice of one real account."""

    __tablename__ = "strategy_sleeves"
    __table_args__ = (
        UniqueConstraint("sleeve_key", name="uq_strategy_sleeves_sleeve_key"),
        CheckConstraint(ACCOUNT_MARKET_SQL, name="account_market"),
        CheckConstraint(CURRENCY_SQL, name="currency"),
        CheckConstraint(
            "sleeve_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'", name="sleeve_key_format"
        ),
        CheckConstraint(
            "char_length(btrim(rule_id)) BETWEEN 1 AND 128", name="rule_id_nonblank"
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="created_by_nonblank",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class StrategySleeveVersion(Base):
    """Append-only budget, loss limit and state history of one sleeve."""

    __tablename__ = "strategy_sleeve_versions"
    __table_args__ = (
        UniqueConstraint(
            "sleeve_id", "version", name="uq_strategy_sleeve_versions_sleeve_version"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(f"state IN ({_STATES_SQL})", name="state"),
        CheckConstraint("budget_cap >= 0", name="budget_cap_nonnegative"),
        CheckConstraint("loss_limit > 0", name="loss_limit_positive"),
        CheckConstraint(
            "prereg_metric IS NULL OR char_length(btrim(prereg_metric)) BETWEEN 1 AND 300",
            name="prereg_metric_nonblank",
        ),
        CheckConstraint(
            "prereg_min_effect_pct IS NULL OR prereg_min_effect_pct > 0",
            name="prereg_min_effect_positive",
        ),
        CheckConstraint(
            "prereg_horizon_sessions IS NULL OR prereg_horizon_sessions >= 1",
            name="prereg_horizon_positive",
        ),
        CheckConstraint(
            "prereg_max_duration_days IS NULL OR prereg_max_duration_days >= 1",
            name="prereg_max_duration_positive",
        ),
        CheckConstraint(
            "state <> 'live' OR (prereg_metric IS NOT NULL "
            "AND prereg_min_effect_pct IS NOT NULL "
            "AND prereg_horizon_sessions IS NOT NULL "
            "AND prereg_max_duration_days IS NOT NULL)",
            name="live_requires_prereg",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 500", name="reason_nonblank"
        ),
        CheckConstraint(
            "char_length(btrim(set_by)) BETWEEN 1 AND 128", name="set_by_nonblank"
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    budget_cap: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    loss_limit: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    # ROB-1468 pre-registration, set by the operator: the primary metric, the
    # minimum effect worth acting on (percentage points of the sleeve budget),
    # the fixed horizon H in sessions and the maximum duration in days.
    prereg_metric: Mapped[str | None] = mapped_column(Text)
    prereg_min_effect_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    prereg_horizon_sessions: Mapped[int | None] = mapped_column(Integer)
    prereg_max_duration_days: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    set_by: Mapped[str] = mapped_column(Text, nullable=False)
    set_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
