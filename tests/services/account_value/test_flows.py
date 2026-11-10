from datetime import date
from decimal import Decimal

import pytest

from app.services.account_value.flows import ExternalFlow, record_flow
from tests.services.account_value.test_snapshot import Session


@pytest.mark.asyncio
async def test_only_account_external_flows_are_allowed():
    session = Session()
    account = "upbit:upbit:" + "a" * 64
    await record_flow(
        session, ExternalFlow(account, "account", "KRW", Decimal(10), date(2026, 1, 1))
    )
    assert session.commits == 0
    with pytest.raises(ValueError):
        await record_flow(
            session,
            ExternalFlow(account, "crypto", "KRW", Decimal(10), date(2026, 1, 1)),
        )
