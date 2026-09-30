"""#1112 — night sweep and expired[inference] against the run-owned test DB.

AC A1: stale ``proposed`` groups past ``valid_until`` become ``expired``; valid,
approved and mid-approval groups are untouched; no broker or order path is
reached (every entry point is replaced by a fake that fails the test on call).
AC A5: a second run changes nothing.
AC A3/A4 end to end: the inferred rung carries the marker in every projection
that lists it, and the 7-D report names blockers by row id and rule.

The sweep is a global maintenance pass over a shared test DB, so assertions are
scoped to the rows each test seeds.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, engine
from app.mcp_server.tooling import order_execution
from app.mcp_server.tooling import order_proposal_tools as opt
from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.models.order_proposals import OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.models.trading import InstrumentType
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.client import TossReadClient
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
)
from app.services.order_proposals.night_sweep import NIGHT_SWEEP_VOID_REASON
from app.services.order_proposals.service import RungInput
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

KST = datetime.timezone(datetime.timedelta(hours=9))
DAY = datetime.date(2026, 9, 29)  # an XKRX session (Tue)
NOW = datetime.datetime(2026, 9, 30, 7, 0, tzinfo=KST)  # the 07:00 sweep
OWNER = "test-owner-agent"


def kst(hh: int, mm: int = 0, ss: int = 0) -> datetime.datetime:
    return datetime.datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss, tzinfo=KST)


def _sym() -> str:
    return f"Z{uuid.uuid4().hex[:6].upper()}"


class _Trap:
    """Any call is a test failure: the sweep must never reach these."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"night sweep reached a broker/order path: {self.name}")


class _Notifier:
    """Telegram stand-in: the existing expiry path edits cards / copies notices."""

    def __init__(self) -> None:
        self.edited: list[Any] = []
        self.sent: list[Any] = []

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text))
        return SimpleNamespace(ok=True)

    async def send_approval_message(self, text, inline_keyboard, **kwargs: Any):
        # Only the expiry notice copy may be sent, and never with buttons.
        assert inline_keyboard is None
        self.sent.append(text)
        return SimpleNamespace(ok=True)


@pytest.fixture
def broker_traps(monkeypatch):
    monkeypatch.setattr(KISClient, "__init__", _Trap("KISClient"))
    monkeypatch.setattr(TossReadClient, "__init__", _Trap("TossReadClient"))
    monkeypatch.setattr(
        order_execution, "_execute_and_record", _Trap("_execute_and_record")
    )
    monkeypatch.setattr(order_execution, "_place_order_impl", _Trap("_place_order"))
    monkeypatch.setattr(opt, "_fetch_void_evidence", _Trap("_fetch_void_evidence"))
    notifier = _Notifier()
    monkeypatch.setattr(opt, "_get_trade_notifier", lambda: notifier)
    monkeypatch.setattr(opt, "now_kst", lambda: NOW)
    return notifier


async def _create(symbol: str, *, valid_until: datetime.datetime | None) -> Any:
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group = await service.create_proposal(
            symbol=symbol,
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            strategy="underwater_support_net",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("1000"), None)],
            creator_agent_id=OWNER,
        )
        group.valid_until = valid_until
        await session.commit()
        return group.proposal_id


async def _transition(proposal_id: uuid.UUID, *states: str) -> None:
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        for state in states:
            await service.transition_rung(proposal_id, 0, new_state=state)
        await session.commit()


async def _state(proposal_id: uuid.UUID) -> tuple[str, list[OrderProposalRung]]:
    async with AsyncSessionLocal() as session:
        group, rungs = await OrderProposalsService(session).get_proposal(proposal_id)
        return group.lifecycle_state, list(rungs)


# --- A1 + A5: the stale-proposal sweep ---------------------------------------


