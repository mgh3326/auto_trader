from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.sql.elements import TextClause

from app.services.account_value.flows import ExternalFlow, record_flow
from tests.services.account_value.test_snapshot import Session


@pytest.mark.asyncio
async def test_only_account_external_flows_are_allowed():
    session = Session()
    account = "upbit:upbit:" + "a" * 64
    await record_flow(
        session,
        ExternalFlow(account, "account", "KRW", Decimal(10), date(2026, 1, 1), uuid4()),
    )
    assert session.commits == 0
    with pytest.raises(ValueError):
        await record_flow(
            session,
            ExternalFlow(
                account, "crypto", "KRW", Decimal(10), date(2026, 1, 1), uuid4()
            ),
        )


@pytest.mark.asyncio
async def test_retry_id_is_idempotent_and_conflicting_reuse_is_rejected():
    from types import SimpleNamespace

    class Session:
        def __init__(self):
            self.rows = {}

        async def execute(self, statement, params=None):
            if isinstance(statement, TextClause):
                return SimpleNamespace()
            params = statement.compile().params
            if statement.is_insert:
                assert "clock_timestamp()" in str(statement)
                key = params["flow_id"]
                if key in self.rows:
                    return SimpleNamespace(scalar_one_or_none=lambda: None)
                self.rows[key] = SimpleNamespace(**params)
                return SimpleNamespace(scalar_one_or_none=lambda: 1)
            key = next(iter(params.values()))
            return SimpleNamespace(scalar_one=lambda: self.rows[key])

        async def commit(self):
            pass

        async def rollback(self):
            pass

    session = Session()
    account = "upbit:upbit:" + "a" * 64
    key = uuid4()
    flow = ExternalFlow(account, "account", "KRW", Decimal(10), date(2026, 1, 1), key)
    assert await record_flow(session, flow, commit=True) == "inserted"
    assert await record_flow(session, flow, commit=True) == "already_present"
    with pytest.raises(ValueError, match="different flow"):
        await record_flow(
            session,
            ExternalFlow(account, "account", "KRW", Decimal(11), flow.day, key),
            commit=True,
        )
    equal_second = ExternalFlow(
        account, "account", "KRW", Decimal(10), flow.day, uuid4()
    )
    assert await record_flow(session, equal_second, commit=True) == "inserted"
    assert len(session.rows) == 2
