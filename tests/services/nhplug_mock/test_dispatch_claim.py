"""Round-3 tester repros (#711): one committed row -> exactly one send, exactly
the committed body.

Each scenario below reproduced a double send or a quantity drift against the
retired in-process intent (5a3c54f).  With the durable claim, every one must
produce exactly one broker order request whose body equals the committed row.
The mutant harness restores each old weakness and shows these tests go red.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from app.core.db import AsyncSessionLocal
from app.services.brokers.nhplug.contracts import DryRunConfirmContract, ExpectedOrder
from app.services.brokers.nhplug.errors import NHPlugMockClaimRejected
from app.services.nhplug_mock import operations
from app.services.nhplug_mock.ledger_service import NHPlugMockLedgerService
from tests.services.nhplug_mock._fake_broker import MOCK_ACCOUNT, FakeNHMockBroker

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONFIRMED = DryRunConfirmContract(dry_run=False, confirm=True)
CREDENTIALS = operations.NHPlugMockCredentials(
    app_key="test-key", app_secret="test-secret", account_no=MOCK_ACCOUNT
)
COMMITTED = ExpectedOrder("place", "005930", "buy", 1, 50000, None)
COMMITTED_BODY = {
    "act_no": MOCK_ACCOUNT,
    "iem_cd": "005930",
    "orr_qty": 1,
    "orr_pr": 50000,
    "nmn_pr_tp_cd": "01",
    "orr_cnd_dit_cd": "00",
    "ssl_nmn_pr_dit_cd": "00",
    "rmt_mkt_cd": "KRX",
    "sor_mkt_sli_yn": "N",
}


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


async def _tokens() -> str:
    return "unit-test-token"


async def _client(broker: FakeNHMockBroker) -> Any:
    return await operations.open_verified_client(
        CREDENTIALS, token_provider=_tokens, transport=broker.transport
    )


async def _committed_row(ledger: NHPlugMockLedgerService) -> Any:
    return await ledger.record_submitting(
        order_date=f"3{uuid.uuid4().int % 10_000_000:07d}",
        operation_kind="place",
        symbol="005930",
        side="buy",
        quantity=1,
        price=50000,
    )


async def _dispatch(
    client: Any,
    ledger: NHPlugMockLedgerService,
    row: Any,
    *,
    client_request_id: str | None = None,
    expected: ExpectedOrder = COMMITTED,
) -> Any:
    return await client.dispatch_claimed_order(
        ledger=ledger,
        ledger_row_id=row.id,
        client_request_id=client_request_id or str(row.client_request_id),
        expected=expected,
        authorization=CONFIRMED,
    )


def _assert_exactly_one_committed_send(broker: FakeNHMockBroker) -> None:
    orders = [r for r in broker.requests if "/order/" in r.url.path]
    assert len(orders) == 1, [r.url.path for r in orders]
    assert orders[0].url.path == "/krstock/order/v1/cashBuy"
    assert json.loads(orders[0].content)["Input_0"] == COMMITTED_BODY


async def test_altered_quantity_clone_cannot_send_a_drifted_body(
    db_session: Any, armed: None
) -> None:
    """Repro 1: the committed row says 1; an attempt to send 2 sends nothing."""

    ledger = NHPlugMockLedgerService(db_session)
    broker = FakeNHMockBroker()
    client = await _client(broker)
    row = await _committed_row(ledger)
    for drifted in (
        ExpectedOrder("place", "005930", "buy", 2, 50000, None),
        ExpectedOrder("place", "005930", "buy", 1, 50100, None),
    ):
        with pytest.raises(NHPlugMockClaimRejected):
            await _dispatch(client, ledger, row, expected=drifted)
    await _dispatch(client, ledger, row)
    _assert_exactly_one_committed_send(broker)


async def test_replay_with_a_new_client_request_id_sends_once(
    db_session: Any, armed: None
) -> None:
    """Repro 2: after one send, a replay (new or same request id) sends nothing."""

    ledger = NHPlugMockLedgerService(db_session)
    broker = FakeNHMockBroker()
    client = await _client(broker)
    row = await _committed_row(ledger)
    await _dispatch(client, ledger, row)
    for request_id in (str(uuid.uuid4()), str(row.client_request_id)):
        with pytest.raises(NHPlugMockClaimRejected):
            await _dispatch(client, ledger, row, client_request_id=request_id)
    _assert_exactly_one_committed_send(broker)


async def test_a_second_client_cannot_send_the_same_row(
    db_session: Any, armed: None
) -> None:
    """Repro 3: a second verified client (and a second session) sends nothing."""

    ledger = NHPlugMockLedgerService(db_session)
    broker = FakeNHMockBroker()
    first = await _client(broker)
    second = await _client(broker)
    row = await _committed_row(ledger)
    await _dispatch(first, ledger, row)
    async with AsyncSessionLocal() as other_session:
        other_ledger = NHPlugMockLedgerService(other_session)
        with pytest.raises(NHPlugMockClaimRejected):
            await _dispatch(second, other_ledger, row)
    _assert_exactly_one_committed_send(broker)


@pytest.mark.parametrize("clients", ("same_client", "two_clients"))
async def test_concurrent_dispatches_send_exactly_once(
    db_session: Any, armed: None, clients: str
) -> None:
    """Repro 4: racing dispatches of one row; the row lock admits exactly one."""

    ledger = NHPlugMockLedgerService(db_session)
    broker = FakeNHMockBroker()
    first = await _client(broker)
    second = first if clients == "same_client" else await _client(broker)
    row = await _committed_row(ledger)
    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        results = await asyncio.gather(
            _dispatch(first, NHPlugMockLedgerService(s1), row),
            _dispatch(second, NHPlugMockLedgerService(s2), row),
            return_exceptions=True,
        )
    rejected = [r for r in results if isinstance(r, NHPlugMockClaimRejected)]
    sent = [r for r in results if isinstance(r, dict)]
    assert len(sent) == 1 and len(rejected) == 1, results
    _assert_exactly_one_committed_send(broker)


async def test_operations_retry_of_a_dispatched_row_never_resends(
    db_session: Any, armed: None
) -> None:
    """A rejected claim leaves the owning dispatch's row untouched."""

    ledger = NHPlugMockLedgerService(db_session)
    broker = FakeNHMockBroker()
    client = await _client(broker)
    row = await _committed_row(ledger)
    await _dispatch(client, ledger, row)
    before = await ledger.get(row.id)
    with pytest.raises(NHPlugMockClaimRejected):
        await _dispatch(client, ledger, row)
    after = await ledger.get(row.id)
    assert (after.status, after.claim_token) == (before.status, before.claim_token)
    _assert_exactly_one_committed_send(broker)
