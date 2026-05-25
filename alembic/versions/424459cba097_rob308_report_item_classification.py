"""rob308 report item classification

Revision ID: 424459cba097
Revises: 38d4f86503b1
Create Date: 2026-05-24 17:43:46.866606

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '424459cba097'
down_revision: Union[str, Sequence[str], None] = '38d4f86503b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns to review.investment_report_items
    op.add_column('investment_report_items', sa.Column('decision_bucket', sa.Text(), nullable=True), schema='review')
    op.add_column('investment_report_items', sa.Column('cited_symbol_report_uuid', sa.UUID(), nullable=True), schema='review')
    op.add_column('investment_report_items', sa.Column('cited_dimension_report_uuids', sa.ARRAY(sa.UUID()), server_default=sa.text('ARRAY[]::uuid[]'), nullable=False), schema='review')
    
    # Add CheckConstraint
    op.create_check_constraint(
        'ck_investment_report_items_decision_bucket',
        'investment_report_items',
        "decision_bucket IS NULL OR decision_bucket IN ('new_buy_candidate','open_action','completed_or_existing','deferred_no_action','risk_watch')",
        schema='review'
    )


def downgrade() -> None:
    # Drop CheckConstraint (handle both name variations defensively)
    op.execute('ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "ck_investment_report_items_ck_investment_report_items_decision_bucket"')
    op.execute('ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "ck_investment_report_items_decision_bucket"')
    
    # Drop columns
    op.drop_column('investment_report_items', 'cited_dimension_report_uuids', schema='review')
    op.drop_column('investment_report_items', 'cited_symbol_report_uuid', schema='review')
    op.drop_column('investment_report_items', 'decision_bucket', schema='review')
