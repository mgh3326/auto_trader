"""ROB-402 — WatchOrderIntentLedger ORM mirrors migration daf4130b13ce."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger


def _row(**over):
    base = {
        "correlation_id": f"corr-{uuid4().hex}",
        "idempotency_key": f"idem-{uuid4().hex}",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "condition_type": "below",
        "threshold": Decimal("55000"),
        "threshold_key": "55000",
        "action": "auto_execute_mock",
        "side": "buy",
        "account_mode": "kis_mock",
        "execution_source": "watch",
        "lifecycle_state": "previewed",
        "preview_line": {"symbol": "005930", "side": "buy"},
        "kst_date": "2026-06-01",
    }
    base.update(over)
    return WatchOrderIntentLedger(**base)


@pytest.mark.asyncio
async def test_intent_row_inserts(db_session: AsyncSession):
    row = _row()
    db_session.add(row)
    await db_session.commit()
    assert row.id is not None
    assert row.execution_allowed is False  # server default


@pytest.mark.asyncio
async def test_intent_account_mode_check_blocks_live(db_session: AsyncSession):
    db_session.add(_row(account_mode="kis_live"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
