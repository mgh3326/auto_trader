"""add_dca_plans_and_steps

Revision ID: add_dca_plans_and_steps
Revises: c2f3e4b5d6e7
Create Date: 2026-02-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "add_dca_plans_and_steps"
down_revision: Union[str, Sequence[str], None] = "c2f3e4b5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create DCA plans and steps tables."""
    # Create dca_plans table
    op.create_table(
        "dca_plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.String(50), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("splits", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active", "completed", "cancelled", "expired", name="dca_plan_status"
            ),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rsi_14", sa.Numeric(5, 2), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dca_plans")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_dca_plans_user_id_users"),
            ondelete="CASCADE",
        ),
    )

    # Create indexes for dca_plans
    op.create_index(
        op.f("ix_dca_plans_user_status"), "dca_plans", ["user_id", "status"]
    )
    op.create_index(op.f("ix_dca_plans_symbol"), "dca_plans", ["symbol"])

    # Create dca_plan_steps table
    op.create_table(
        "dca_plan_steps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("step_number", sa.BigInteger(), nullable=False),
        sa.Column("target_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("target_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("target_quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ordered",
                "partial",
                "filled",
                "cancelled",
                "skipped",
                name="dca_step_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("filled_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("filled_quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("filled_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column("ordered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("level_source", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dca_plan_steps")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["dca_plans.id"],
            name=op.f("fk_dca_plan_steps_plan_id_dca_plans"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("plan_id", "step_number", name=op.f("uq_dca_plan_step")),
    )

    # Create indexes for dca_plan_steps
    op.create_index(op.f("ix_dca_plan_steps_plan_id"), "dca_plan_steps", ["plan_id"])
    op.create_index(op.f("ix_dca_plan_steps_order_id"), "dca_plan_steps", ["order_id"])


def downgrade() -> None:
    """Drop DCA plans and steps tables."""
    # Drop indexes first
    op.drop_index(op.f("ix_dca_plan_steps_order_id"), table_name="dca_plan_steps")
    op.drop_index(op.f("ix_dca_plan_steps_plan_id"), table_name="dca_plan_steps")
    op.drop_index(op.f("ix_dca_plans_symbol"), table_name="dca_plans")
    op.drop_index(op.f("ix_dca_plans_user_status"), table_name="dca_plans")

    # Drop tables
    op.drop_table("dca_plan_steps")
    op.drop_table("dca_plans")

    # Drop enum types to prevent Postgres conflicts on re-migration
    op.execute("DROP TYPE IF EXISTS dca_step_status")
    op.execute("DROP TYPE IF EXISTS dca_plan_status")
