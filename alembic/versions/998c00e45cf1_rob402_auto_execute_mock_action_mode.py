"""rob402 auto_execute_mock action_mode

Revision ID: 998c00e45cf1
Revises: 9ef8171137c3
"""

from alembic import op

revision = "998c00e45cf1"
down_revision = "9ef8171137c3"
branch_labels = None
depends_on = None

_S = "review"
_NEW = (
    "action_mode IN ('notify_only','preview_only','approval_required',"
    "'auto_execute_mock')"
)
_OLD = "action_mode IN ('notify_only','preview_only','approval_required')"
_TABLES = (
    ("investment_watch_alerts", "ck_investment_watch_alerts_action_mode"),
    ("investment_watch_events", "ck_investment_watch_events_action_mode"),
)


def upgrade() -> None:
    for table, name in _TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _NEW, schema=_S)


def downgrade() -> None:
    for table, name in _TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _OLD, schema=_S)