@pytest.mark.asyncio
async def test_night_sweep_expires_only_stale_proposed_and_is_idempotent(
    broker_traps,
):
    past = NOW - datetime.timedelta(days=22)
    stale = await _create(_sym(), valid_until=past)
    valid = await _create(_sym(), valid_until=NOW + datetime.timedelta(hours=8))
    no_deadline = await _create(_sym(), valid_until=None)
    stale_reconfirm = await _create(_sym(), valid_until=past)
    await _transition(stale_reconfirm, "revalidating")
    async with AsyncSessionLocal() as session:
        await OrderProposalsService(session).mark_needs_reconfirm(
            stale_reconfirm, 0, now=past - datetime.timedelta(hours=1)
        )
        await session.commit()
    mid_approval = await _create(_sym(), valid_until=past)
    await _transition(mid_approval, "revalidating")
    approved = await _create(_sym(), valid_until=past)
    await _transition(approved, "revalidating", "approved")

    first = await opt.run_order_proposal_night_sweep(now=NOW)

    swept = set(first["expiry"]["swept_proposal_ids"])
    assert str(stale) in swept
    assert str(stale_reconfirm) in swept
    for untouched in (valid, no_deadline, mid_approval, approved):
        assert str(untouched) not in swept

    state, rungs = await _state(stale)
    assert state == "expired"
    assert [(r.state, r.void_reason) for r in rungs] == [
        ("expired", NIGHT_SWEEP_VOID_REASON)
    ]
    assert (await _state(valid))[1][0].state == "pending_approval"
    assert (await _state(no_deadline))[1][0].state == "pending_approval"
    assert (await _state(mid_approval))[1][0].state == "revalidating"
    assert (await _state(approved))[1][0].state == "approved"

    snapshot = {
        pid: [(r.state, r.void_reason, r.updated_at) for r in (await _state(pid))[1]]
        for pid in (stale, stale_reconfirm, valid, mid_approval, approved)
    }

    second = await opt.run_order_proposal_night_sweep(now=NOW)

    assert not {str(stale), str(stale_reconfirm)} & set(
        second["expiry"]["swept_proposal_ids"]
    )
    for pid, before in snapshot.items():
        after = [(r.state, r.void_reason, r.updated_at) for r in (await _state(pid))[1]]
        assert after == before, pid


# --- the KIS leftover inference ----------------------------------------------


async def _resting_kis_rung(symbol: str) -> tuple[uuid.UUID, str]:
    proposal_id = await _create(symbol, valid_until=kst(15, 30))
    order_no = f"00{uuid.uuid4().int % 10**8:08d}"
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(proposal_id, 0, new_state=state)
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id=order_no,
            correlation_id=f"corr-{order_no}",
            idempotency_key=f"idem-{order_no}",
            approval_hash_digest="digest",
            now=kst(10, 0, 2),
        )
        session.add(
            KISLiveOrderLedger(
                trade_date=kst(10, 0, 1),
                symbol=symbol,
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("1000"),
                amount=Decimal("1000"),
                currency="KRW",
                order_no=order_no,
                order_time="100000",
                account_mode="kis_live",
                broker="kis",
                status="accepted",
                lifecycle_state="accepted",
                idempotency_key=f"idem-{order_no}",
                correlation_id=f"corr-{order_no}",
            )
        )
        session.add(
            ExecutionLedger(
                broker="kis",
                account_mode="live",
                venue="KRX",
                instrument_type=InstrumentType.equity_kr,
                symbol=symbol,
                raw_symbol=symbol,
                side="buy",
                broker_order_id=f"SEED-{symbol}",
                fill_seq=0,
                filled_qty=Decimal("10"),
                filled_price=Decimal("1200"),
                filled_notional=Decimal("12000"),
                filled_at=kst(10) - datetime.timedelta(days=20),
                currency="KRW",
                source="manual_import",
            )
        )
        session.add(
            ExecutionLedgerReconcileRun(
                run_id=uuid.uuid4(),
                broker="kis",
                window_start=kst(20, 30) - datetime.timedelta(days=1),
                window_end=kst(20, 30),
                finished_at=kst(20, 31),
                dry_run=False,
            )
        )
        await session.commit()
    return proposal_id, order_no


def _blocking_row(report: dict[str, Any], symbol: str) -> dict[str, Any]:
    return next(row for row in report["symbols"] if row["symbol"] == symbol)


