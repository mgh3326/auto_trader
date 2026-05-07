"""add research_reports tables (ROB-140)

Revision ID: b1c2d3e4
Revises: c1a2b3d4
Create Date: 2026-05-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b1c2d3e4"
down_revision: str | Sequence[str] | None = "c1a2b3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_report_ingestion_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exported_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("copyright_notice", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "run_uuid", name="uq_research_report_ingestion_runs_run_uuid"
        ),
    )

    op.create_table(
        "research_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("analyst", sa.Text(), nullable=True),
        sa.Column("published_at_text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("detail_url", sa.Text(), nullable=True),
        sa.Column("detail_title", sa.Text(), nullable=True),
        sa.Column("detail_subtitle", sa.Text(), nullable=True),
        sa.Column("detail_excerpt", sa.Text(), nullable=True),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column("pdf_filename", sa.Text(), nullable=True),
        sa.Column("pdf_sha256", sa.Text(), nullable=True),
        sa.Column("pdf_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("pdf_page_count", sa.Integer(), nullable=True),
        sa.Column("pdf_text_length", sa.Integer(), nullable=True),
        sa.Column(
            "symbol_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("raw_text_policy", sa.Text(), nullable=True),
        sa.Column("attribution_publisher", sa.Text(), nullable=True),
        sa.Column("attribution_copyright_notice", sa.Text(), nullable=True),
        sa.Column(
            "attribution_full_text_exported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "attribution_pdf_body_exported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ingestion_run_id",
            sa.BigInteger(),
            sa.ForeignKey("research_report_ingestion_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("dedup_key", name="uq_research_reports_dedup_key"),
    )
    op.create_index(
        "ix_research_reports_published_at",
        "research_reports",
        ["published_at"],
    )
    op.create_index(
        "ix_research_reports_source_published_at",
        "research_reports",
        ["source", "published_at"],
    )
    op.create_index(
        "ix_research_reports_symbol_candidates_gin",
        "research_reports",
        ["symbol_candidates"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_reports_symbol_candidates_gin", table_name="research_reports"
    )
    op.drop_index(
        "ix_research_reports_source_published_at", table_name="research_reports"
    )
    op.drop_index("ix_research_reports_published_at", table_name="research_reports")
    op.drop_table("research_reports")
    op.drop_table("research_report_ingestion_runs")
