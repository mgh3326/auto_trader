"""Validate the KR mover source CHECK without extending the ADD lock."""

# expand-contract: contract-step 6758
from alembic import op

revision = "20261126_hk6758_kr_mover_val"
down_revision = "20261126_hk6758_kr_mover_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # expand-contract: contract-step 6758
    op.execute(
        "ALTER TABLE invest_momentum_event_snapshots "
        "VALIDATE CONSTRAINT ck_invest_momentum_event_snapshots_source"
    )


def downgrade() -> None:
    pass
