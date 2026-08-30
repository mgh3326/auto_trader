"""Durable immutable activation marker for the ROB-1301 Q6 collection."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BuyGateABCollectionEpoch(Base):
    """The one pre-recorded epoch; production migration is its only writer.

    ``first_valid_record_at`` is intentionally absent.  It is derived as a
    nullable observation from valid event rows and cannot move this marker.
    UPDATE, DELETE, and TRUNCATE are rejected by database triggers.
    """

    __tablename__ = "buy_gate_ab_collection_epoch"
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
            "addendum_version = 'rob-1331-q6-activation-epoch.v1'",
            name="addendum_version",
        ),
        CheckConstraint(
            "collection_calendar_days = 28",
            name="calendar_days",
        ),
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
        CheckConstraint(
            "jsonb_typeof(policy_projection) = 'object'",
            name="policy_projection_object",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        autoincrement=False,
    )
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
    policy_projection: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


__all__ = ["BuyGateABCollectionEpoch"]
