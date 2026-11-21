"""hk 6026: Upbit market_event (유의/주의) 저장 + 관측 시각.

Revision ID: 20261122_hk6026_market_event
Revises: 20261121_hk5946_futures_mock
Create Date: 2026-11-22

Upbit 는 2024-11-20 부로 ``GET /v1/market/all?is_details=true`` 의
``market_warning`` 필드를 없애고 ``market_event: {warning: bool, caution: {<TYPE>:
bool}}`` 로 대체했다 (docs.upbit.com/kr/changelog/
mytrade_market_warning_deprecated_11_20, 필드 정의는
docs.upbit.com/kr/reference/list-trading-pairs).

추가형 마이그레이션이다 — 기존 열·행·CHECK 는 그대로 두고 두 열만 더한다.
백필은 없다: 과거 ``market_warning`` 값만으로는 주의(caution)를 알 수 없고,
관측 시각도 없으므로 값을 지어내지 않는다(리더는 ``unknown`` 으로 fail-closed).

- ``market_event`` JSONB NULL — 마지막 관측값
  ``{"warning": bool, "caution": {<TYPE>: bool}}``. Upbit 가 말하지 않았으면 NULL.
- ``market_event_observed_at`` TIMESTAMPTZ NULL — 그 값을 관측한 시각.
  신선도(``stale``)는 이 시각으로 판정하며 행의 ``updated_at`` 을 쓰지 않는다.

``at_app`` 권한: 기존 테이블 GRANT 가 열 추가를 덮으므로 새 GRANT 는 필요 없다.
Downgrade 는 두 열을 지우며 저장된 시장 경보 관측을 잃는다.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261122_hk6026_market_event"
down_revision: str | Sequence[str] | None = "20261121_hk5946_futures_mock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "upbit_symbol_universe"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "market_event",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "market_event_observed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "market_event_observed_at")
    op.drop_column(_TABLE, "market_event")