@pytest.mark.asyncio
async def test_eligible_kis_leftover_is_closed_as_inference_and_stops_blocking(
    broker_traps,
):
    symbol = _sym()
    proposal_id, order_no = await _resting_kis_rung(symbol)

    before = await opt.order_proposal_list(symbol=symbol, include_kr_buy_blocking=True)
    row = _blocking_row(before["kr_buy_blocking"], symbol)
    assert row["blocked"] is True
    [item] = row["blocking"]
    assert item["proposal_id"] == str(proposal_id)
    assert item["rule"] == "kis_resting_rung_inference_eligible_pending_sweep"
    assert item["inference"]["failed_conditions"] == []

    first = await opt.run_order_proposal_night_sweep(now=NOW)
    assert any(
        r["proposal_id"] == str(proposal_id) for r in first["inference"]["applied_rows"]
    )

    state, rungs = await _state(proposal_id)
    assert state == "expired"
    assert rungs[0].state == "expired"
    assert rungs[0].void_reason == EXPIRED_INFERENCE_VOID_REASON

    got = await opt.order_proposal_get(str(proposal_id))
    assert got["rungs"][0]["expiry_basis"] == "inference"
    assert got["rungs"][0]["expiry_caveat"] == "no_broker_original"

    after = await opt.order_proposal_list(symbol=symbol, include_kr_buy_blocking=True)
    row = _blocking_row(after["kr_buy_blocking"], symbol)
    assert row["blocked"] is False
    [cleared] = row["cleared"]
    assert cleared["basis"] == "expired_inference"
    assert cleared["caveat"] == "no_broker_original"
    assert datetime.datetime.fromisoformat(cleared["cleared_at"]) == NOW

    # The broker-evidence ledger is never rewritten by an inference.
    async with AsyncSessionLocal() as session:
        ledger_status = (
            await session.execute(
                select(KISLiveOrderLedger.status).where(
                    KISLiveOrderLedger.order_no == order_no
                )
            )
        ).scalar_one()
    assert ledger_status == "accepted"

    second = await opt.run_order_proposal_night_sweep(now=NOW)
    assert not any(
        r["proposal_id"] == str(proposal_id)
        for r in second["inference"]["applied_rows"]
    )
    assert (await _state(proposal_id))[1][0].updated_at == rungs[0].updated_at


@pytest.mark.asyncio
async def test_kis_leftover_with_a_fill_keeps_blocking_with_the_reason(broker_traps):
    symbol = _sym()
    proposal_id, order_no = await _resting_kis_rung(symbol)
    async with AsyncSessionLocal() as session:
        session.add(
            ExecutionLedger(
                broker="kis",
                account_mode="live",
                venue="KRX",
                instrument_type=InstrumentType.equity_kr,
                symbol=symbol,
                raw_symbol=symbol,
                side="buy",
                broker_order_id=order_no,
                fill_seq=0,
                filled_qty=Decimal("1"),
                filled_price=Decimal("1000"),
                filled_notional=Decimal("1000"),
                filled_at=kst(11),
                currency="KRW",
                source="websocket",
            )
        )
        await session.commit()

    await opt.run_order_proposal_night_sweep(now=NOW)

    assert (await _state(proposal_id))[1][0].state == "resting"
    report = await opt.order_proposal_list(symbol=symbol, include_kr_buy_blocking=True)
    row = _blocking_row(report["kr_buy_blocking"], symbol)
    assert row["blocked"] is True
    [item] = row["blocking"]
    assert item["rule"] == "kis_resting_rung_inference_conditions_not_met"
    assert "no_fill_in_execution_ledger" in item["inference"]["failed_conditions"]
    assert "holding_quantity_unchanged" in item["inference"]["failed_conditions"]


@pytest.mark.asyncio
async def test_kis_leftover_is_not_inferred_before_the_conservative_deadline(
    broker_traps,
):
    symbol = _sym()
    proposal_id, _ = await _resting_kis_rung(symbol)

    await opt.run_order_proposal_night_sweep(now=kst(16, 30))

    assert (await _state(proposal_id))[1][0].state == "resting"


@pytest.mark.asyncio
async def test_default_list_output_has_no_blocking_key(broker_traps):
    symbol = _sym()
    await _create(symbol, valid_until=None)
    result = await opt.order_proposal_list(symbol=symbol)
    assert "kr_buy_blocking" not in result


@pytest.mark.asyncio
async def test_superseded_group_with_a_broker_live_rung_still_blocks(broker_traps):
    symbol = _sym()
    proposal_id = await _create(symbol, valid_until=NOW + datetime.timedelta(hours=8))
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(proposal_id, 0, new_state=state)
        await service.record_ack(
            proposal_id,
            0,
            broker_order_id=f"ACK-{symbol}",
            correlation_id=f"corr-ack-{symbol}",
            idempotency_key=f"idem-ack-{symbol}",
            approval_hash_digest="digest",
            now=NOW - datetime.timedelta(hours=1),
        )
        group, _ = await service.get_proposal(proposal_id)
        # Supersession retires only still-local rungs; the acked one stays live.
        group.lifecycle_state = "superseded"
        await session.commit()

    report = await opt.order_proposal_list(symbol=symbol, include_kr_buy_blocking=True)

    row = _blocking_row(report["kr_buy_blocking"], symbol)
    assert row["blocked"] is True
    [item] = row["blocking"]
    assert item["proposal_id"] == str(proposal_id)
    assert item["lifecycle_state"] == "superseded"
    assert item["rung_state"] == "acked"
    assert item["rule"] == "broker_live_rung_awaiting_broker_evidence"
