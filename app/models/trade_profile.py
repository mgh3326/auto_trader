from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trading import InstrumentType


class ProfileName(enum.StrEnum):
    aggressive = "aggressive"
    balanced = "balanced"
    conservative = "conservative"
    exit = "exit"
    hold_only = "hold_only"


class SellMode(enum.StrEnum):
    any = "any"
    rebalance_only = "rebalance_only"
    none = "none"


class TierParamType(enum.StrEnum):
    buy = "buy"
    sell = "sell"
    stop = "stop"
    rebalance = "rebalance"
    common = "common"


class FilterName(enum.StrEnum):
    regime = "regime"
    kill_switch = "kill_switch"
    fear_greed = "fear_greed"
    funding_rate = "funding_rate"
    btc_filter = "btc_filter"
    liquidity = "liquidity"


class AssetProfile(Base):
    __tablename__ = "asset_profiles"
    __table_args__ = (
        CheckConstraint("tier BETWEEN 1 AND 4", name="asset_profiles_tier_range"),
        CheckConstraint(
            "profile IN ('aggressive','balanced','conservative','exit','hold_only')",
            name="asset_profiles_profile_allowed",
        ),
        CheckConstraint(
            "sell_mode IN ('any','rebalance_only','none')",
            name="asset_profiles_sell_mode_allowed",
        ),
        CheckConstraint(
            "profile <> 'exit' OR buy_allowed = FALSE",
            name="asset_profiles_exit_buy_rule",
        ),
        CheckConstraint(
            "profile <> 'hold_only' OR sell_mode = 'rebalance_only'",
            name="asset_profiles_hold_only_sell_mode_rule",
        ),
        CheckConstraint(
            "tags IS NULL OR jsonb_typeof(tags) = 'array'",
            name="asset_profiles_tags_array_type",
        ),
        Index(
            "uq_asset_profiles_user_symbol_instrument",
            "user_id",
            "symbol",
            "instrument_type",
            unique=True,
        ),
        Index("ix_asset_profiles_user_instrument_type", "user_id", "instrument_type"),
        Index("ix_asset_profiles_user_profile", "user_id", "profile"),
        Index("ix_asset_profiles_tags_gin", "tags", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    profile: Mapped[str] = mapped_column(String(24), nullable=False)
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    max_position_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    buy_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sell_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SellMode.any.value
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TierRuleParam(Base):
    __tablename__ = "tier_rule_params"
    __table_args__ = (
        CheckConstraint("tier BETWEEN 1 AND 4", name="tier_rule_params_tier_range"),
        CheckConstraint(
            "profile IN ('aggressive','balanced','conservative','exit','hold_only')",
            name="tier_rule_params_profile_allowed",
        ),
        CheckConstraint(
            "param_type IN ('buy','sell','stop','rebalance','common')",
            name="tier_rule_params_param_type_allowed",
        ),
        Index(
            "uq_tier_rule_params_key",
            "user_id",
            "instrument_type",
            "tier",
            "profile",
            "param_type",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    profile: Mapped[str] = mapped_column(String(24), nullable=False)
    param_type: Mapped[str] = mapped_column(String(16), nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketFilter(Base):
    __tablename__ = "market_filters"
    __table_args__ = (
        CheckConstraint(
            r"filter_name ~ '^[a-z][a-z0-9_]{0,29}$'",
            name="market_filters_filter_name_format",
        ),
        Index(
            "ix_market_filters_user_instrument_enabled",
            "user_id",
            "instrument_type",
            "enabled",
        ),
        Index(
            "uq_market_filters_user_instrument_filter",
            "user_id",
            "instrument_type",
            "filter_name",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    filter_name: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProfileChangeLog(Base):
    __tablename__ = "profile_change_log"
    __table_args__ = (
        Index("ix_profile_change_log_user_changed_at", "user_id", "changed_at"),
        Index("ix_profile_change_log_target_changed_at", "target", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
