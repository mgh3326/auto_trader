"""Add opt-in official event metadata and immutable source observations."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision = "20261126_hk6406_event_format"
down_revision = "20261125_hk6582_dart_capital"
branch_labels = None
depends_on = None

EVENT_COLUMNS = (
    ("published_at", sa.TIMESTAMP(timezone=True)),
    ("published_date", sa.Date()),
    ("published_offset", sa.Text()),
    ("published_precision", sa.Text()),
    ("revised_at", sa.TIMESTAMP(timezone=True)),
    ("effective_at", sa.TIMESTAMP(timezone=True)),
    ("relevance", sa.Text()),
    ("relevance_state", sa.Text()),
    ("relevance_basis", sa.Text()),
    ("instrument_type", sa.Text()),
    ("group_key", sa.Text()),
    ("redistributable", sa.Boolean()),
    ("symbols", ARRAY(sa.Text())),
    ("backfilled", sa.Boolean()),
)

REVISION_COLUMNS = (
    ("changed_fields", JSONB(none_as_null=True)),
    ("source_revision_id", sa.Text()),
)

SOURCE_COLUMNS = (
    ("source_url", sa.Text()),
    ("published_at", sa.TIMESTAMP(timezone=True)),
    ("published_date", sa.Date()),
    ("published_offset", sa.Text()),
    ("published_precision", sa.Text()),
    ("first_received_at", sa.TIMESTAMP(timezone=True)),
    ("revised_at", sa.TIMESTAMP(timezone=True)),
    ("raw_hash", sa.Text()),
)

PUBLICATION_CHECK = """
(published_precision IS NULL AND published_at IS NULL AND published_date IS NULL AND published_offset IS NULL)
OR (published_precision IS NOT NULL AND (
    (published_precision = 'date' AND published_at IS NULL AND published_date IS NOT NULL)
    OR (published_precision IN ('second', 'minute') AND published_at IS NOT NULL AND published_date IS NULL)
))
"""

EVENT_CHECKS = {
    "publication": PUBLICATION_CHECK,
    "instrument_type": "instrument_type IN ('equity_kr', 'equity_us', 'crypto', 'index', 'fx')",
    "relevance": "relevance IN ('direct', 'sector', 'market')",
    "relevance_state": "relevance_state IN ('known', 'unknown')",
    "relevance_basis": "relevance_basis IN ('recorded', 'inferred')",
}


def upgrade() -> None:
    for table, columns in (
        ("market_events", EVENT_COLUMNS),
        ("market_event_revisions", REVISION_COLUMNS),
    ):
        for name, column_type in columns:
            op.add_column(table, sa.Column(name, column_type))

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
        *(sa.Column(name, column_type) for name, column_type in SOURCE_COLUMNS),
        sa.UniqueConstraint(
            "source", "source_event_id", name="uq_market_event_sources_provider"
        ),
    )
    for table, checks in (
        ("market_events", EVENT_CHECKS),
        ("market_event_sources", {"publication": EVENT_CHECKS["publication"]}),
    ):
        for name, expression in checks.items():
            constraint = f"ck_{table}_{name}"
            op.create_check_constraint(
                op.f(constraint), table, expression, postgresql_not_valid=True
            )
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")

    op.execute("""
        CREATE FUNCTION reject_market_event_source_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'market_event_sources is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_market_event_sources_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON market_event_sources
        FOR EACH STATEMENT EXECUTE FUNCTION reject_market_event_source_mutation()
    """)
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
    event_data = " OR ".join(f"{name} IS NOT NULL" for name, _ in EVENT_COLUMNS)
    revision_data = " OR ".join(f"{name} IS NOT NULL" for name, _ in REVISION_COLUMNS)
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM market_event_sources)
                OR EXISTS (SELECT 1 FROM market_events WHERE {event_data})
                OR EXISTS (SELECT 1 FROM market_event_revisions WHERE {revision_data})
            THEN RAISE EXCEPTION 'hk 6406 downgrade would discard event format data';
            END IF;
        END $$
    """)
    op.drop_table("market_event_sources")
    op.execute("DROP FUNCTION reject_market_event_source_mutation()")
    for name in EVENT_CHECKS:
        op.drop_constraint(
            op.f(f"ck_market_events_{name}"), "market_events", type_="check"
        )
    for table, columns in (
        ("market_event_revisions", REVISION_COLUMNS),
        ("market_events", EVENT_COLUMNS),
    ):
        for name, _ in reversed(columns):
            op.drop_column(table, name)
