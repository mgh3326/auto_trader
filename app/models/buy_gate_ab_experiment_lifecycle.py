"""Append-only durable lifecycle records for ROB-1301 termination and v2."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BuyGateABExperimentTermination(Base):
    """The singleton, immutable terminal record for the ROB-1301 epoch."""

    __tablename__ = "buy_gate_ab_experiment_termination"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint(
            "experiment_id = 'rob-1301-buy-gate-ab-shadow'",
            name="experiment_id",
        ),
        CheckConstraint(
            "epoch_id = 'rob-1301-q6-collection-epoch.v1'",
            name="epoch_id",
        ),
        CheckConstraint(
            "reason = 'STOPPED_BY_OPERATOR_DECISION'",
            name="reason",
        ),
        CheckConstraint("decided_by = 'operator'", name="decided_by"),
        CheckConstraint("carryover = 'forbidden'", name="carryover"),
        CheckConstraint(
            "terminal_status = 'INSUFFICIENT_SAMPLE'",
            name="terminal_status",
        ),
        CheckConstraint("terminal_outcome = 'NO_FIRING'", name="terminal_outcome"),
        CheckConstraint(
            "preregistration_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="preregistration_spec_sha256",
        ),
        CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    experiment_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    epoch_id: Mapped[str] = mapped_column(Text, nullable=False)
    terminated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)
    carryover: Mapped[str] = mapped_column(Text, nullable=False)
    terminal_status: Mapped[str] = mapped_column(Text, nullable=False)
    terminal_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    preregistration_spec_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    policy_projection_sha256: Mapped[str] = mapped_column(Text, nullable=False)


class BuyGateABExperimentRegistration(Base):
    """The singleton, immutable ROB-1351 v2 registration (not activation)."""

    __tablename__ = "buy_gate_ab_experiment_registration"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint(
            "experiment_id = 'rob-1351-buy-gate-moderate-live'",
            name="experiment_id",
        ),
        CheckConstraint(
            "preregistration_version = 'rob-1351-buy-gate-moderate-live.v1'",
            name="preregistration_version",
        ),
        CheckConstraint(
            "spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="spec_sha256",
        ),
        CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(policy_projection) = 'object'",
            name="policy_projection_object",
        ),
        CheckConstraint(
            "predecessor_experiment_id = 'rob-1301-buy-gate-ab-shadow'",
            name="predecessor_experiment_id",
        ),
        CheckConstraint("carryover = 'forbidden'", name="carryover"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    experiment_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    preregistration_version: Mapped[str] = mapped_column(Text, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    policy_projection_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    policy_projection: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    predecessor_experiment_id: Mapped[str] = mapped_column(Text, nullable=False)
    carryover: Mapped[str] = mapped_column(Text, nullable=False)


class BuyGateABCollectionEpochV2(Base):
    """The intentionally empty future v2 activation marker table."""

    __tablename__ = "buy_gate_ab_collection_epoch_v2"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint(
            "experiment_id = 'rob-1351-buy-gate-moderate-live'",
            name="experiment_id",
        ),
        CheckConstraint("collection_calendar_days = 28", name="calendar_days"),
        CheckConstraint(
            "collection_end_exclusive = "
            "collection_start + collection_calendar_days::integer",
            name="fixed_window",
        ),
        CheckConstraint(
            "collection_clock_timezone = 'Asia/Seoul'",
            name="clock_timezone",
        ),
        CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        CheckConstraint(
            "preregistration_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="preregistration_spec_sha256",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    experiment_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    epoch_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    addendum_version: Mapped[str] = mapped_column(Text, nullable=False)
    collection_armed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    collection_start: Mapped[date] = mapped_column(Date, nullable=False)
    collection_end_exclusive: Mapped[date] = mapped_column(Date, nullable=False)
    collection_calendar_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    collection_clock_timezone: Mapped[str] = mapped_column(Text, nullable=False)
    policy_projection_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    preregistration_spec_sha256: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "BuyGateABCollectionEpochV2",
    "BuyGateABExperimentRegistration",
    "BuyGateABExperimentTermination",
]
