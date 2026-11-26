"""Validate the expanded mover source CHECK without holding ADD's strong lock."""

from alembic import op

revision = "20261126_hk6663_us_mover_sources_val"
down_revision = "20261126_hk6663_us_mover_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE invest_momentum_event_snapshots "
        "VALIDATE CONSTRAINT ck_invest_momentum_event_snapshots_source"
    )


def downgrade() -> None:
    pass
