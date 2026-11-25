"""hk 6508: FnGuide public CompanyInfo snapshots (consensus, invest, finance).

Revision ID: 20261125_hk6508_fnguide_snapshots
Revises: 20261125_hk6322_us_venue_val
Create Date: 2026-11-25

Additive. Three new tables, no existing object is touched:

* ``fnguide_snapshots`` — one row per (symbol, page, observed_date), with the
  fetch time, source URL, the header labels exactly as the page gives them, the
  bounded raw embedded JSON (or the reason it is absent) and the counts. A page
  that parsed but carries no values is stored with ``status='no_values'`` and a
  closed ``reason``; fetch failures are never stored here.
* ``fnguide_values`` — normalized cells per (snapshot, section, item, column)
  with an exact decimal string, or NULL plus a closed reason.
* ``fnguide_reports`` — broker research list entries per (symbol, observed_date).

Conditional ``at_app`` grants (hard rule 18): SELECT and INSERT only, plus the
identity sequences; no UPDATE or DELETE, the collector is insert-only.
Downgrade refuses while any row exists, so no collected history is discarded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261125_hk6508_fnguide_snapshots"
down_revision: str | Sequence[str] | None = "20261125_hk6322_us_venue_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fnguide_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("page", sa.String(length=16), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("page_title", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "raw_embedded", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("raw_reason", sa.String(length=32), nullable=True),
        sa.Column("value_count", sa.Integer(), nullable=False),
        sa.Column("report_count", sa.Integer(), nullable=False),
        sa.Column("skipped_items", sa.Integer(), nullable=False),
        sa.Column("unparsed_blocks", sa.Integer(), nullable=False),
        sa.Column("duplicate_keys", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol",
            "page",
            "observed_date",
            name="uq_fnguide_snapshots_symbol_page_date",
        ),
        sa.CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_fnguide_snapshots_symbol"),
        sa.CheckConstraint(
            "page IN ('Consensus', 'Invest', 'Finance')",
            name="ck_fnguide_snapshots_page",
        ),
        sa.CheckConstraint(
            "(status = 'parsed' AND value_count > 0)"
            " OR (status = 'no_values' AND reason IS NOT NULL AND value_count = 0)",
            name="ck_fnguide_snapshots_status_reason",
        ),
        sa.CheckConstraint(
            "raw_embedded IS NOT NULL OR raw_reason IS NOT NULL",
            name="ck_fnguide_snapshots_raw_or_reason",
        ),
        sa.CheckConstraint(
            "value_count >= 0 AND report_count >= 0 AND skipped_items >= 0"
            " AND unparsed_blocks >= 0 AND duplicate_keys >= 0",
            name="ck_fnguide_snapshots_counts",
        ),
    )
    op.create_table(
        "fnguide_values",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("fnguide_snapshots.id"),
            nullable=False,
        ),
        sa.Column("section", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.Text(), nullable=False),
        sa.Column("item_code", sa.String(length=20), server_default="", nullable=False),
        sa.Column("item_qualifier", sa.Text(), server_default="", nullable=False),
        sa.Column("column_key", sa.String(length=32), nullable=False),
        sa.Column("column_label", sa.Text(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_reason", sa.String(length=16), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("digit", sa.SmallInteger(), nullable=True),
        sa.UniqueConstraint(
            "snapshot_id",
            "section",
            "item_name",
            "item_code",
            "item_qualifier",
            "column_key",
            name="uq_fnguide_values_natural",
        ),
        sa.CheckConstraint(
            "(value_text IS NULL) <> (value_reason IS NULL)",
            name="ck_fnguide_values_text_or_reason",
        ),
        sa.CheckConstraint(
            "value_text IS NULL OR value_text ~ '^-?[0-9]+(\\.[0-9]+)?$'",
            name="ck_fnguide_values_exact_decimal",
        ),
        sa.CheckConstraint(
            "value_reason IS NULL OR value_reason IN ('missing', 'unparsable')",
            name="ck_fnguide_values_reason",
        ),
    )
    op.create_table(
        "fnguide_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("fnguide_snapshots.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("broker", sa.Text(), nullable=True),
        sa.Column("analyst", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "symbol", "observed_date", "report_hash", name="uq_fnguide_reports_natural"
        ),
    )
    # hk 6508 — conditional least-privilege grants for the app role (hard rule 18).
    # expand-contract: contract-step 6508
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON public.fnguide_snapshots TO at_app;
                GRANT SELECT, INSERT ON public.fnguide_values TO at_app;
                GRANT SELECT, INSERT ON public.fnguide_reports TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE public.fnguide_snapshots_id_seq TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE public.fnguide_values_id_seq TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE public.fnguide_reports_id_seq TO at_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # hk 6508 — refuse rather than drop collected history.
    # expand-contract: contract-step 6508
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.fnguide_snapshots)
               OR EXISTS (SELECT 1 FROM public.fnguide_values)
               OR EXISTS (SELECT 1 FROM public.fnguide_reports) THEN
                RAISE EXCEPTION
                    'fnguide tables have rows; downgrade would discard collected history';
            END IF;
        END
        $$
        """
    )
    # expand-contract: contract-step 6508
    op.drop_table("fnguide_reports")
    # expand-contract: contract-step 6508
    op.drop_table("fnguide_values")
    # expand-contract: contract-step 6508
    op.drop_table("fnguide_snapshots")
