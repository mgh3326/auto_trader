"""Add opt-in official event metadata and immutable source observations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261126_hk6406_event_format"
down_revision = "20261125_hk6582_dart_capital"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "market_events",
        sa.Column("published_at", sa.TIMESTAMP(timezone=True)),
        if_not_exists=True,
    )
    op.add_column(
        "market_events", sa.Column("published_date", sa.Date()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("published_offset", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("published_precision", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events",
        sa.Column("revised_at", sa.TIMESTAMP(timezone=True)),
        if_not_exists=True,
    )
    op.add_column(
        "market_events",
        sa.Column("effective_at", sa.TIMESTAMP(timezone=True)),
        if_not_exists=True,
    )
    op.add_column(
        "market_events", sa.Column("relevance", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("relevance_state", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("relevance_basis", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("instrument_type", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("group_key", sa.Text()), if_not_exists=True
    )
    op.add_column(
        "market_events", sa.Column("redistributable", sa.Boolean()), if_not_exists=True
    )
    op.add_column(
        "market_events",
        sa.Column("symbols", postgresql.ARRAY(sa.Text())),
        if_not_exists=True,
    )
    op.add_column(
        "market_events", sa.Column("backfilled", sa.Boolean()), if_not_exists=True
    )
    op.add_column(
        "market_event_revisions",
        sa.Column("changed_fields", postgresql.JSONB(none_as_null=True)),
        if_not_exists=True,
    )
    op.add_column(
        "market_event_revisions",
        sa.Column("source_revision_id", sa.Text()),
        if_not_exists=True,
    )

    op.create_table(
        "market_event_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("market_events.id"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("published_date", sa.Date()),
        sa.Column("published_offset", sa.Text()),
        sa.Column("published_precision", sa.Text()),
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("revised_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("raw_hash", sa.Text()),
        sa.UniqueConstraint(
            "source", "source_event_id", name="uq_market_event_sources_provider"
        ),
    )
    op.create_check_constraint(
        op.f("ck_market_events_publication"),
        "market_events",
        "(published_precision IS NULL AND published_at IS NULL AND published_date IS NULL AND published_offset IS NULL)\nOR (published_precision IS NOT NULL AND (\n    (published_precision = 'date' AND published_at IS NULL AND published_date IS NOT NULL)\n    OR (published_precision IN ('second', 'minute') AND published_at IS NOT NULL AND published_date IS NULL)\n))",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_events VALIDATE CONSTRAINT ck_market_events_publication"
    )
    op.create_check_constraint(
        op.f("ck_market_events_instrument_type"),
        "market_events",
        "instrument_type IN ('equity_kr', 'equity_us', 'crypto', 'index', 'fx')",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_events VALIDATE CONSTRAINT ck_market_events_instrument_type"
    )
    op.create_check_constraint(
        op.f("ck_market_events_relevance"),
        "market_events",
        "relevance IN ('direct', 'sector', 'market')",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_events VALIDATE CONSTRAINT ck_market_events_relevance"
    )
    op.create_check_constraint(
        op.f("ck_market_events_relevance_state"),
        "market_events",
        "relevance_state IN ('known', 'unknown')",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_events VALIDATE CONSTRAINT ck_market_events_relevance_state"
    )
    op.create_check_constraint(
        op.f("ck_market_events_relevance_basis"),
        "market_events",
        "relevance_basis IN ('recorded', 'inferred')",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_events VALIDATE CONSTRAINT ck_market_events_relevance_basis"
    )
    op.create_check_constraint(
        op.f("ck_market_event_sources_publication"),
        "market_event_sources",
        "(published_precision IS NULL AND published_at IS NULL AND published_date IS NULL AND published_offset IS NULL)\nOR (published_precision IS NOT NULL AND (\n    (published_precision = 'date' AND published_at IS NULL AND published_date IS NOT NULL)\n    OR (published_precision IN ('second', 'minute') AND published_at IS NOT NULL AND published_date IS NULL)\n))",
        postgresql_not_valid=True,
    )
    # expand-contract: contract-step 6406
    op.execute(
        "ALTER TABLE market_event_sources VALIDATE CONSTRAINT ck_market_event_sources_publication"
    )

    # expand-contract: contract-step 6406
    op.execute("""
        CREATE FUNCTION reject_market_event_source_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'market_event_sources is append-only';
        END;
        $$
    """)
    # expand-contract: contract-step 6406
    op.execute("""
        CREATE TRIGGER trg_market_event_sources_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON market_event_sources
        FOR EACH STATEMENT EXECUTE FUNCTION reject_market_event_source_mutation()
    """)
    # expand-contract: contract-step 6406
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON market_event_sources TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE market_event_sources_id_seq TO at_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE market_events, market_event_revisions, market_event_sources "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM market_event_sources)
                OR EXISTS (SELECT 1 FROM market_events WHERE published_at IS NOT NULL
                OR published_date IS NOT NULL
                OR published_offset IS NOT NULL
                OR published_precision IS NOT NULL
                OR revised_at IS NOT NULL
                OR effective_at IS NOT NULL
                OR relevance IS NOT NULL
                OR relevance_state IS NOT NULL
                OR relevance_basis IS NOT NULL
                OR instrument_type IS NOT NULL
                OR group_key IS NOT NULL
                OR redistributable IS NOT NULL
                OR symbols IS NOT NULL
                OR backfilled IS NOT NULL)
                OR EXISTS (SELECT 1 FROM market_event_revisions WHERE changed_fields IS NOT NULL OR source_revision_id IS NOT NULL)
            THEN RAISE EXCEPTION 'hk 6406 downgrade would discard event format data';
            END IF;
        END $$
    """)
    op.drop_table("market_event_sources")
    op.execute("DROP FUNCTION reject_market_event_source_mutation()")
    op.drop_constraint(
        op.f("ck_market_events_publication"), "market_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_market_events_instrument_type"), "market_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_market_events_relevance"), "market_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_market_events_relevance_state"), "market_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_market_events_relevance_basis"), "market_events", type_="check"
    )
    op.drop_column("market_event_revisions", "source_revision_id")
    op.drop_column("market_event_revisions", "changed_fields")
    op.drop_column("market_events", "backfilled")
    op.drop_column("market_events", "symbols")
    op.drop_column("market_events", "redistributable")
    op.drop_column("market_events", "group_key")
    op.drop_column("market_events", "instrument_type")
    op.drop_column("market_events", "relevance_basis")
    op.drop_column("market_events", "relevance_state")
    op.drop_column("market_events", "relevance")
    op.drop_column("market_events", "effective_at")
    op.drop_column("market_events", "revised_at")
    op.drop_column("market_events", "published_precision")
    op.drop_column("market_events", "published_offset")
    op.drop_column("market_events", "published_date")
    op.drop_column("market_events", "published_at")
