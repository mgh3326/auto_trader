"""hk 6513: Benzinga calendar evidence columns + append-only revision log.

Revision ID: 20261125_hk6513_benzinga_calendars
Revises: 20261125_hk6322_us_venue_val
Create Date: 2026-11-25

Additive. Two things are added for the Benzinga calendar ingestion
(``docs/agent-contracts/market-data-and-screener-contracts.md``, section
"Benzinga calendar ingestion"):

* five nullable ``market_events`` columns — the provider's own timestamps kept
  as given (``provider_created_raw``/``provider_updated_raw``) and as aware UTC
  (``provider_created_at``/``provider_updated_at``), plus our first-observation
  clock ``first_received_at``. No backfill: an older row was not observed by
  this writer, so inventing a value would be a lie.
* ``market_event_revisions`` — an append-only log of every distinct provider
  version of a record, keyed by ``source_event_id``. A revised record becomes a
  new row instead of silently overwriting the previous state. Uniqueness is by
  (source, category, market, source_event_id, revision_seq) and by the same key
  plus ``content_hash``, so re-ingesting an unchanged payload inserts nothing.

No category/source CHECK exists on ``market_events`` (the taxonomy is code), so
the new values need no constraint change.

``at_app`` grant (hard rule 18): SELECT, INSERT on the new table and USAGE,
SELECT on its sequence, granted only when the role exists. The ``market_events``
column grant is covered by the table-level grant.

``downgrade`` refuses while any revision row exists or any ``market_events`` row
carries one of the new columns — dropping them would lose provider evidence
that no other store keeps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261125_hk6513_benzinga_calendars"
down_revision: str | Sequence[str] | None = "20261125_hk6322_us_venue_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS = (
    ("provider_created_at", sa.TIMESTAMP(timezone=True)),
    ("provider_created_raw", sa.Text()),
    ("provider_updated_at", sa.TIMESTAMP(timezone=True)),
    ("provider_updated_raw", sa.Text()),
    ("first_received_at", sa.TIMESTAMP(timezone=True)),
)


def upgrade() -> None:
    for name, column_type in _NEW_COLUMNS:
        op.add_column("market_events", sa.Column(name, column_type, nullable=True))

    op.create_table(
        "market_event_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("revision_seq", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "provider_created_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "provider_updated_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "raw_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_event_id <> ''",
            name="ck_market_event_revisions_source_event_id_not_empty",
        ),
        sa.UniqueConstraint(
            "source",
            "category",
            "market",
            "source_event_id",
            "revision_seq",
            name="uq_market_event_revisions_seq",
        ),
        sa.UniqueConstraint(
            "source",
            "category",
            "market",
            "source_event_id",
            "content_hash",
            name="uq_market_event_revisions_hash",
        ),
    )
    op.create_index(
        "ix_market_event_revisions_key",
        "market_event_revisions",
        ["source", "category", "market", "source_event_id"],
    )

    # Stage-4 role may not exist in dev/CI databases — conditional GRANT.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT SELECT, INSERT ON market_event_revisions TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE "
        "market_event_revisions_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    revision_rows = op.get_bind().execute(
        sa.text("SELECT count(*) FROM market_event_revisions")
    ).scalar_one()
    if revision_rows:
        raise RuntimeError(
            "refusing to downgrade: market_event_revisions holds "
            f"{revision_rows} provider revision row(s)"
        )
    observed_rows = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM market_events WHERE first_received_at IS NOT NULL "
            "OR provider_created_at IS NOT NULL OR provider_updated_at IS NOT NULL "
            "OR provider_created_raw IS NOT NULL "
            "OR provider_updated_raw IS NOT NULL"
        )
    ).scalar_one()
    if observed_rows:
        raise RuntimeError(
            "refusing to downgrade: market_events holds "
            f"{observed_rows} row(s) with provider evidence columns"
        )

    op.drop_index(
        "ix_market_event_revisions_key", table_name="market_event_revisions"
    )
    op.drop_table("market_event_revisions")
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column("market_events", name)
