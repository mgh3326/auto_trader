# app/models/investment_snapshots.py
"""ROB-269 Phase 1 — Snapshot foundation ORM (immutable artifacts).

Four tables under ``review`` schema:
* ``investment_snapshot_runs``   — one collection run.
* ``investment_snapshots``       — immutable artifact row.
* ``investment_snapshot_bundles``— a reusable report data bundle.
* ``investment_snapshot_bundle_items`` — bundle ↔ snapshot link with role.

Append-only invariant is enforced at the service layer
(``app.services.investment_snapshots.repository``). Direct ``UPDATE/DELETE``
is forbidden once services land.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base

_ACCOUNT_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','alpaca_paper','upbit_live')"
)
_MARKET_CHECK = "market IN ('kr','us','crypto')"


# ---------------------------------------------------------------------------
# review.investment_snapshot_runs
# ---------------------------------------------------------------------------
class InvestmentSnapshotRun(Base):
    __tablename__ = "investment_snapshot_runs"
    __table_args__ = (
        UniqueConstraint("run_uuid", name="uq_investment_snapshot_runs_run_uuid"),
        CheckConstraint(
            "purpose IN ('report_generation','scheduled_refresh',"
            "'manual_refresh','reviewer_requested')",
            name="ck_investment_snapshot_runs_purpose",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_runs_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_runs_account_scope",
        ),
        CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_investment_snapshot_runs_status",
        ),
        CheckConstraint(
            "requested_by IN ('hermes','user','scheduler','claude_code','reviewer')",
            name="ck_investment_snapshot_runs_requested_by",
        ),
        Index(
            "ix_investment_snapshot_runs_purpose_market_started",
            "purpose",
            "market",
            text("started_at DESC"),
        ),
        Index(
            "ix_investment_snapshot_runs_status_started",
            "status",
            text("started_at DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    refresh_reason: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    run_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


# ---------------------------------------------------------------------------
# review.investment_snapshots
# ---------------------------------------------------------------------------
class InvestmentSnapshot(Base):
    __tablename__ = "investment_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_uuid", name="uq_investment_snapshots_snapshot_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_snapshots_idempotency_key"
        ),
        UniqueConstraint(
            "canonical_payload_hash",
            "snapshot_kind",
            "market",
            "account_scope",
            name="uq_investment_snapshots_canonical_dedup",
        ),
        CheckConstraint(
            "snapshot_kind IN ('portfolio','market','news','symbol',"
            "'candidate_universe','browser_probe','invest_page',"
            "'journal','watch_context','naver_remote_debug',"
            "'toss_remote_debug','llm_input_frozen','pending_orders',"
            "'validated_run_card','kr_market_ranking')",
            name="ck_investment_snapshots_snapshot_kind",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshots_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshots_account_scope",
        ),
        CheckConstraint(
            "source_kind IN ('kis_mcp','auto_trader_mcp','invest_api',"
            "'naver_remote_debug','toss_remote_debug','combined',"
            "'news_ingestor','manual','domain_ref')",
            name="ck_investment_snapshots_source_kind",
        ),
        CheckConstraint(
            "freshness_status IN ('fresh','soft_stale','hard_stale',"
            "'partial','unavailable')",
            name="ck_investment_snapshots_freshness_status",
        ),
        CheckConstraint(
            "(source_table IS NULL AND source_id IS NULL AND source_uri IS NULL) "
            "OR (source_table IS NOT NULL AND source_id IS NOT NULL "
            "AND source_uri IS NOT NULL)",
            name="ck_investment_snapshots_source_ref_triple",
        ),
        Index(
            "ix_investment_snapshots_kind_market_symbol_as_of",
            "snapshot_kind",
            "market",
            "symbol",
            text("as_of DESC"),
        ),
        Index("ix_investment_snapshots_source_uri", "source_uri"),
        Index("ix_investment_snapshots_run_id", "run_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_snapshot_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_kind: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)

    source_table: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_uri: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)

    payload_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_timestamps_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    coverage_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    errors_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    freshness_status: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.investment_snapshot_bundles
# ---------------------------------------------------------------------------
class InvestmentSnapshotBundle(Base):
    __tablename__ = "investment_snapshot_bundles"
    __table_args__ = (
        UniqueConstraint(
            "bundle_uuid", name="uq_investment_snapshot_bundles_bundle_uuid"
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_investment_snapshot_bundles_idempotency_key",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_bundles_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_bundles_account_scope",
        ),
        CheckConstraint(
            "status IN ('complete','partial','stale_fallback','failed')",
            name="ck_investment_snapshot_bundles_status",
        ),
        Index(
            "ix_investment_snapshot_bundles_purpose_market_account_asof",
            "purpose",
            "market",
            "account_scope",
            text("as_of DESC"),
        ),
        Index(
            "ix_investment_snapshot_bundles_status_created",
            "status",
            text("created_at DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    freshness_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.investment_snapshot_bundle_items
# ---------------------------------------------------------------------------
class InvestmentSnapshotBundleItem(Base):
    __tablename__ = "investment_snapshot_bundle_items"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "snapshot_id",
            name="uq_investment_snapshot_bundle_items_bundle_snapshot",
        ),
        CheckConstraint(
            "role IN ('required','optional','fallback','conflict_evidence')",
            name="ck_investment_snapshot_bundle_items_role",
        ),
        Index(
            "ix_investment_snapshot_bundle_items_snapshot",
            "snapshot_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bundle_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_snapshot_bundles.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
