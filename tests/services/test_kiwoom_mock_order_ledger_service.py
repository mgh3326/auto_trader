"""hk 4712 (3797 T-P2) — Kiwoom mock order ledger service (unit, no database).

A small in-memory session stands in for ``AsyncSession`` so idempotency, payload
conflict and the writer-only static guard run without a database. The real
UNIQUE/CHECK behaviour is proven by the CI-only sibling
``test_kiwoom_mock_order_ledger_service_db.py``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.review import KiwoomMockOrderLedger
from app.services.kiwoom_mock_order_ledger_service import (
    KiwoomMockLedgerPayloadConflict,
    KiwoomMockOrderLedgerService,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    """Just enough of ``AsyncSession`` for ``KiwoomMockOrderLedgerService``."""

    def __init__(self) -> None:
        self.store: dict[str, KiwoomMockOrderLedger] = {}
        self.pending: list[KiwoomMockOrderLedger] = []
        self.adds = 0
        self.commits = 0
        self.rollbacks = 0
        # A winner that "appears" between the lookup and the flush (UNIQUE race).
        self.race_winner: KiwoomMockOrderLedger | None = None

    async def execute(self, stmt):
        params = stmt.compile().params
        client_order_id = next(iter(params.values()))
        return _Result(self.store.get(client_order_id))

    def add(self, row) -> None:
        self.adds += 1
        self.pending.append(row)

    async def flush(self) -> None:
        if self.race_winner is not None:
            winner, self.race_winner = self.race_winner, None
            self.store[winner.client_order_id] = winner
            self.pending.clear()
            raise IntegrityError("INSERT", {}, Exception("uq client_order_id"))
        for row in self.pending:
            row.id = len(self.store) + 1

    async def commit(self) -> None:
        self.commits += 1
        for row in self.pending:
            self.store[row.client_order_id] = row
        self.pending.clear()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.pending.clear()

    async def refresh(self, row) -> None:  # noqa: ARG002
        return None


def _send_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "status": "accepted",
        "client_order_id": "kwoomm-0123456789abcdef",
        "idempotency_key": "kwoomm-0123456789abcdef",
        "symbol": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "broker_order_id": "0000111222",
        "response_code": "0",
        "response_message": "정상",
        "raw_response": {"return_code": 0, "ord_no": "0000111222"},
        "trading_day": "20261116",
    }
    kwargs.update(overrides)
    return kwargs


def _uncertain_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs = _send_kwargs(**overrides)
    kwargs.pop("status")
    return kwargs


@pytest.mark.asyncio
async def test_record_send_accepted_writes_the_designed_row():
    session = FakeSession()

    row = await KiwoomMockOrderLedgerService(session).record_send(**_send_kwargs())

    assert session.commits == 1 and session.adds == 1
    assert row.status == "accepted"
    assert row.lifecycle_state == "accepted"
    assert row.broker_order_id == "0000111222"
    assert (row.account_mode, row.broker, row.market) == (
        "kiwoom_mock",
        "kiwoom",
        "kr",
    )
    assert (row.instrument_type, row.currency, row.order_type) == (
        "equity_kr",
        "KRW",
        "limit",
    )
    assert row.operation_kind == "place"
    assert row.trading_day == "20261116"
    assert (row.quantity, row.price, row.amount) == (
        Decimal("3"),
        Decimal("70000"),
        Decimal("210000"),
    )
    assert row.symbol == row.raw_symbol == "005930"


@pytest.mark.asyncio
async def test_record_send_rejected_has_no_broker_order_id():
    session = FakeSession()

    row = await KiwoomMockOrderLedgerService(session).record_send(
        **_send_kwargs(
            status="rejected",
            broker_order_id=None,
            response_code="20",
            raw_response={"return_code": 20},
        )
    )

    assert row.status == "rejected"
    assert row.lifecycle_state == "failed"
    assert row.broker_order_id is None


@pytest.mark.asyncio
async def test_accepted_without_a_broker_order_id_is_never_written():
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)

    for missing in (None, ""):
        with pytest.raises(ValueError):
            await service.record_send(**_send_kwargs(broker_order_id=missing))

    assert session.adds == 0 and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uncertain", "filled", "pending", ""])
async def test_record_send_only_takes_accepted_or_rejected(status):
    session = FakeSession()

    with pytest.raises(ValueError):
        await KiwoomMockOrderLedgerService(session).record_send(
            **_send_kwargs(status=status)
        )

    assert session.adds == 0


@pytest.mark.asyncio
async def test_record_uncertain_keeps_an_unknown_outcome_without_an_order_id():
    session = FakeSession()

    row = await KiwoomMockOrderLedgerService(session).record_uncertain(
        **_uncertain_kwargs(broker_order_id=None, response_code=None)
    )

    assert row.status == "uncertain"
    assert row.lifecycle_state == "submitted"
    assert row.broker_order_id is None


@pytest.mark.asyncio
async def test_record_send_is_idempotent_on_client_order_id():
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)

    first = await service.record_send(**_send_kwargs())
    second = await service.record_send(**_send_kwargs())

    assert second is first
    assert session.adds == 1 and session.commits == 1


@pytest.mark.asyncio
async def test_record_uncertain_is_idempotent_on_client_order_id():
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)

    first = await service.record_uncertain(**_uncertain_kwargs(broker_order_id=None))
    second = await service.record_uncertain(**_uncertain_kwargs(broker_order_id=None))

    assert second is first
    assert session.adds == 1


@pytest.mark.asyncio
async def test_a_replay_returns_the_first_outcome_and_never_rewrites_it():
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)

    first = await service.record_uncertain(**_uncertain_kwargs(broker_order_id=None))
    replay = await service.record_send(**_send_kwargs())

    assert replay is first
    assert replay.status == "uncertain"  # I3: only a broker read may resolve it
    assert replay.broker_order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"price": 70100}, "price"),
        ({"quantity": 4}, "quantity"),
        ({"side": "sell"}, "side"),
        ({"symbol": "000660"}, "symbol"),
        ({"trading_day": "20261117"}, "trading_day"),
    ],
)
async def test_same_key_with_a_different_payload_is_a_conflict(override, field):
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)
    await service.record_send(**_send_kwargs())

    with pytest.raises(KiwoomMockLedgerPayloadConflict) as excinfo:
        await service.record_send(**_send_kwargs(**override))

    assert field in excinfo.value.differing
    assert session.adds == 1


@pytest.mark.asyncio
async def test_get_status_reads_the_recorded_status_or_none():
    session = FakeSession()
    service = KiwoomMockOrderLedgerService(session)

    assert await service.get_status("kwoomm-0123456789abcdef") is None
    await service.record_send(**_send_kwargs(status="rejected", broker_order_id=None))

    assert await service.get_status("kwoomm-0123456789abcdef") == "rejected"
    assert session.adds == 1  # the read wrote nothing


@pytest.mark.asyncio
async def test_a_unique_race_replays_the_winner():
    session = FakeSession()
    winner_session = FakeSession()
    winner = await KiwoomMockOrderLedgerService(winner_session).record_send(
        **_send_kwargs()
    )
    session.race_winner = winner

    row = await KiwoomMockOrderLedgerService(session).record_send(**_send_kwargs())

    assert row is winner
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_a_unique_race_with_a_different_payload_is_a_conflict():
    session = FakeSession()
    winner = await KiwoomMockOrderLedgerService(FakeSession()).record_send(
        **_send_kwargs(price=70100)
    )
    session.race_winner = winner

    with pytest.raises(KiwoomMockLedgerPayloadConflict):
        await KiwoomMockOrderLedgerService(session).record_send(**_send_kwargs())


@pytest.mark.asyncio
async def test_an_integrity_error_without_a_winner_is_not_swallowed():
    class NoWinnerSession(FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    with pytest.raises(IntegrityError):
        await KiwoomMockOrderLedgerService(NoWinnerSession()).record_send(
            **_send_kwargs()
        )


# --- I8: the service is the only writer (hard rule 5) -----------------------

_LEDGER_REFERENCE = re.compile(
    r"KiwoomMockOrderLedger\b|kiwoom_mock_order_ledger(?!_service)"
)
_ALLOWED_REFERENCES = {
    "app/models/review.py",
    "app/services/kiwoom_mock_order_ledger_service.py",
}


def test_only_the_service_module_writes_the_ledger():
    offenders: list[str] = []
    for root in ("app", "scripts", "research"):
        for path in (REPO / root).rglob("*.py"):
            relative = path.relative_to(REPO).as_posix()
            if relative in _ALLOWED_REFERENCES:
                continue
            if _LEDGER_REFERENCE.search(path.read_text(encoding="utf-8")):
                offenders.append(relative)
    assert offenders == []


def test_the_service_module_is_the_only_one_that_inserts_ledger_rows():
    source = (REPO / "app/services/kiwoom_mock_order_ledger_service.py").read_text(
        encoding="utf-8"
    )
    # Raw SQL write verbs never appear; rows go through the ORM session only.
    assert not re.search(r"\b(insert|update|delete)\s*\(", source, re.IGNORECASE)
    assert "text(" not in source
