"""Investment report-scoped persistence (ROB-265).

Five entities under the ``review`` schema replace the legacy
``analysis_report*`` / ``watch_order_intent_ledger`` family.

* ``InvestmentReport`` — report header (one per published/draft report bundle).
* ``InvestmentReportItem`` — action/watch/risk items owned by a report.
* ``InvestmentReportItemDecision`` — operator decisions on items (audit).
* ``InvestmentWatchAlert`` — immutable activation snapshot of approved watch items.
* ``InvestmentWatchEvent`` — trigger events the scanner writes when an alert fires.

The shape is intentionally NOT backward-compatible with the legacy tables.
All writes must go through ``app.services.investment_reports.*`` (added in a
later plan). Direct ``INSERT/UPDATE/DELETE`` is forbidden once those services
land.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


# ---------------------------------------------------------------------------
# review.investment_reports — report header (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentReport(Base):
    """Report-scoped header artifact. Owns items, decisions, and watches.

    ``thesis_text`` and ``no_action_note`` are report-level fields (locked
    refinement: kept off the item table to avoid ``item_kind`` bloat).
    ``previous_report_uuid`` is a trace hint only — context retrieval is
    a query in a later plan, not a single-link traversal.
    """

    __tablename__ = "investment_reports"
    __table_args__ = (
        UniqueConstraint("report_uuid", name="uq_investment_reports_report_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_reports_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('draft','published','decided','expired','superseded')",
            name="ck_investment_reports_status",
        ),
        CheckConstraint(
            "execution_mode IN ('advisory_only','mock_preview')",
            name="ck_investment_reports_execution_mode",
        ),
        CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_investment_reports_account_scope",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_reports_market",
        ),
        CheckConstraint(
            "market_session IS NULL OR market_session IN "
            "('regular','nxt','pre','post','24x7')",
            name="ck_investment_reports_market_session",
        ),
        # Advisory-only invariants — locked refinement #6.
        # If account is live, execution_mode MUST be advisory_only.
        CheckConstraint(
            "account_scope IS DISTINCT FROM 'kis_live' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_live_advisory_only",
        ),
        # If session is NXT, execution_mode MUST be advisory_only.
        CheckConstraint(
            "market_session IS DISTINCT FROM 'nxt' OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_nxt_advisory_only",
        ),
        Index(
            "ix_investment_reports_market_session_created",
            "market",
            "market_session",
            "created_at",
        ),
        Index("ix_investment_reports_status_created", "status", "created_at"),
        Index("ix_investment_reports_report_type_created", "report_type", "created_at"),
        # ROB-269 Phase 3 — snapshot bundle linkage + 3-layer stale gate layer (i).
        Index(
            "ix_investment_reports_snapshot_bundle_uuid",
            "snapshot_bundle_uuid",
        ),
        CheckConstraint(
            # ROB-269 Phase 3 (corrected by 20260519_rob269_p3a): the explicit
            # ``IS NOT NULL`` guard collapses ``overall`` missing-key / JSON-null
            # to FALSE so PostgreSQL CHECK does not accept the row on UNKNOWN.
            "status <> 'published' "
            "OR snapshot_freshness_summary IS NULL "
            "OR ("
            "(snapshot_freshness_summary->>'overall') IS NOT NULL "
            "AND (snapshot_freshness_summary->>'overall') IN "
            "('fresh','soft_stale','partial')"
            ")",
            name="ck_investment_reports_no_published_on_hard_stale",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    market_session: Mapped[str | None] = mapped_column(Text)
    account_scope: Mapped[str | None] = mapped_column(Text)
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'advisory_only'")
    )
    created_by_profile: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_summary: Mapped[str | None] = mapped_column(Text)
    thesis_text: Mapped[str | None] = mapped_column(Text)
    no_action_note: Mapped[str | None] = mapped_column(Text)

    market_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    portfolio_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    previous_report_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    report_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # ROB-269 Phase 3 — bundle linkage + freshness/coverage metadata. All
    # nullable so legacy reports stay readable. ``snapshot_freshness_summary``
    # is consulted by the DB CHECK above; the structure must always carry an
    # ``overall`` key when set (see schemas / stale_gate).
    snapshot_bundle_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    snapshot_policy_version: Mapped[str | None] = mapped_column(Text)
    snapshot_coverage_summary: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_freshness_summary: Mapped[dict | None] = mapped_column(JSONB)
    source_conflicts: Mapped[dict | None] = mapped_column(JSONB)
    unavailable_sources: Mapped[dict | None] = mapped_column(JSONB)
    # ROB-318 Phase 3 (PR-B) — deterministic report-level diagnostics bundle:
    # {why_no_action, data_sufficiency_by_source, report_quality_summary}.
    # Nullable so legacy reports stay readable.
    snapshot_report_diagnostics: Mapped[dict | None] = mapped_column(JSONB)


# ---------------------------------------------------------------------------
# review.investment_report_items — action/watch/risk items owned by a report
# ---------------------------------------------------------------------------
class InvestmentReportItem(Base):
    """Report-owned proposal item. Source of truth for proposed watches.

    Locked refinements:
    * ``item_kind ∈ {action, watch, risk}`` only.
    * ``item_status`` excludes ``executed`` — execution lives in trade
      journals/broker ledgers, never on a report item.
    * ``target_kind`` preserved so the watch scanner's asset/index/fx
      dispatch can be reproduced.
    """

    __tablename__ = "investment_report_items"
    __table_args__ = (
        UniqueConstraint("item_uuid", name="uq_investment_report_items_item_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_report_items_idempotency_key"
        ),
        CheckConstraint(
            "item_kind IN ('action','watch','risk')",
            name="ck_investment_report_items_item_kind",
        ),
        CheckConstraint(
            "status IN ('proposed','approved','denied','deferred','activated','expired')",
            name="ck_investment_report_items_status",
        ),
        CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_report_items_target_kind",
        ),
        CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_investment_report_items_side",
        ),
        CheckConstraint(
            "intent IN ('buy_review','sell_review','risk_review',"
            "'trend_recovery_review','rebalance_review')",
            name="ck_investment_report_items_intent",
        ),
        # ROB-274 — proposal-lifecycle CHECKs. ``operation`` is nullable so
        # legacy rows (operation IS NULL) remain valid; the frontend treats
        # null as 'create' for the purposes of watch-condition rendering.
        CheckConstraint(
            "operation IS NULL OR operation IN ("
            "'create','modify','cancel','keep','replace','review'"
            ")",
            name="ck_investment_report_items_operation",
        ),
        CheckConstraint(
            "apply_policy IS NULL OR apply_policy = 'requires_user_approval'",
            name="ck_investment_report_items_apply_policy",
        ),
        # ROB-274 — operation-aware watch invariants. The pre-274 CHECKs
        # required watch_condition / valid_until for every watch item;
        # cancel/keep/review now reference an existing watch alert and
        # therefore don't carry a fresh condition. See migration
        # 20260520_rob274_p1 for the canonical predicate strings.
        CheckConstraint(
            "item_kind <> 'watch' "
            "OR operation IN ('cancel','keep','review') "
            "OR watch_condition IS NOT NULL",
            name="ck_investment_report_items_watch_has_condition",
        ),
        CheckConstraint(
            "item_kind <> 'watch' "
            "OR operation IN ('cancel','keep','review') "
            "OR valid_until IS NOT NULL",
            name="ck_investment_report_items_watch_has_expiry",
        ),
        CheckConstraint(
            "decision_bucket IS NULL OR decision_bucket IN "
            "('new_buy_candidate','open_action','completed_or_existing',"
            "'deferred_no_action','risk_watch')",
            name="ck_investment_report_items_decision_bucket",
        ),
        Index(
            "ix_investment_report_items_report",
            "report_id",
            "status",
        ),
        Index(
            "ix_investment_report_items_kind_status",
            "item_kind",
            "status",
        ),
        Index(
            "ix_investment_report_items_symbol",
            "symbol",
        ),
        Index(
            "ix_investment_report_items_operation_kind",
            "operation",
            "item_kind",
            "status",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    item_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'asset'")
    )

    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(8, 4))

    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    watch_condition: Mapped[dict | None] = mapped_column(JSONB)
    trigger_checklist: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_action: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'proposed'")
    )
    item_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    # ROB-274 proposal-state columns. All nullable; legacy rows keep these
    # NULL and the operation-aware CHECKs above let them through.
    operation: Mapped[str | None] = mapped_column(Text)
    target_ref: Mapped[dict | None] = mapped_column(JSONB)
    current_state: Mapped[dict | None] = mapped_column(JSONB)
    proposed_state: Mapped[dict | None] = mapped_column(JSONB)
    diff: Mapped[list | None] = mapped_column(JSONB)
    apply_policy: Mapped[str | None] = mapped_column(Text)

    # ROB-308 — final-item classification + citations.
    decision_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    cited_symbol_report_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    cited_dimension_report_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    # ROB-352 Slice B — per-item snapshot provenance citations. Mirrors
    # cited_dimension_report_uuids; derived from the item's evidence_snapshot
    # by the generator unless the caller supplies them explicitly.
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# review.investment_report_item_decisions — operator decision audit (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentReportItemDecision(Base):
    """One decision row per (item, actor, idempotency_key).

    Multiple decisions per item are allowed (e.g. ``defer`` → later
    ``approve``). The latest-decision query is left to the service layer.
    """

    __tablename__ = "investment_report_item_decisions"
    __table_args__ = (
        UniqueConstraint(
            "decision_uuid",
            name="uq_investment_report_item_decisions_decision_uuid",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_investment_report_item_decisions_idempotency_key",
        ),
        CheckConstraint(
            "decision IN ('approve','deny','defer','skip','partial_approve')",
            name="ck_investment_report_item_decisions_decision",
        ),
        Index(
            "ix_investment_report_item_decisions_item_created",
            "item_id",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_report_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    approved_payload_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.investment_watch_alerts — activated watch projection (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentWatchAlert(Base):
    """Immutable activation snapshot for an approved watch item.

    Items are the source of truth; alerts duplicate scanner-critical fields
    so the scanner doesn't have to join back to items on every tick. Once
    activated, the snapshot fields here are not mutated except for
    ``status`` and ``updated_at``.
    """

    __tablename__ = "investment_watch_alerts"
    __table_args__ = (
        UniqueConstraint("alert_uuid", name="uq_investment_watch_alerts_alert_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_alerts_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('active','triggered','expired','canceled')",
            name="ck_investment_watch_alerts_status",
        ),
        CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_watch_alerts_target_kind",
        ),
        CheckConstraint(
            "operator IN ('above','below','between')",
            name="ck_investment_watch_alerts_operator",
        ),
        CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required')",
            name="ck_investment_watch_alerts_action_mode",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_watch_alerts_market",
        ),
        CheckConstraint(
            "combine IN ('and')",
            name="ck_investment_watch_alerts_combine",
        ),
        Index(
            "ix_investment_watch_alerts_market_status",
            "market",
            "status",
        ),
        Index(
            "ix_investment_watch_alerts_status_valid_until",
            "status",
            "valid_until",
        ),
        Index(
            "ix_investment_watch_alerts_source_report",
            "source_report_uuid",
        ),
        Index(
            "ix_investment_watch_alerts_source_item",
            "source_item_uuid",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alert_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    source_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    source_item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )

    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_high: Mapped[float | None] = mapped_column(Numeric(20, 8))
    conditions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    combine: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'and'")
    )

    intent: Mapped[str] = mapped_column(Text, nullable=False)
    action_mode: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_checklist: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_action: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Watches are time-bounded by contract — alerts must always carry an
    # expiry. Status transitions to ``expired`` once ``valid_until`` passes
    # (transition logic lives in the scanner re-wire, Plan 4).
    valid_until: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    alert_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# review.investment_watch_events — scanner trigger events (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentWatchEvent(Base):
    """Scanner-emitted trigger event linked back to source report/item.

    Replaces every legacy write path that went through
    ``watch_order_intent_ledger``. ``idempotency_key`` is
    ``alert_uuid:kst_date:threshold_key`` so a single watch can only
    fire once per day per threshold cross.

    **Audit identity is self-contained.** Because ``alert_id`` is
    ``ON DELETE SET NULL``, the event row must carry the full immutable
    trigger snapshot (``market``, ``target_kind``, ``symbol``, ``metric``,
    ``operator``, ``threshold``, ``threshold_key``, ``intent``,
    ``action_mode``) so it remains audit-useful after the source alert
    is removed.

    ``source_report_uuid`` / ``source_item_uuid`` are logical audit links
    (no FK on purpose) — the snapshot fields above are the canonical
    record. Plan 2's service layer validates source existence/consistency
    at write time.
    """

    __tablename__ = "investment_watch_events"
    __table_args__ = (
        UniqueConstraint("event_uuid", name="uq_investment_watch_events_event_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_events_idempotency_key"
        ),
        CheckConstraint(
            "outcome IN ('notified','review_required','preview_attached',"
            "'expired','ignored','failed')",
            name="ck_investment_watch_events_outcome",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_watch_events_market",
        ),
        CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_watch_events_target_kind",
        ),
        CheckConstraint(
            "operator IN ('above','below','between')",
            name="ck_investment_watch_events_operator",
        ),
        CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required')",
            name="ck_investment_watch_events_action_mode",
        ),
        # Plan 4 hardening — Hermes delivery is auditable and the alert
        # is only transitioned to ``triggered`` once delivery_status is
        # ``delivered``. ``pending``/``skipped``/``failed`` rows are
        # legitimate audit history that the next scan loop can re-attempt.
        CheckConstraint(
            "delivery_status IN ('pending','delivered','skipped','failed')",
            name="ck_investment_watch_events_delivery_status",
        ),
        Index(
            "ix_investment_watch_events_alert_created",
            "alert_id",
            "created_at",
        ),
        Index(
            "ix_investment_watch_events_source_report",
            "source_report_uuid",
        ),
        Index(
            "ix_investment_watch_events_kst_date",
            "kst_date",
        ),
        Index(
            "ix_investment_watch_events_outcome_created",
            "outcome",
            "created_at",
        ),
        Index(
            "ix_investment_watch_events_delivery_status_created",
            "delivery_status",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    # SET NULL so historical events survive an alert deletion (audit).
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.investment_watch_alerts.id", ondelete="SET NULL")
    )
    source_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    source_item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )

    # Immutable trigger-identity snapshot copied from the source alert at
    # event creation. Must survive alert deletion.
    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    threshold_high: Mapped[float | None] = mapped_column(Numeric(20, 8))
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    action_mode: Mapped[str] = mapped_column(Text, nullable=False)

    current_value: Mapped[float | None] = mapped_column(Numeric(20, 8))
    scanner_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_report_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.investment_report_items.id", ondelete="SET NULL")
    )

    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)

    # Plan 4 hardening — Hermes delivery tracking. ``delivery_status``
    # starts at ``pending``; the scanner updates it after the delivery
    # attempt. The alert.status transition to ``triggered`` is gated on
    # ``delivered`` so a skipped/failed delivery does not silently
    # consume the watch.
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    delivery_reason: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
