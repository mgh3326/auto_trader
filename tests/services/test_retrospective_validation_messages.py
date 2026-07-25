from __future__ import annotations

import pytest

from app.services.trade_journal.trade_retrospective_service import (
    RetrospectiveValidationError,
    save_retrospective,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_invalid_root_cause_class_message_lists_valid_values(db_session):
    with pytest.raises(RetrospectiveValidationError) as exc:
        await save_retrospective(
            db_session,
            symbol="AAPL",
            instrument_type="equity_us",
            account_mode="toss_live",
            outcome="filled",
            root_cause_class="process_error",
        )
    msg = str(exc.value)
    assert "process_error" in msg
    assert "execution" in msg and "harness" in msg  # enumerates the valid set


async def test_invalid_trigger_type_message_lists_valid_values(db_session):
    with pytest.raises(RetrospectiveValidationError) as exc:
        await save_retrospective(
            db_session,
            symbol="AAPL",
            instrument_type="equity_us",
            account_mode="toss_live",
            outcome="filled",
            trigger_type="bogus",
        )
    msg = str(exc.value)
    assert "bogus" in msg
    assert "fill" in msg and "guardrail_block" in msg
