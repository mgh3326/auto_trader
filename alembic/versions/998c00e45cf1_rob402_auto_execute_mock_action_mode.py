"""rob402 auto_execute_mock action_mode and outcome

Revision ID: 998c00e45cf1
Revises: 9ef8171137c3
"""

from alembic import op

revision = "998c00e45cf1"
down_revision = "9ef8171137c3"
branch_labels = None
depends_on = None

_S = "review"
_NEW_MODE = (
    "action_mode IN ('notify_only','preview_only','approval_required',"
    "'auto_execute_mock')"
)
_OLD_MODE = "action_mode IN ('notify_only','preview_only','approval_required')"
_MODE_TABLES = (
    ("investment_watch_alerts", "ck_investment_watch_alerts_action_mode"),
    ("investment_watch_events", "ck_investment_watch_events_action_mode"),
)

_NEW_OUTCOME = (
    "outcome IN ('notified','review_required','preview_attached',"
    "'executed','expired','ignored','failed')"
)
_OLD_OUTCOME = (
    "outcome IN ('notified','review_required','preview_attached',"
    "'expired','ignored','failed')"
)


def upgrade() -> None:
    for table, name in _MODE_TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _NEW_MODE, schema=_S)
    op.drop_constraint("ck_investment_watch_events_outcome", "investment_watch_events", schema=_S, type_="check")
    op.create_check_constraint("ck_investment_watch_events_outcome", "investment_watch_events", _NEW_OUTCOME, schema=_S)


def downgrade() -> None:
    for table, name in _MODE_TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _OLD_MODE, schema=_S)
    op.drop_constraint("ck_investment_watch_events_outcome", "investment_watch_events", schema=_S, type_="check")
    op.create_check_constraint("ck_investment_watch_events_outcome", "investment_watch_events", _OLD_OUTCOME, schema=_S)
