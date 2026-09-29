"""#943 rule-executed P follow — run-owned PostgreSQL, fake broker, fake notifier.

No test here reaches a broker or a notifier transport: every broker read is a
``FakeBroker`` provider and every notification is captured in a list.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.db import AsyncSessionLocal
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services import protected_position_auto_follow as auto
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedQuantityService,
    ProtectionKey,
    normalize_protection_key,
)

pytestmark = pytest.mark.integration

ON = SimpleNamespace(protected_position_auto_follow_enabled=True)
OFF = SimpleNamespace(protected_position_auto_follow_enabled=False)


class FakeBroker:
    """Scripted broker holdings; ``script`` values are consumed per read."""

    def __init__(
        self,
        held: str,
        *,
        script: list[str] | None = None,
        mine: set[str] | None = None,
    ) -> None:
        self.held = Decimal(held)
        self.script = [Decimal(value) for value in script or []]
        self.mine = mine
        self.reads = 0

    def factory(self, key: ProtectionKey):
        async def observe() -> BrokerPositionObservation:
            self.reads += 1
            if self.mine is not None and key.symbol not in self.mine:
                # Heads declared by other tests share this database; the
                # lever must leave them alone.
                return BrokerPositionObservation(
                    held=Decimal("1000000000"),
                    sellable=Decimal("1000000000"),
                    observed_at=datetime.now(UTC),
                )
            held = self.script.pop(0) if self.script else self.held
            return BrokerPositionObservation(
                held=held, sellable=held, observed_at=datetime.now(UTC)
            )

        return observe


class Notes:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    async def __call__(self, message: str) -> None:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("transport down")


def _symbol() -> str:
    return f"A{uuid4().hex[:7].upper()}"


def _key(symbol: str, scope: str = "kis_live") -> ProtectionKey:
    return normalize_protection_key(account_scope=scope, market="kr", symbol=symbol)


async def _declare(db_session, symbol: str, quantity: str, *, scope: str = "kis_live"):
    async def observe() -> BrokerPositionObservation:
        return BrokerPositionObservation(
            held=Decimal("100"), sellable=Decimal("100"), observed_at=datetime.now(UTC)
        )

    return await ProtectedQuantityService(db_session).save(
        account_scope=scope,
        market="kr",
        symbol=symbol,
        protected_quantity=quantity,
        expected_revision=None,
        reason="first declaration",
        idempotency_key=f"declare-{uuid4()}",
        actor_user_id=1,
        origin="operator_cli",
        observation_provider=observe,
        confirm_protection_change=True,
    )


async def _fill(
    db_session,
    symbol: str,
    *,
    side: str,
    source: str = "reconciler",
    account_mode: str = "live",
    broker: str = "kis",
) -> int:
    fill = ExecutionLedgerUpsert(
        broker=broker,
        account_mode=account_mode,
        venue="krx" if broker == "kis" else "toss_kr",
        instrument_type="equity_kr",
        symbol=symbol,
        raw_symbol=symbol,
        side=side,
        broker_order_id=f"auto-follow-{uuid4()}",
        fill_seq=0,
        filled_qty=Decimal("1"),
        filled_price=Decimal("70000"),
        filled_notional=Decimal("70000"),
        filled_at=datetime.now(UTC),
        currency="KRW",
        source=source,
        raw_payload_json={"fixture": "auto-follow"},
    )
    _status, row_id = await ExecutionLedgerRepository(db_session).upsert_fill(fill)
    await db_session.commit()
    return row_id


async def _follow(ids: list[int], broker: FakeBroker, notes: Notes, settings=ON):
    return await auto.follow_committed_fills(
        ids,
        session_factory=AsyncSessionLocal,
        provider_factory=broker.factory,
        notify=notes,
        settings_obj=settings,
    )


async def _lever(broker: FakeBroker, notes: Notes, *, settings=ON, dry_run=False):
    return await auto.reconcile_declared_positions(
        account_scope="kis_live",
        dry_run=dry_run,
        session_factory=AsyncSessionLocal,
        provider_factory=broker.factory,
        notify=notes,
        settings_obj=settings,
    )


async def _state(symbol: str, scope: str = "kis_live"):
    async with AsyncSessionLocal() as db:
        service = ProtectedQuantityService(db)
        key = _key(symbol, scope)
        head = await service.get(key=key)
        revisions = await service.list_revisions(key=key)
        return head, revisions


def _mine(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    return [item for item in payload["outcomes"] if item["symbol"] == symbol]


@pytest.mark.unit
def test_kill_switch_defaults_off() -> None:
    assert (
        Settings.model_fields["protected_position_auto_follow_enabled"].default is False
    )
    assert auto.auto_follow_enabled(SimpleNamespace()) is False
    assert (
        auto.auto_follow_enabled(
            SimpleNamespace(protected_position_auto_follow_enabled="true")
        )
        is False
    )


@pytest.mark.asyncio
async def test_authoritative_buy_fill_raises_declared_p_to_fresh_holding(
    db_session,
) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "1")
    ledger_id = await _fill(db_session, symbol, side="buy")
    broker, notes = FakeBroker("3"), Notes()

    [outcome] = await _follow([ledger_id], broker, notes)

    head, revisions = await _state(symbol)
    assert outcome.status == "raised"
    assert head is not None and head.protected_quantity == Decimal("3")
    assert head.revision == 2
    last = revisions[-1]
    assert (last.action, last.origin, last.actor_user_id) == (
        "increase",
        "operator_cli",
        auto.protection_owner_user_id(),
    )
    assert last.reason == f"auto:fill {ledger_id}"
    assert last.idempotency_key == f"auto:fill:{ledger_id}"
    assert Decimal(str(last.broker_held_observed)) == Decimal("3")
    assert len(notes.messages) == 1
    assert symbol in notes.messages[0] and "1 -> 3" in notes.messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "account_mode", "reason"),
    [
        ("websocket", "live", "not_authoritative"),
        ("manual_import", "live", "not_authoritative"),
        ("reconciler", "mock", "not_live"),
    ],
)
async def test_provisional_or_non_live_rows_never_touch_p(
    db_session, source: str, account_mode: str, reason: str
) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "1")
    ledger_id = await _fill(
        db_session, symbol, side="buy", source=source, account_mode=account_mode
    )
    broker, notes = FakeBroker("3"), Notes()

    [outcome] = await _follow([ledger_id], broker, notes)

    head, revisions = await _state(symbol)
    assert (outcome.status, outcome.reason) == ("skipped", reason)
    assert head is not None and head.protected_quantity == Decimal("1")
    assert len(revisions) == 1
    assert broker.reads == 0
    assert notes.messages == []


@pytest.mark.asyncio
async def test_authoritative_sell_fill_lowers_p_to_holding_and_never_raises(
    db_session,
) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "4")
    sell_id = await _fill(db_session, symbol, side="sell")
    notes = Notes()

    [lowered] = await _follow([sell_id], FakeBroker("2"), notes)
    head, revisions = await _state(symbol)
    assert lowered.status == "lowered"
    assert head is not None and head.protected_quantity == Decimal("2")
    assert (revisions[-1].action, revisions[-1].reason) == (
        "decrease",
        "auto:reconcile",
    )
    assert revisions[-1].idempotency_key.startswith("auto:reconcile:")

    # A sell fill never raises P, even when the holding is above it.
    other_sell = await _fill(db_session, symbol, side="sell")
    [unchanged] = await _follow([other_sell], FakeBroker("9"), notes)
    head, revisions = await _state(symbol)
    assert unchanged.status == "unchanged"
    assert head is not None and head.protected_quantity == Decimal("2")
    assert len(revisions) == 2
    assert len(notes.messages) == 1


@pytest.mark.asyncio
async def test_lever_lowers_after_app_sale_without_any_ledger_row(db_session) -> None:
    symbol = _symbol()
    released_symbol = _symbol()
    await _declare(db_session, symbol, "4")
    await _declare(db_session, released_symbol, "2")
    notes = Notes()

    class PerKey(FakeBroker):
        def factory(self, key: ProtectionKey):
            self.held = Decimal("1") if key.symbol == symbol else Decimal("0")
            if key.symbol not in {symbol, released_symbol}:
                self.held = Decimal("100000")
            return super().factory(key)

    preview = await _lever(PerKey("0"), notes, dry_run=True)
    assert _mine(preview, symbol)[0]["status"] == "would_lower"
    head, _ = await _state(symbol)
    assert head is not None and head.protected_quantity == Decimal("4")
    assert notes.messages == []

    result = await _lever(PerKey("0"), notes)
    [item] = _mine(result, symbol)
    [released] = _mine(result, released_symbol)
    head, revisions = await _state(symbol)
    released_head, released_revisions = await _state(released_symbol)
    assert item["status"] == "lowered"
    assert head is not None and head.protected_quantity == Decimal("1")
    assert revisions[-1].reason == "auto:reconcile"
    assert released["status"] == "lowered"
    assert released_head is not None and released_head.protected_quantity == 0
    assert released_revisions[-1].action == "release"
    assert sum(symbol in m or released_symbol in m for m in notes.messages) == 2


@pytest.mark.asyncio
async def test_lever_never_raises_p_above_its_declaration(db_session) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "4")
    notes = Notes()

    result = await _lever(FakeBroker("10", mine={symbol}), notes)

    [item] = _mine(result, symbol)
    head, revisions = await _state(symbol)
    assert item["status"] == "unchanged"
    assert head is not None and head.protected_quantity == Decimal("4")
    assert len(revisions) == 1


@pytest.mark.asyncio
async def test_p_never_exceeds_in_lock_holding_when_holding_drops_mid_decision(
    db_session,
) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "1")
    ledger_id = await _fill(db_session, symbol, side="buy")
    # pre-lock read 5, in-lock read 3 (save refuses 5), then 3 and 3 again.
    broker, notes = FakeBroker("3", script=["5", "3"]), Notes()

    [outcome] = await _follow([ledger_id], broker, notes)

    head, revisions = await _state(symbol)
    assert outcome.status == "raised"
    assert head is not None and head.protected_quantity == Decimal("3")
    assert all(
        Decimal(str(row.new_quantity)) <= Decimal(str(row.broker_held_observed))
        for row in revisions[1:]
    )
    assert len(revisions) == 2
    assert len(notes.messages) == 1


@pytest.mark.asyncio
async def test_undeclared_and_released_keys_are_never_auto_declared(db_session) -> None:
    undeclared = _symbol()
    released = _symbol()
    declared = await _declare(db_session, released, "2")
    await ProtectedQuantityService(db_session).save(
        account_scope="kis_live",
        market="kr",
        symbol=released,
        protected_quantity="0",
        expected_revision=declared.revision,
        reason="release",
        idempotency_key=f"release-{uuid4()}",
        actor_user_id=1,
        origin="operator_cli",
        observation_provider=FakeBroker("100").factory(_key(released)),
        confirm_protection_change=True,
        confirm_symbol=released,
    )
    undeclared_fill = await _fill(db_session, undeclared, side="buy")
    released_fill = await _fill(db_session, released, side="buy")
    notes = Notes()

    outcomes = await _follow([undeclared_fill, released_fill], FakeBroker("7"), notes)

    assert [(o.status, o.reason) for o in outcomes] == [
        ("skipped", "undeclared"),
        ("skipped", "released"),
    ]
    undeclared_head, _ = await _state(undeclared)
    released_head, released_revisions = await _state(released)
    assert undeclared_head is None
    assert released_head is not None and released_head.protected_quantity == 0
    assert len(released_revisions) == 2
    assert notes.messages == []


@pytest.mark.asyncio
async def test_replaying_the_same_row_or_lever_adds_no_revision(db_session) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "1")
    ledger_id = await _fill(db_session, symbol, side="buy")
    notes = Notes()

    await _follow([ledger_id, ledger_id], FakeBroker("3"), notes)
    # The holding grew again through an unprocessed fill; replaying the old
    # row must still not append a second revision under its key.
    [replay] = await _follow([ledger_id], FakeBroker("6"), notes)
    await _lever(FakeBroker("6", mine={symbol}), notes)
    await _lever(FakeBroker("6", mine={symbol}), notes)

    head, revisions = await _state(symbol)
    assert (replay.status, replay.reason) == ("skipped", "replay")
    assert head is not None and head.protected_quantity == Decimal("3")
    assert len(revisions) == 2
    assert len(notes.messages) == 1


@pytest.mark.asyncio
async def test_two_close_fills_converge_on_the_final_holding(db_session) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "1")
    first = await _fill(db_session, symbol, side="buy")
    second = await _fill(db_session, symbol, side="buy")
    notes = Notes()

    class Barrier(FakeBroker):
        """Hold both pre-lock reads until each worker has read head revision 1."""

        def __init__(self) -> None:
            super().__init__("5")
            self.arrived = 0
            self.both = asyncio.Event()

        def factory(self, key: ProtectionKey):
            inner = super().factory(key)

            async def observe() -> BrokerPositionObservation:
                if self.arrived < 2:
                    self.arrived += 1
                    if self.arrived == 2:
                        self.both.set()
                    await asyncio.wait_for(self.both.wait(), timeout=10)
                return await inner()

            return observe

    broker = Barrier()

    results = await asyncio.gather(
        _follow([first], broker, notes), _follow([second], broker, notes)
    )

    head, revisions = await _state(symbol)
    statuses = sorted(outcome.status for [outcome] in results)
    assert head is not None and head.protected_quantity == Decimal("5")
    # Both decided from revision 1; the loser hit stale_form, re-read the
    # head and found it already at the holding.
    assert statuses == ["raised", "unchanged"]
    assert len(revisions) == 2
    assert len(notes.messages) == 1


@pytest.mark.asyncio
async def test_kill_switch_off_reads_nothing_and_writes_nothing(db_session) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "4")
    ledger_id = await _fill(db_session, symbol, side="sell")
    broker, notes = FakeBroker("0"), Notes()

    assert await _follow([ledger_id], broker, notes, settings=OFF) == []
    lever = await _lever(broker, notes, settings=OFF)

    head, revisions = await _state(symbol)
    assert lever == {"status": "disabled", "dry_run": False, "outcomes": []}
    assert head is not None and head.protected_quantity == Decimal("4")
    assert len(revisions) == 1
    assert broker.reads == 0
    assert notes.messages == []


@pytest.mark.asyncio
async def test_notification_failure_never_undoes_or_repeats_the_change(
    db_session,
) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "4")
    ledger_id = await _fill(db_session, symbol, side="sell")
    notes = Notes(fail=True)

    [outcome] = await _follow([ledger_id], FakeBroker("2"), notes)

    head, _ = await _state(symbol)
    assert outcome.status == "lowered"
    assert head is not None and head.protected_quantity == Decimal("2")
    assert len(notes.messages) == 1


@pytest.mark.asyncio
async def test_broker_read_failure_is_an_outcome_not_an_exception(db_session) -> None:
    symbol = _symbol()
    await _declare(db_session, symbol, "4")
    ledger_id = await _fill(db_session, symbol, side="sell")

    def broken(key: ProtectionKey):
        async def observe() -> BrokerPositionObservation:
            raise RuntimeError("broker down")

        return observe

    [outcome] = await auto.follow_committed_fills(
        [ledger_id],
        session_factory=AsyncSessionLocal,
        provider_factory=broken,
        notify=Notes(),
        settings_obj=ON,
    )

    head, _ = await _state(symbol)
    assert (outcome.status, outcome.reason) == ("error", "RuntimeError")
    assert head is not None and head.protected_quantity == Decimal("4")
