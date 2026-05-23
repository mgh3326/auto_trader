"""rob-301 p1: add investment_symbol_intermediate_reports

Symbol-scoped intermediate report artifacts (ROB-301). New dedicated table on the
SYMBOL axis (UNIQUE(run_uuid, symbol, report_kind, artifact_version)), distinct
from investment_stage_artifacts (analysis-dimension axis). Real FK to the owning
investment_stage_runs run.

Revision ID: 20260523_rob301_p1
Revises: e26f8bac2e5f
Create Date: 2026-05-23

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260523_rob301_p1"
down_revision: str | None = "e26f8bac2e5f"
branch_labels: str | None = None
depends_on: str | None = None

_DECISION_BUCKETS = (
    "new_buy_candidate",
    "open_action",
    "completed_or_existing",
    "deferred_no_action",
    "risk_watch",
)
_VERDICTS = ("buy", "sell", "hold", "risk", "unavailable")
_UNAVAILABLE_REASONS = ("hermes_omitted", "data_unavailable")
_REPORT_KINDS = ("final_report_symbol",)
_ACCOUNT_SCOPES = ("kis_live", "kis_mock", "alpaca_paper", "upbit_live")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "investment_symbol_intermediate_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "symbol_report_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "snapshot_bundle_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("symbol_name", sa.Text(), nullable=True),
        sa.Column(
            "report_kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'final_report_symbol'"),
        ),
        sa.Column(
            "artifact_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("decision_bucket", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "buy_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "sell_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "risk_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "missing_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "freshness_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "source_stage_artifact_uuids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::uuid[]"),
        ),
        sa.Column(
            "cited_snapshot_uuids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::uuid[]"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["run_uuid"],
            ["review.investment_stage_runs.run_uuid"],
            ondelete="CASCADE",
            name=op.f(
                "fk_investment_symbol_intermediate_reports_run_uuid_investment_stage_runs"
            ),
        ),
        sa.CheckConstraint(
            f"decision_bucket IN ({_in(_DECISION_BUCKETS)})",
            name=op.f("ck_investment_symbol_intermediate_reports_decision_bucket"),
        ),
        sa.CheckConstraint(
            f"verdict IN ({_in(_VERDICTS)})",
            name=op.f("ck_investment_symbol_intermediate_reports_verdict"),
        ),
        sa.CheckConstraint(
            "unavailable_reason IS NULL OR unavailable_reason IN "
            f"({_in(_UNAVAILABLE_REASONS)})",
            name=op.f("ck_investment_symbol_intermediate_reports_unavailable_reason"),
        ),
        sa.CheckConstraint(
            f"report_kind IN ({_in(_REPORT_KINDS)})",
            name=op.f("ck_investment_symbol_intermediate_reports_report_kind"),
        ),
        sa.CheckConstraint(
            f"account_scope IS NULL OR account_scope IN ({_in(_ACCOUNT_SCOPES)})",
            name=op.f("ck_investment_symbol_intermediate_reports_account_scope"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name=op.f("ck_investment_symbol_intermediate_reports_confidence_range"),
        ),
        sa.CheckConstraint(
            "verdict <> 'unavailable' OR decision_bucket = 'deferred_no_action'",
            name=op.f("ck_investment_symbol_intermediate_reports_unavailable_bucket"),
        ),
        sa.UniqueConstraint(
            "run_uuid",
            "symbol",
            "report_kind",
            "artifact_version",
            name=op.f("uq_investment_symbol_intermediate_reports_run_symbol_kind_ver"),
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_symbol_intermediate_reports_run_uuid",
        "investment_symbol_intermediate_reports",
        ["run_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_investment_symbol_intermediate_reports_symbol",
        "investment_symbol_intermediate_reports",
        ["symbol"],
        schema="review",
    )
    op.create_index(
        "ix_investment_symbol_intermediate_reports_bundle_uuid",
        "investment_symbol_intermediate_reports",
        ["snapshot_bundle_uuid"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_symbol_intermediate_reports_bundle_uuid",
        table_name="investment_symbol_intermediate_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_symbol_intermediate_reports_symbol",
        table_name="investment_symbol_intermediate_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_symbol_intermediate_reports_run_uuid",
        table_name="investment_symbol_intermediate_reports",
        schema="review",
    )
    op.drop_table(
        "investment_symbol_intermediate_reports",
        schema="review",
    )
