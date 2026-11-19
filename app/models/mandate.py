"""Mandate model tables (hk 3795 ticket T1): clauses, approvals, activation profiles
and split plans.

Nine ``review`` tables (T1) plus the two morning-batch tables of T3a
(``MandatePlanBatch``, ``MandatePlanBatchMember``), all append-only: no UPDATE, DELETE or TRUNCATE for any
role (DB triggers in ``alembic/versions/20261120_hk5528_mandate_model.py``). A plan's
state is the fold of its events (``app.services.mandate.fold``). All writes go
through ``app.services.mandate.store``.

The production DDL is the migration; ORM metadata builds the test schema, so the
two are pinned together by ``tests/services/mandate/test_migration_shape.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.services.mandate import vocabulary as v


def _in(values: tuple[str, ...] | frozenset[str]) -> str:
    return ",".join(f"'{x}'" for x in sorted(values))


def _event_in(events: frozenset[str]) -> str:
    return f"event IN ({_in(events)})"


def event_check_sql() -> list[tuple[str, str]]:
    """``(name, sql)`` of every CHECK on ``mandate_plan_events``.

    Generated from ``vocabulary.FIELD_RULES`` so the field rules of §3.2 are
    stated once; the migration carries the rendered copy.
    """
    checks: list[tuple[str, str]] = [
        ("event_vocabulary", f"event IN ({_in(v.ALL_EVENTS)})"),
        ("seq_positive", "seq >= 1"),
    ]
    for group, (required, allowed) in v.FIELD_RULES.items():
        columns = v.FIELD_COLUMNS[group]
        first = columns[0]
        if len(columns) > 1:
            both = " AND ".join(
                f"({c} IS NULL) = ({first} IS NULL)" for c in columns[1:]
            )
            checks.append((f"{group}_together", both))
        if required == allowed:
            checks.append(
                (f"{group}_fields", f"({first} IS NOT NULL) = ({_event_in(required)})")
            )
        else:
            checks.append(
                (
                    f"{group}_allowed",
                    f"{first} IS NULL OR {_event_in(allowed)}",
                )
            )
            if required:
                checks.append(
                    (
                        f"{group}_required",
                        f"NOT ({_event_in(required)}) OR {first} IS NOT NULL",
                    )
                )
    checks += [
        (
            "tranche_index_range",
            "tranche_index IS NULL OR tranche_index BETWEEN 1 AND 3",
        ),
        (
            "resume_target_value",
            "resume_target IS NULL OR resume_target IN ('approved','active')",
        ),
        (
            "approval_mode_value",
            f"approval_mode IS NULL OR approval_mode IN ({_in(v.APPROVAL_MODES)})",
        ),
        (
            "profile_with_auto_fence",
            "(event = 'tranche_send_fenced' AND approval_mode = 'auto') "
            "= (activation_profile_id IS NOT NULL)",
        ),
        (
            "first_fill_fields",
            "((first_fill_amount IS NULL) = (first_fill_quantity IS NULL)) AND "
            "((first_fill_amount IS NOT NULL) = "
            "(event IN ('tranche_closed_partial','tranche_filled') "
            "AND tranche_index = 1))",
        ),
        (
            "amounts_positive",
            "(notional IS NULL OR notional > 0) AND "
            "(fill_amount IS NULL OR fill_amount > 0) AND "
            "(fill_quantity IS NULL OR fill_quantity > 0) AND "
            "(first_fill_amount IS NULL OR first_fill_amount > 0) AND "
            "(first_fill_quantity IS NULL OR first_fill_quantity > 0)",
        ),
        ("fenced_seq_positive", "fenced_seq IS NULL OR fenced_seq >= 1"),
        ("thesis_seq_positive", "thesis_seq IS NULL OR thesis_seq >= 1"),
        (
            "evidence_object",
            "jsonb_typeof(evidence) = 'object'",
        ),
        (
            "evidence_keys",
            "(evidence - ARRAY[" + ",".join(f"'{k}'" for k in v.EVIDENCE_KEYS) + "]) "
            "= '{}'::jsonb",
        ),
        ("actor_nonblank", "char_length(btrim(actor)) BETWEEN 1 AND 128"),
    ]
    return checks


class MandateApprovalRequest(Base):
    """Immutable snapshot of what an operator is asked to approve (§3.5)."""

    __tablename__ = "mandate_approval_requests"
    __table_args__ = (
        CheckConstraint(
            f"subject_kind IN ({_in(v.APPROVAL_SUBJECT_KINDS)})", name="subject_kind"
        ),
        CheckConstraint("base_sleeve_version >= 1", name="base_version_positive"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"
        ),
        CheckConstraint(
            "char_length(btrim(approval_ref)) BETWEEN 1 AND 300",
            name="approval_ref_nonblank",
        ),
        CheckConstraint(
            "char_length(btrim(requested_by)) BETWEEN 1 AND 128",
            name="requested_by_nonblank",
        ),
        Index("ix_mandate_approval_requests_sleeve_id", "sleeve_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    base_sleeve_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    approval_ref: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateApproval(Base):
    """One authenticated operator decision on a request (append-only)."""

    __tablename__ = "mandate_approvals"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_mandate_approvals_request_id"),
        CheckConstraint(f"decision IN ({_in(v.APPROVAL_DECISIONS)})", name="decision"),
        CheckConstraint(
            "subject_sha256 ~ '^[0-9a-f]{64}$'", name="subject_sha256_format"
        ),
        CheckConstraint(
            "principal ~ '^[1-9][0-9]{0,15}$' AND "
            "(char_length(principal) < 16 OR principal <= '4503599627370495')",
            name="principal_format",
        ),
        CheckConstraint("nonce_digest ~ '^[0-9a-f]{64}$'", name="nonce_digest_format"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_approval_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    subject_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    principal: Mapped[str] = mapped_column(Text, nullable=False)
    nonce_digest: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateTerms(Base):
    """The clause of one live sleeve version (§3.1), bound to an approval."""

    __tablename__ = "mandate_terms"
    __table_args__ = (
        UniqueConstraint(
            "sleeve_version_id", name="uq_mandate_terms_sleeve_version_id"
        ),
        UniqueConstraint("approval_id", name="uq_mandate_terms_approval_id"),
        CheckConstraint(f"terms_version = '{v.TERMS_VERSION}'", name="terms_version"),
        CheckConstraint(
            "basket_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'", name="basket_key_format"
        ),
        CheckConstraint(
            "char_length(btrim(account_ref)) BETWEEN 1 AND 200",
            name="account_ref_nonblank",
        ),
        CheckConstraint(
            "char_length(btrim(universe_rule)) BETWEEN 1 AND 500",
            name="universe_rule_nonblank",
        ),
        CheckConstraint("entry_max_premium_bps > 0", name="premium_positive"),
        CheckConstraint("per_order_cap > 0", name="per_order_cap_positive"),
        CheckConstraint("daily_cap > 0", name="daily_cap_positive"),
        CheckConstraint(
            "sector_cap_pct > 0 AND sector_cap_pct <= 100", name="sector_cap_range"
        ),
        CheckConstraint(
            "basket_drawdown_freeze_pct > 0 AND basket_drawdown_freeze_pct <= 100",
            name="basket_drawdown_range",
        ),
        CheckConstraint(
            "account_drawdown_freeze_pct > 0 AND account_drawdown_freeze_pct <= 100",
            name="account_drawdown_range",
        ),
        CheckConstraint(
            "jsonb_typeof(tranche_rule) = 'object' AND "
            "(tranche_rule - ARRAY['weights','triggers_pct_from_first','min_gap_sessions']) "
            "= '{}'::jsonb AND "
            "tranche_rule ?& ARRAY['weights','triggers_pct_from_first','min_gap_sessions']",
            name="tranche_rule_closed",
        ),
        CheckConstraint(
            f"loss_sell_mode IN ({_in(v.LOSS_SELL_MODES)})", name="loss_sell_mode"
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"
        ),
        Index("ix_mandate_terms_basket_key", "basket_key"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeve_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terms_version: Mapped[str] = mapped_column(Text, nullable=False)
    basket_key: Mapped[str] = mapped_column(Text, nullable=False)
    account_ref: Mapped[str] = mapped_column(Text, nullable=False)
    universe_rule: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_symbols: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    entry_max_premium_bps: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    per_order_cap: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    daily_cap: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    sector_cap_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    basket_drawdown_freeze_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    account_drawdown_freeze_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    tranche_rule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    loss_sell_mode: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    approval_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateActivationProfile(Base):
    """Which clause version opens auto-approval, and up to which tranche (§3.6)."""

    __tablename__ = "mandate_activation_profiles"
    __table_args__ = (
        UniqueConstraint(
            "sleeve_id",
            "profile_version",
            name="uq_mandate_activation_profiles_version",
        ),
        UniqueConstraint(
            "approval_id", name="uq_mandate_activation_profiles_approval_id"
        ),
        CheckConstraint("profile_version >= 1", name="version_positive"),
        CheckConstraint("max_tranche_index BETWEEN 0 AND 3", name="max_tranche_range"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    terms_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    max_tranche_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    effective_session: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    approval_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandatePlan(Base):
    """Immutable identity of one split-buy plan for one symbol in one account."""

    __tablename__ = "mandate_plans"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(symbol)) BETWEEN 1 AND 40", name="symbol_nonblank"
        ),
        CheckConstraint(
            "char_length(btrim(account_ref)) BETWEEN 1 AND 200",
            name="account_ref_nonblank",
        ),
        CheckConstraint(
            "cardinality(scope_account_refs) BETWEEN 1 AND 2", name="scope_refs_size"
        ),
        CheckConstraint(
            "account_ref = ANY (scope_account_refs)", name="account_in_scope"
        ),
        CheckConstraint("planned_amount > 0", name="planned_positive"),
        CheckConstraint(
            "adopted_amount >= 0 AND adopted_amount <= planned_amount",
            name="adopted_within_planned",
        ),
        CheckConstraint(
            "jsonb_typeof(adopted_evidence) = 'object'", name="adopted_evidence_object"
        ),
        CheckConstraint(
            "char_length(btrim(thesis)) BETWEEN 1 AND 1000", name="thesis_length"
        ),
        CheckConstraint(
            "cardinality(invalidation_conditions) <= 3", name="invalidation_size"
        ),
        CheckConstraint(
            "char_length(btrim(sector_key)) BETWEEN 1 AND 100",
            name="sector_key_nonblank",
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="created_by_nonblank",
        ),
        Index("ix_mandate_plans_sleeve_id", "sleeve_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    approved_terms_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_ref: Mapped[str] = mapped_column(Text, nullable=False)
    scope_account_refs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    planned_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    adopted_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    adopted_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    invalidation_conditions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False
    )
    sector_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandatePlanTranche(Base):
    """One tranche of a plan, created with the plan (immutable)."""

    __tablename__ = "mandate_plan_tranches"
    __table_args__ = (
        CheckConstraint("tranche_index BETWEEN 1 AND 3", name="tranche_index_range"),
        CheckConstraint("weight > 0 AND weight <= 1", name="weight_range"),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
        CheckConstraint("trigger_pct_from_first <= 0", name="trigger_not_positive"),
        CheckConstraint("min_gap_sessions >= 0", name="min_gap_nonnegative"),
        CheckConstraint(
            "tranche_index <> 1 OR (trigger_pct_from_first = 0 AND min_gap_sessions = 0)",
            name="first_tranche_unconditional",
        ),
        {"schema": "review"},
    )

    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_plans.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    tranche_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    trigger_pct_from_first: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    min_gap_sessions: Mapped[int] = mapped_column(Integer, nullable=False)


class MandatePlanEvent(Base):
    """Append-only state transition of a plan (§3.2). State = fold of the stream."""

    __tablename__ = "mandate_plan_events"
    __table_args__ = (
        UniqueConstraint("plan_id", "seq", name="uq_mandate_plan_events_plan_seq"),
        *(CheckConstraint(sql, name=name) for name, sql in event_check_sql()),
        # One terminal event per proposal, one attempt per fence, one release per
        # fence: replays of the projection cannot double count.
        Index(
            "uq_mandate_plan_events_terminal_proposal",
            "plan_id",
            "proposal_id",
            unique=True,
            postgresql_where=text(
                "event IN ('tranche_filled','tranche_partially_filled','tranche_unfilled')"
            ),
        ),
        Index(
            "uq_mandate_plan_events_proposed",
            "plan_id",
            "proposal_id",
            unique=True,
            postgresql_where=text("event = 'tranche_proposed'"),
        ),
        Index(
            "uq_mandate_plan_events_fence_attempt",
            "plan_id",
            "attempt_id",
            unique=True,
            postgresql_where=text("event = 'tranche_send_fenced'"),
        ),
        Index(
            "uq_mandate_plan_events_fence_release",
            "plan_id",
            "fenced_seq",
            unique=True,
            postgresql_where=text("event = 'tranche_send_released'"),
        ),
        Index("ix_mandate_plan_events_event", "plan_id", "event"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    tranche_index: Mapped[int | None] = mapped_column(SmallInteger)
    first_fill_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    first_fill_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    fill_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    fill_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    terms_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("review.mandate_terms.id", ondelete="RESTRICT")
    )
    terms_sha256: Mapped[str | None] = mapped_column(Text)
    effective_session: Mapped[date | None] = mapped_column(Date)
    resume_target: Mapped[str | None] = mapped_column(Text)
    # No FK: the batch tables arrive with T3a.
    batch_member_id: Mapped[int | None] = mapped_column(BigInteger)
    # No FK (§3.2): the proposal row may be voided or rebuilt without touching history.
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    notional: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    kst_day: Mapped[date | None] = mapped_column(Date)
    approval_mode: Mapped[str | None] = mapped_column(Text)
    account_ref: Mapped[str | None] = mapped_column(Text)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    activation_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_activation_profiles.id", ondelete="RESTRICT"),
    )
    fenced_seq: Mapped[int | None] = mapped_column(Integer)
    thesis_seq: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateSymbolReservation(Base):
    """A plan's claim on (realm, market, symbol); held until ``released`` (I1)."""

    __tablename__ = "mandate_symbol_reservations"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_mandate_symbol_reservations_plan_id"),
        CheckConstraint(f"realm IN ({_in(v.REALMS)})", name="realm"),
        CheckConstraint(f"market IN ({_in(v.PLAN_MARKETS)})", name="market"),
        CheckConstraint(
            "char_length(btrim(symbol)) BETWEEN 1 AND 40", name="symbol_nonblank"
        ),
        Index("ix_mandate_symbol_reservations_key", "realm", "market", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    realm: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateSymbolReservationRelease(Base):
    """The release of a reservation, written with the plan's ``released`` event."""

    __tablename__ = "mandate_symbol_reservation_releases"
    __table_args__ = (
        UniqueConstraint(
            "reservation_id",
            name="uq_mandate_symbol_reservation_releases_reservation_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_symbol_reservations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandatePlanBatch(Base):
    """One morning approval batch of a basket for one target session (I22, §5.1).

    ``UNIQUE(realm, basket_key, target_session)`` plus the insert trigger of
    ``20261120_hk5648_mandate_batches`` ("a new batch only after the basket's earlier
    batch is closed") keep a basket to one open batch. Both deadlines are fixed
    here: approvals before ``approval_deadline_at`` (the open of the target
    session) take effect on it, approvals up to ``expires_at`` (its close) on the
    next session, and nothing is answered after ``expires_at``.
    """

    __tablename__ = "mandate_plan_batches"
    __table_args__ = (
        UniqueConstraint(
            "realm",
            "basket_key",
            "target_session",
            name="uq_mandate_plan_batches_basket_session",
        ),
        CheckConstraint(f"realm IN ({_in(v.REALMS)})", name="realm"),
        CheckConstraint(f"market IN ({_in(v.PLAN_MARKETS)})", name="market"),
        CheckConstraint(
            "char_length(btrim(basket_key)) BETWEEN 1 AND 100",
            name="basket_key_nonblank",
        ),
        CheckConstraint("approval_deadline_at < expires_at", name="deadlines_ordered"),
        CheckConstraint(
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="created_by_nonblank",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    realm: Mapped[str] = mapped_column(Text, nullable=False)
    basket_key: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_session: Mapped[date] = mapped_column(Date, nullable=False)
    approval_deadline_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandatePlanBatchMember(Base):
    """One card issued for one plan of a batch (the immutable snapshot, §5.1).

    ``card_no`` 1 is written with the batch. A re-send appends the next ``card_no``
    with a new nonce digest and the same snapshot; only the highest card of a
    (batch, plan) is answerable. The decision itself is the plan event that carries
    the card's id in ``batch_member_id``: at most one per (batch, plan), which is
    also what makes a nonce single-use. Nothing here is ever updated.
    """

    __tablename__ = "mandate_plan_batch_members"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "plan_id", "card_no", name="uq_mandate_plan_batch_members_card"
        ),
        UniqueConstraint("nonce_digest", name="uq_mandate_plan_batch_members_nonce"),
        CheckConstraint("card_no >= 1", name="card_no_positive"),
        CheckConstraint("kind IN ('new_plan','reapproval')", name="kind"),
        CheckConstraint("terms_sha256 ~ '^[0-9a-f]{64}$'", name="terms_sha256_format"),
        CheckConstraint("plan_sha256 ~ '^[0-9a-f]{64}$'", name="plan_sha256_format"),
        CheckConstraint("nonce_digest ~ '^[0-9a-f]{64}$'", name="nonce_digest_format"),
        CheckConstraint(
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="created_by_nonblank",
        ),
        Index("ix_mandate_plan_batch_members_plan", "plan_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_plan_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    card_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    terms_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.mandate_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terms_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    plan_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    nonce_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
