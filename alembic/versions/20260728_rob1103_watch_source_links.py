"""ROB-1103 make direct-watch source links honest and protect real links.

Revision ID: 20260728_rob1103_watch_links
Revises: 20260727_alpaca_paper_lab
Create Date: 2026-07-28 12:00:00.000000

Direct watches do not own an investment report/item. ROB-768 filled the
non-null source columns with deterministic UUIDv5 placeholders, which looked
like report links but could never match the UUIDv4 report table. Make those
links nullable for direct watches and add NOT VALID foreign keys to the alert
projection. NOT VALID preserves the legacy UUIDv5 rows and the ROB-413 smoke
orphan while enforcing referential integrity for every new non-null link.

Legacy formula (kept here for audit, not reused):
namespace ``7d85169b-7e5d-4d53-87eb-1bb7ba8ecf60`` with names
``report:<idempotency_key>`` and ``item:<idempotency_key>``.

Event rows deliberately keep nullable, non-FK audit links: their immutable
snapshot must survive deletion of the alert/report/item.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_rob1103_watch_links"
down_revision: str | Sequence[str] | None = "20260727_alpaca_paper_lab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_ALERTS = "investment_watch_alerts"
_EVENTS = "investment_watch_events"
_REPORT_FK = "fk_investment_watch_alerts_source_report_uuid"
_ITEM_FK = "fk_investment_watch_alerts_source_item_uuid"


def upgrade() -> None:
    for table in (_ALERTS, _EVENTS):
        op.alter_column(
            table,
            "source_report_uuid",
            existing_type=sa.Uuid(),
            nullable=True,
            schema=_SCHEMA,
        )
        op.alter_column(
            table,
            "source_item_uuid",
            existing_type=sa.Uuid(),
            nullable=True,
            schema=_SCHEMA,
        )

    op.execute(
        f"""
        ALTER TABLE {_SCHEMA}.{_ALERTS}
        ADD CONSTRAINT {_REPORT_FK}
        FOREIGN KEY (source_report_uuid)
        REFERENCES {_SCHEMA}.investment_reports (report_uuid)
        ON DELETE SET NULL
        NOT VALID
        """
    )
    op.execute(
        f"""
        ALTER TABLE {_SCHEMA}.{_ALERTS}
        ADD CONSTRAINT {_ITEM_FK}
        FOREIGN KEY (source_item_uuid)
        REFERENCES {_SCHEMA}.investment_report_items (item_uuid)
        ON DELETE SET NULL
        NOT VALID
        """
    )


def downgrade() -> None:
    op.drop_constraint(_ITEM_FK, _ALERTS, schema=_SCHEMA, type_="foreignkey")
    op.drop_constraint(_REPORT_FK, _ALERTS, schema=_SCHEMA, type_="foreignkey")

    for table in (_EVENTS, _ALERTS):
        op.alter_column(
            table,
            "source_item_uuid",
            existing_type=sa.Uuid(),
            nullable=False,
            schema=_SCHEMA,
        )
        op.alter_column(
            table,
            "source_report_uuid",
            existing_type=sa.Uuid(),
            nullable=False,
            schema=_SCHEMA,
        )
