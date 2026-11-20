"""hk 5441 — the ``upbit_live`` path against the run-owned test database (CI).

What only the database can prove: the triggers of
``alembic/versions/20261120_hk5441_upbit_intent_gateway.py`` refuse what the
design forbids for any writer (I1, I3, I6, B29, S5, S7), the claim of an exit
handoff is single (S5 d), and the real store journals a shadow child end to end
without a broker order (I6).

Sleeves, mandates and handoffs cannot be deleted (their guards refuse it), so
every test creates its own uniquely named rows and leaves them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal, engine
from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.models.execution_mandates import (
    ExecutionExitHandoff,
    ExecutionMandate,
    ExecutionMandateRevocation,
)
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.strategy_sleeves import StrategySleeve
from app.schemas.execution_intent import (
    DecimalInventoryConstraint,
    ExecutionIntentV2,
    UpbitSubmitChild,
)
from app.services.execution_intent_gateway import upbit_store as upbit_store_module
from app.services.execution_intent_gateway.account_control import (
    PostgresAccountControlStore,
    set_account_mode,
)
from app.services.execution_intent_gateway.decisions import ChildState
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.mandates import (
    MandateError,
    create_mandate,
    create_operator_exit,
    draft_mandate,
    revoke_mandate,
)
from app.services.execution_intent_gateway.upbit_decisions import (
    AccountReads,
    UpbitDecision,
)
from app.services.execution_intent_gateway.upbit_gateway import UpbitGatewayPath
from app.services.execution_intent_gateway.upbit_store import (
    IntentRefused,
    UpbitGatewayStore,
)
from app.services.kis_mock_runner.control import (
    KillMode,
    read_effective_kill_switch,
)
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.service import RungInput
from app.services.strategy_sleeves import account_binding, exit_handoffs
from app.services.strategy_sleeves import store as sleeve_store
from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.exit_handoffs import (
    ExitHandoffError,
    SettleOutcome,
    fence_card_send,
    open_exit_handoff,
    settle_exit_handoff,
)
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.support import (
    FakeKillSwitch,
    journal_tables_lock,
)
from tests.services.execution_intent_gateway.upbit_support import (
    LIVE_ENV,
    FakeLiveForwarder,
    FakeShadowForwarder,
)

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic/versions"
ACCOUNT_REF = "upbit:upbit:" + "b" * 64
SHA = "a" * 40
SYMBOL = "KRW-XRP"
PREREG: dict[str, Any] = {
    "metric": "cost-net result at H minus baseline",
    "min_effect_pct": "1",
    "horizon_sessions": "20",
    "max_duration_days": "90",
}


def _migration(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name.split(".")[0], _VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture(autouse=True)
async def _account_control_row() -> Any:
    """The seed row of the kill table, under the journal tables' test lock.

    The guards themselves come from the test schema bootstrap, which installs
    this revision's ``GUARD_DDL`` and the two journal triggers of 20261101. No
    DDL runs here: a per-test ``DROP TRIGGER`` on ``order_proposals`` would take
    locks other DB tests are waiting behind.

    ``test_store_db`` truncates the journal tables after each of its tests, on
    another xdist worker against this same database. The lock keeps that from
    happening under a test here.
    """
    hk5441 = _migration("20261120_hk5441_upbit_intent_gateway.py")
    async with journal_tables_lock():
        async with engine.begin() as conn:
            for statement in hk5441.SEED_DDL:
                await conn.execute(text(statement))
        yield


def _now() -> datetime:
    return datetime.now(UTC)


async def _sleeve(state: str, *, execution_path: str = "go") -> StrategySleeve:
    async with AsyncSessionLocal() as session:
        sleeve, _version = await sleeve_store.create_sleeve(
            session,
            sleeve_key=f"hk5441-{uuid.uuid4().hex[:12]}",
            rule_id="rob1458.crypto_b3",
            account_mode="upbit",
            market="crypto",
            state=state,
            budget_cap="1950000",
            loss_limit="1000000",
            reason="hk 5441 db test",
            actor="test",
            execution_path=execution_path,
            **PREREG,
        )
        await session.commit()
        return sleeve


async def _mandate(
    sleeve: StrategySleeve, mode: str, *, max_intent_ttl_seconds: str = "43200"
) -> ExecutionMandate:
    async with AsyncSessionLocal() as session:
        draft = await draft_mandate(
            session,
            sleeve_key=sleeve.sleeve_key,
            strategy="upbit-trend-long-v1",
            strategy_code_sha=SHA,
            mode=mode,
            allowed_symbols=["KRW-BTC", SYMBOL],
            per_intent_max_notional_krw="480000",
            max_open_intents="4",
            price_band_bps="30",
            max_intent_ttl_seconds=max_intent_ttl_seconds,
            expires_at=_now() + timedelta(days=30),
            account_ref=ACCOUNT_REF if mode == "live" else None,
            now=_now(),
        )
        row = await create_mandate(
            session,
            draft,
            actor="test-operator",
            approval_ref="decision/2026-11-20/hk5441-db-test",
            confirm_content_sha256=draft.content_sha256,
            confirm_account_ref=draft.account_ref,
        )
        await session.commit()
        return row


def _intent(mandate: ExecutionMandate, **overrides: Any) -> ExecutionIntentV2:
    values: dict[str, Any] = {
        "schema_version": 2,
        "intent_id": uuid.uuid4(),
        "strategy": mandate.strategy,
        "account": "upbit_live",
        "venue": "upbit",
        "execution_mode": mandate.mode,
        "mandate_id": mandate.id,
        "sleeve_id": mandate.sleeve_id,
        "symbol": SYMBOL,
        "side": "buy",
        "max_quantity_decimal": "100",
        "max_notional_krw": 300_000,
        "strategy_code_sha": SHA,
        "limit_price_krw": 3_000,
        "max_slippage_bps": 30,
        "expires_at": _now() + timedelta(hours=1),
        "inventory": DecimalInventoryConstraint(
            position_at_authorization="0",
            min_position_after="0",
            max_position_after="100",
        ),
        "policy_version": "upbit-trend-long-v1",
        "authorization_ref": f"mandate:{mandate.id}",
        "decision_ref": f"decision-{uuid.uuid4().hex[:8]}",
    }
    return ExecutionIntentV2(**{**values, **overrides})


def _reads(**overrides: Any) -> AccountReads:
    values: dict[str, Any] = {
        "kill_mode": KillMode.ACTIVE,
        "live_enabled": True,
        "avg_buy_price": Decimal("2500"),
    }
    return AccountReads(**{**values, **overrides})


def _submit(
    intent: ExecutionIntentV2, key: str, quantity: str = "10"
) -> UpbitSubmitChild:
    return UpbitSubmitChild(
        action="submit",
        intent_id=intent.intent_id,
        idempotency_key=key,
        quantity_decimal=quantity,
        limit_price_krw=3_000,
    )


def _key() -> str:
    return f"k-{uuid.uuid4().hex[:20]}"


def _path(store: UpbitGatewayStore, *, avg: str = "2500") -> tuple[Any, Any, Any]:
    shadow, live = FakeShadowForwarder(), FakeLiveForwarder()

    async def avg_reader(_symbol: str) -> Decimal | None:
        return Decimal(avg)

    async def cooldown(_symbol: str) -> bool:
        return False

    path = UpbitGatewayPath(
        store=store,
        kill_switch=FakeKillSwitch(),
        shadow_forwarder=shadow,
        live_forwarder=live,
        avg_price_reader=avg_reader,
        cooldown_reader=cooldown,
        environment=LIVE_ENV,
    )
    return path, shadow, live


async def _child(key: str) -> ExecutionChildRequest:
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(ExecutionChildRequest).where(
                ExecutionChildRequest.idempotency_key == key
            )
        )
        assert row is not None
        return row


# ------------------------------------------------------------------ S7


@pytest.mark.asyncio
async def test_s7_a_mandate_cannot_bind_to_a_card_sleeve_even_by_direct_insert() -> (
    None
):
    go = await _sleeve("shadow")
    good = await _mandate(go, "shadow")
    card = await _sleeve("shadow", execution_path="card")
    async with AsyncSessionLocal() as session:
        version = await sleeve_store.current_version(session, card.id)
        assert version is not None
        session.add(
            ExecutionMandate(
                id=uuid.uuid4(),
                terms_version=good.terms_version,
                strategy=good.strategy,
                strategy_code_sha=SHA,
                sleeve_id=card.id,
                sleeve_version_id=version.id,
                account="upbit_live",
                mode="shadow",
                allowed_symbols=[SYMBOL],
                per_intent_max_notional_krw=Decimal("480000"),
                max_open_intents=4,
                price_band_bps=30,
                max_intent_ttl=timedelta(hours=12),
                expires_at=_now() + timedelta(days=1),
                loss_sell_mode="human_card",
                approval_ref="operator-cli/test",
                content_sha256="0" * 64,
                created_by="test",
            )
        )
        with pytest.raises(DBAPIError):
            await session.flush()


@pytest.mark.asyncio
async def test_s7_the_service_refuses_a_card_sleeve_and_a_second_mandate() -> None:
    card = await _sleeve("shadow", execution_path="card")
    with pytest.raises(MandateError):
        await _mandate(card, "shadow")
    go = await _sleeve("shadow")
    await _mandate(go, "shadow")
    with pytest.raises(MandateError):
        await _mandate(go, "shadow")


@pytest.mark.asyncio
async def test_s7_the_execution_path_never_changes() -> None:
    card = await _sleeve("shadow", execution_path="card")
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(StrategySleeve)
                .where(StrategySleeve.id == card.id)
                .values(execution_path="go")
            )


@pytest.mark.asyncio
async def test_s7_a_go_sleeve_refuses_an_ordinary_card() -> None:
    go = await _sleeve("live")
    async with AsyncSessionLocal() as session:
        with pytest.raises(OrderProposalError):
            await OrderProposalsService(session).create_proposal(
                symbol=SYMBOL,
                market="crypto",
                account_mode="upbit",
                side="sell",
                order_type="limit",
                proposer="p",
                thesis="hk 5441 db test",
                rungs=[RungInput(0, "sell", Decimal("1"), Decimal("3000"), None)],
                sleeve_id=go.id,
            )
    # The trigger holds the same rule for a writer that skips the service.
    async with AsyncSessionLocal() as session:
        proposal_id = uuid.uuid4()
        session.add(
            OrderProposal(
                proposal_id=proposal_id,
                root_proposal_id=proposal_id,
                symbol=SYMBOL,
                market="crypto",
                account_mode="upbit",
                side="sell",
                order_type="limit",
                proposer="p",
                sleeve_id=go.id,
            )
        )
        with pytest.raises(DBAPIError):
            await session.flush()


# ------------------------------------------------------------------ I1, I3


@pytest.mark.asyncio
async def test_i1_a_stored_intent_needs_its_mandate_and_a_revoked_one_refuses() -> None:
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    store = UpbitGatewayStore(AsyncSessionLocal)
    first = _intent(mandate)
    await store.store_intent(first, now=_now(), reads=_reads())
    # The same contents again is a no-op (I12 relies on it).
    await store.store_intent(first, now=_now(), reads=_reads())
    async with AsyncSessionLocal() as session:
        stored = await session.get(ExecutionIntent, first.intent_id)
        assert stored is not None and stored.schema_version == 2
        assert stored.max_quantity is None and stored.execution_mode == "shadow"

    async with AsyncSessionLocal() as session:
        _row, pending = await revoke_mandate(
            session, mandate_id=mandate.id, reason="db test", actor="test-operator"
        )
        await session.commit()
    assert pending == []
    with pytest.raises(IntentRefused) as refused:
        await store.store_intent(
            _intent(
                mandate,
                symbol="KRW-BTC",
                limit_price_krw=150_000_000,
                max_quantity_decimal="0.001",
            ),
            now=_now(),
            reads=_reads(),
        )
    assert refused.value.refusal.value == "mandate_revoked"


@pytest.mark.asyncio
async def test_i3_the_trigger_refuses_an_intent_wider_than_its_mandate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    # Let the Python check through, so only the trigger stands in the way.
    monkeypatch.setattr(
        upbit_store_module, "check_storable", lambda *_a, **_k: UpbitDecision()
    )
    store = UpbitGatewayStore(AsyncSessionLocal)
    for overrides in (
        {"max_notional_krw": 480_001},
        {"max_slippage_bps": 31},
        {"strategy_code_sha": "c" * 40},
        {"symbol": "KRW-DOGE"},
        {"expires_at": _now() + timedelta(hours=13)},
    ):
        with pytest.raises(DBAPIError):
            await store.store_intent(
                _intent(mandate, **overrides), now=_now(), reads=_reads()
            )


@pytest.mark.asyncio
async def test_revocations_are_append_only() -> None:
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    async with AsyncSessionLocal() as session:
        await revoke_mandate(
            session, mandate_id=mandate.id, reason="db test", actor="test-operator"
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        with pytest.raises(MandateError):
            await revoke_mandate(
                session, mandate_id=mandate.id, reason="again", actor="test-operator"
            )
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ExecutionMandateRevocation)
                .where(ExecutionMandateRevocation.mandate_id == mandate.id)
                .values(reason="rewritten")
            )
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ExecutionMandate)
                .where(ExecutionMandate.id == mandate.id)
                .values(price_band_bps=500)
            )


# ------------------------------------------------------------------ I6, B29


@pytest.mark.asyncio
async def test_i6_a_shadow_child_is_journaled_shadowed_and_no_broker_order_can_be_recorded() -> (
    None
):
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    path, shadow, live = _path(store)
    key = _key()

    outcome = await path.handle(_submit(intent, key))
    again = await path.handle(_submit(intent, key))

    assert outcome.status == "shadowed" and again.replayed is True
    assert shadow.calls == [key] and live.calls == []
    row = await _child(key)
    assert row.state == "shadowed" and row.broker_order_id is None
    assert row.quantity is None and row.quantity_decimal == Decimal("10")
    [position] = await store.shadow_positions(sleeve.id)
    assert position["symbol"] == SYMBOL
    assert Decimal(position["quantity"]) == Decimal("10")
    assert Decimal(position["entry_price"]) == Decimal("3000")

    # A second child is reserved and then given a broker outcome by hand: the
    # trigger refuses it for a shadow intent.
    second = _key()
    reservation = await store.reserve(
        _submit(intent, second),
        request_digest="digest",
        decide=lambda *_a: UpbitDecision(),
        now_fn=_now,
    )
    assert reservation.child is not None
    with pytest.raises(DBAPIError):
        await store.finish(
            reservation.child.child_id,
            state=ChildState.ACCEPTED,
            broker_order_id="uuid-forged",
            result={},
            now=_now(),
        )
    assert (await _child(second)).state == "sending"


@pytest.mark.asyncio
async def test_b29_not_sent_needs_a_listed_reason_and_no_broker_order() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())

    async def reserved() -> int:
        reservation = await store.reserve(
            _submit(intent, _key(), quantity="1"),
            request_digest="digest",
            decide=lambda *_a: UpbitDecision(),
            now_fn=_now,
        )
        assert reservation.child is not None
        return reservation.child.child_id

    for reason, order_id in (("made_up_reason", None), ("account_mismatch", "uuid-1")):
        with pytest.raises(DBAPIError):
            await store.finish(
                await reserved(),
                state=ChildState.NOT_SENT,
                broker_order_id=order_id,
                result={},
                now=_now(),
                refusal_reason=reason,
            )
    child_id = await reserved()
    assert await store.finish(
        child_id,
        state=ChildState.NOT_SENT,
        broker_order_id=None,
        result={},
        now=_now(),
        refusal_reason="account_mismatch",
    )
    # A live intent can never be shadowed.
    with pytest.raises(DBAPIError):
        await store.finish(
            await reserved(),
            state=ChildState.SHADOWED,
            broker_order_id=None,
            result={},
            now=_now(),
        )


# ------------------------------------------------------------------ S5


async def _handoff(sleeve: StrategySleeve, mandate: ExecutionMandate) -> int:
    async with AsyncSessionLocal() as session:
        row, created = await open_exit_handoff(
            session,
            sleeve_id=sleeve.id,
            symbol=SYMBOL,
            mandate_id=mandate.id,
            reason="loss_sell_requires_card",
            opened_by="test",
        )
        assert created is True
        _same, created_again = await open_exit_handoff(
            session,
            sleeve_id=sleeve.id,
            symbol=SYMBOL,
            mandate_id=mandate.id,
            reason="loss_sell_requires_card",
            opened_by="test",
        )
        assert created_again is False
        await session.commit()
        return row.id


async def _set_state(handoff_id: int, state: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ExecutionExitHandoff)
            .where(ExecutionExitHandoff.id == handoff_id)
            .values(state=state)
        )
        await session.commit()


async def _card(
    sleeve: StrategySleeve,
    handoff_id: int,
    *,
    quantity: str = "40",
    valid_minutes: int = 30,
) -> OrderProposal:
    async with AsyncSessionLocal() as session:
        group = await OrderProposalsService(session).create_proposal(
            symbol=SYMBOL,
            market="crypto",
            account_mode="upbit",
            side="sell",
            order_type="limit",
            proposer="p",
            thesis="hk 5441 exit handoff card",
            rungs=[RungInput(0, "sell", Decimal(quantity), Decimal("3000"), None)],
            sleeve_id=sleeve.id,
            exit_handoff_id=handoff_id,
            valid_until=_now() + timedelta(minutes=valid_minutes),
        )
        await session.commit()
        return group


@pytest.fixture
def proven(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sleeve's proven position and the configured account, as fixed facts.

    Building them from ledger rows is the business of the sleeve store's own DB
    tests; here they are inputs to the handoff rules under test.
    """

    async def holdings(_session: Any, _sleeve: Any, _symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(exit_handoffs, "proven_holdings", holdings)
    monkeypatch.setattr(
        account_binding, "configured_upbit_account_ref", lambda: ACCOUNT_REF
    )


@pytest.mark.asyncio
async def test_s5_a_handoff_only_moves_forward_and_is_never_removed() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    async with AsyncSessionLocal() as session:
        # ``open`` cannot jump to a claim, and a claim needs its card.
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ExecutionExitHandoff)
                .where(ExecutionExitHandoff.id == handoff_id)
                .values(
                    state="card_claimed",
                    proposal_id=uuid.uuid4(),
                    reserved_quantity=Decimal("1"),
                    claim_count=1,
                )
            )
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("DELETE FROM review.execution_exit_handoffs WHERE id = :id"),
                {"id": handoff_id},
            )


@pytest.mark.asyncio
async def test_s5_an_open_handoff_refuses_new_intents_and_children_on_the_symbol() -> (
    None
):
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    await _handoff(sleeve, mandate)

    path, shadow, _live = _path(store)
    key = _key()
    outcome = await path.handle(_submit(intent, key))

    assert outcome.reason == "exit_handoff_active" and shadow.calls == []
    assert (await _child(key)).state == "refused"
    with pytest.raises(IntentRefused) as refused:
        await store.store_intent(_intent(mandate), now=_now(), reads=_reads())
    assert refused.value.refusal.value == "exit_handoff_active"
    # Another symbol of the same sleeve is not handed off.
    await store.store_intent(
        _intent(
            mandate,
            symbol="KRW-BTC",
            limit_price_krw=150_000_000,
            max_quantity_decimal="0.001",
        ),
        now=_now(),
        reads=_reads(),
    )


@pytest.mark.asyncio
async def test_s5_a_card_needs_a_ready_handoff(proven: None) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)
    # A writer that skips the service is refused by the trigger: the handoff is
    # not claimed by this proposal.
    async with AsyncSessionLocal() as session:
        proposal_id = uuid.uuid4()
        session.add(
            OrderProposal(
                proposal_id=proposal_id,
                root_proposal_id=proposal_id,
                symbol=SYMBOL,
                market="crypto",
                account_mode="upbit",
                side="sell",
                order_type="limit",
                proposer="p",
                sleeve_id=sleeve.id,
                exit_handoff_id=handoff_id,
            )
        )
        with pytest.raises(DBAPIError):
            await session.flush()


@pytest.mark.asyncio
async def test_s5_one_handoff_is_claimed_by_one_card(proven: None) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")

    card = await _card(sleeve, handoff_id)

    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None
        assert handoff.state == "card_claimed" and handoff.claim_count == 1
        assert handoff.proposal_id == card.proposal_id
        assert handoff.reserved_quantity == Decimal("100")
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)


@pytest.mark.asyncio
async def test_s5_a_card_larger_than_the_proven_position_is_refused(
    proven: None,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    with pytest.raises(OrderProposalError, match="exceeds_holdings"):
        await _card(sleeve, handoff_id, quantity="100.00000001")


@pytest.mark.asyncio
async def test_s5_a_changed_account_refuses_the_claim(
    proven: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    monkeypatch.setattr(
        account_binding,
        "configured_upbit_account_ref",
        lambda: "upbit:upbit:" + "c" * 64,
    )
    with pytest.raises(OrderProposalError, match="exit_handoff_account_mismatch"):
        await _card(sleeve, handoff_id)


@pytest.mark.asyncio
async def test_s5_a_second_rung_or_a_larger_rung_fails_at_commit(proven: None) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _card(sleeve, handoff_id)

    async with AsyncSessionLocal() as session:
        session.add(
            OrderProposalRung(
                proposal_pk=card.id,
                rung_index=1,
                side="sell",
                quantity=Decimal("1"),
                limit_price=Decimal("3000"),
                state="pending_approval",
            )
        )
        with pytest.raises(DBAPIError):
            await session.commit()
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(OrderProposalRung)
            .where(OrderProposalRung.proposal_pk == card.id)
            .values(quantity=Decimal("100.5"))
        )
        with pytest.raises(DBAPIError):
            await session.commit()
    async with AsyncSessionLocal() as session:
        rungs = (
            await session.scalars(
                select(OrderProposalRung).where(
                    OrderProposalRung.proposal_pk == card.id
                )
            )
        ).all()
        assert [(r.side, r.quantity) for r in rungs] == [("sell", Decimal("40"))]


# ------------------------------------------------------------------ O1, O3, O4


async def _settle(handoff_id: int, *, claim_count: int = 1) -> Any:
    async with AsyncSessionLocal() as session:
        result = await settle_exit_handoff(
            session,
            handoff_id,
            expected_claim_count=claim_count,
            reading=None,
            now=_now(),
        )
        await session.commit()
        return result


async def _expire(card: OrderProposal) -> None:
    async with AsyncSessionLocal() as session:
        expired = await OrderProposalsService(session).expire_if_needed(
            card.proposal_id, now=_now() + timedelta(hours=2)
        )
        assert expired is True
        await session.commit()


@pytest.mark.asyncio
async def test_o3_a_card_that_can_still_be_approved_keeps_the_claim(
    proven: None,
) -> None:
    """After a W5 pre-core dead letter nothing on the proposal has changed: it is
    still pending with its nonce. The handoff must stay claimed, and no second
    card may be created beside the first.
    """
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    await _card(sleeve, handoff_id)

    result = await _settle(handoff_id)

    assert result.outcome is SettleOutcome.HELD and result.state == "card_claimed"
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)


@pytest.mark.asyncio
async def test_o1_a_card_that_expired_before_approval_returns_the_handoff(
    proven: None,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _card(sleeve, handoff_id)
    await _expire(card)

    result = await _settle(handoff_id)

    assert result.outcome is SettleOutcome.RETURNED_UNSENT
    assert result.state == "ready_for_card"
    # The symbol's position can now be offered on a new card.
    second = await _card(sleeve, handoff_id)
    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None
        assert handoff.claim_count == 2 and handoff.proposal_id == second.proposal_id


@pytest.mark.asyncio
async def test_o1_o4_a_counted_send_attempt_keeps_the_claim_and_unmeasures_the_sleeve(
    proven: None,
) -> None:
    """The approval core was entered (the fence counted an attempt) and nothing
    proves the request did not go out. The card later reads expired, exactly as
    after a rolled-back approval. That is not proof: the handoff stays claimed,
    and with no broker reading the whole sleeve becomes unmeasured.
    """
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _card(sleeve, handoff_id)
    await fence_card_send(
        AsyncSessionLocal,
        handoff_id=handoff_id,
        proposal_id=card.proposal_id,
        configured_account_ref=ACCOUNT_REF,
        now=_now(),
    )
    await _expire(card)

    result = await _settle(handoff_id)

    assert result.outcome is SettleOutcome.HELD_UNMEASURED
    assert result.state == "card_claimed"
    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None and handoff.fills_unmeasured is True
        assert handoff.send_attempts == 1 and handoff.send_attempts_proven_unsent == 0
        inputs = await sleeve_store.load_sleeve_inputs(session, sleeve)
    book = compute_sleeve_book(orders=inputs.orders, fills=inputs.fills, marks={})
    assert book.unproven_orders and book.net_pnl is None and book.used is None


@pytest.mark.asyncio
async def test_the_fence_refuses_a_card_that_lost_the_claim_or_the_account(
    proven: None,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _card(sleeve, handoff_id)

    # A lost claim proves nothing about this card. A wrong account is refused
    # after the fence saw the claim held and nothing unexplained, so it says
    # which claim provably did not post.
    for proposal_id, account_ref, code, unsent_claim in (
        (uuid.uuid4(), ACCOUNT_REF, "exit_handoff_claim_lost", None),
        (
            card.proposal_id,
            "upbit:upbit:" + "c" * 64,
            "exit_handoff_account_mismatch",
            1,
        ),
        (card.proposal_id, None, "exit_handoff_account_mismatch", 1),
    ):
        with pytest.raises(ExitHandoffError) as refused:
            await fence_card_send(
                AsyncSessionLocal,
                handoff_id=handoff_id,
                proposal_id=proposal_id,
                configured_account_ref=account_ref,
                now=_now(),
            )
        assert refused.value.code == code
        assert refused.value.unsent_claim_count == unsent_claim
    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None and handoff.send_attempts == 0


# ------------------------------------------------------------------ S6


@pytest.mark.asyncio
async def test_s6_an_operator_exit_sells_under_a_revoked_mandate() -> None:
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    store = UpbitGatewayStore(AsyncSessionLocal)
    buy = _intent(mandate)
    await store.store_intent(buy, now=_now(), reads=_reads())
    path, shadow, _live = _path(store, avg="2000")
    assert (await path.handle(_submit(buy, _key(), quantity="10"))).status == "shadowed"

    async with AsyncSessionLocal() as session:
        await revoke_mandate(
            session, mandate_id=mandate.id, reason="db test", actor="test-operator"
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        exit_row = await create_operator_exit(
            session,
            mandate_id=mandate.id,
            symbol=SYMBOL,
            max_quantity="10",
            reason="db test exit",
            actor="test-operator",
            now=_now(),
        )
        await session.commit()

    def exit_intent(**overrides: Any) -> ExecutionIntentV2:
        return _intent(
            mandate,
            side="sell",
            origin="operator_exit",
            operator_exit_id=exit_row.id,
            authorization_ref=f"operator-exit:{exit_row.id}",
            max_quantity_decimal="10",
            expires_at=exit_row.expires_at,
            inventory=DecimalInventoryConstraint(
                position_at_authorization="10",
                min_position_after="0",
                max_position_after="10",
            ),
            **overrides,
        )

    # A strategy sell is refused by the revocation; the operator exit is not.
    with pytest.raises(IntentRefused):
        await store.store_intent(
            _intent(mandate, side="sell", max_quantity_decimal="10"),
            now=_now(),
            reads=_reads(),
        )
    sell = exit_intent()
    await store.store_intent(sell, now=_now(), reads=_reads())
    key = _key()
    outcome = await path.handle(_submit(sell, key, quantity="10"))

    assert outcome.status == "shadowed" and key in shadow.calls
    assert await store.shadow_positions(sleeve.id) == []


# ------------------------------------------------------------------ I7 (b)


@pytest.mark.asyncio
async def test_the_account_kill_row_is_read_fail_closed_and_cannot_be_deleted() -> None:
    control = PostgresAccountControlStore("upbit_live", AsyncSessionLocal)
    async with AsyncSessionLocal() as session:
        await set_account_mode(
            session,
            account="upbit_live",
            mode="GLOBAL_FREEZE",
            reason="db test",
            actor="test-operator",
        )
        await session.commit()
    assert (await control.read()).mode is KillMode.GLOBAL_FREEZE
    # An ACTIVE row is read as ACTIVE, not as the freeze a failed read becomes.
    for mode in ("ACTIVE", "GLOBAL_FREEZE"):
        async with AsyncSessionLocal() as session:
            await set_account_mode(
                session,
                account="upbit_live",
                mode=mode,
                reason="db test",
                actor="test-operator",
            )
            await session.commit()
        assert (await read_effective_kill_switch(control)).mode is KillMode(mode)
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "DELETE FROM review.execution_account_control WHERE account = 'upbit_live'"
                )
            )


# ------------------------------------------------------------------ B10


@pytest.mark.asyncio
async def test_b10_a_revocation_lists_the_children_reserved_before_it() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    key = _key()
    reservation = await store.reserve(
        _submit(intent, key, quantity="1"),
        request_digest="digest",
        decide=lambda *_a: UpbitDecision(),
        now_fn=_now,
    )
    assert reservation.is_new is True

    async with AsyncSessionLocal() as session:
        _row, pending = await revoke_mandate(
            session, mandate_id=mandate.id, reason="db test", actor="test-operator"
        )
        await session.commit()

    assert [(child.idempotency_key, child.state) for child in pending] == [
        (key, "sending")
    ]
    # After the commit every new child of the mandate is refused.
    path, _shadow, live = _path(store)
    refused = await path.handle(_submit(intent, _key(), quantity="1"))
    assert refused.reason == "mandate_revoked" and live.calls == []


# ------------------------------------------------------------------ S5 (c), O2


@pytest.mark.asyncio
async def test_s5_an_unresolved_order_keeps_the_handoff_open() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    # A child that was reserved and whose outcome nobody recorded: it may be an
    # order at the broker, under a uuid nobody knows.
    await store.reserve(
        _submit(intent, _key(), quantity="1"),
        request_digest="digest",
        decide=lambda *_a: UpbitDecision(),
        now_fn=_now,
    )
    handoff_id = await _handoff(sleeve, mandate)

    async with AsyncSessionLocal() as session:
        state = await exit_handoffs.advance_exit_handoff(
            session, handoff_id, now=_now()
        )
        await session.commit()

    assert state == "open"


async def _sent_card(
    sleeve: StrategySleeve, mandate: ExecutionMandate
) -> tuple[int, Any]:
    """A claimed card whose rung the broker took and then cancelled part-filled."""
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _card(sleeve, handoff_id)
    await fence_card_send(
        AsyncSessionLocal,
        handoff_id=handoff_id,
        proposal_id=card.proposal_id,
        configured_account_ref=ACCOUNT_REF,
        now=_now(),
    )
    async with AsyncSessionLocal() as session:
        rung = await session.scalar(
            select(OrderProposalRung).where(OrderProposalRung.proposal_pk == card.id)
        )
        assert rung is not None
        rung.broker_order_id = "uuid-card"
        rung_id = rung.id
        await session.commit()
    return handoff_id, rung_id


def _ledger_total(
    monkeypatch: pytest.MonkeyPatch, rung_id: int, quantity: str | None
) -> None:
    """What the ledger attributes to the card's rung (``None`` = unattributed)."""
    from types import SimpleNamespace

    async def inputs(_session: Any, _sleeve: Any) -> Any:
        if quantity is None:
            return SimpleNamespace(fills=[], unattributed_rungs=(rung_id,))
        return SimpleNamespace(
            fills=[SimpleNamespace(rung_id=rung_id, quantity=Decimal(quantity))],
            unattributed_rungs=(),
        )

    monkeypatch.setattr(exit_handoffs, "load_sleeve_inputs", inputs)


async def _settle_with(handoff_id: int, reading: Any) -> Any:
    async with AsyncSessionLocal() as session:
        result = await settle_exit_handoff(
            session, handoff_id, expected_claim_count=1, reading=reading, now=_now()
        )
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_s5_a_part_filled_then_cancelled_card_returns_the_remainder(
    proven: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, rung_id = await _sent_card(sleeve, mandate)
    _ledger_total(monkeypatch, rung_id, "15")
    reading = exit_handoffs.BrokerOrderReading("uuid-card", "cancel", Decimal("15"))

    result = await _settle_with(handoff_id, reading)

    # The broker and the ledger agree, and a position is left (the fixture's 100).
    assert result.outcome is SettleOutcome.RETURNED_REMAINDER
    assert result.state == "ready_for_card"
    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None and handoff.proposal_id is None
        assert handoff.fills_unmeasured is False


@pytest.mark.asyncio
async def test_s5_a_card_that_sold_everything_closes_the_handoff(
    proven: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, rung_id = await _sent_card(sleeve, mandate)
    _ledger_total(monkeypatch, rung_id, "40")

    async def nothing_left(_session: Any, _sleeve: Any, _symbol: str) -> Decimal:
        return Decimal("0")

    monkeypatch.setattr(exit_handoffs, "proven_holdings", nothing_left)
    reading = exit_handoffs.BrokerOrderReading("uuid-card", "done", Decimal("40"))

    result = await _settle_with(handoff_id, reading)

    assert result.outcome is SettleOutcome.CLOSED and result.state == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger", "reading"),
    [
        # The ledger lags the broker.
        ("10", exit_handoffs.BrokerOrderReading("uuid-card", "cancel", Decimal("15"))),
        # The broker could not be read.
        ("15", None),
        ("15", exit_handoffs.UNREADABLE_ORDER),
        # The order is still open at the broker.
        ("15", exit_handoffs.BrokerOrderReading("uuid-card", "wait", Decimal("15"))),
        # The ledger could not attribute the rung.
        (None, exit_handoffs.BrokerOrderReading("uuid-card", "cancel", Decimal("15"))),
    ],
)
async def test_o2_o4_an_unproven_sent_card_keeps_the_claim_and_is_unmeasured(
    proven: None, monkeypatch: pytest.MonkeyPatch, ledger: str | None, reading: Any
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, rung_id = await _sent_card(sleeve, mandate)
    _ledger_total(monkeypatch, rung_id, ledger)

    result = await _settle_with(handoff_id, reading)

    assert result.outcome is SettleOutcome.HELD_UNMEASURED
    assert result.state == "card_claimed"
    async with AsyncSessionLocal() as session:
        handoff = await session.get(ExecutionExitHandoff, handoff_id)
        assert handoff is not None and handoff.fills_unmeasured is True
    # No second card while this one is unproven.
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)


# ------------------------------------------------- review round 1 (PR 2486)


async def _claimed_card(sleeve: StrategySleeve, mandate: ExecutionMandate) -> Any:
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    return handoff_id, await _card(sleeve, handoff_id)


async def _fence(handoff_id: int, card: OrderProposal) -> int:
    return await fence_card_send(
        AsyncSessionLocal,
        handoff_id=handoff_id,
        proposal_id=card.proposal_id,
        configured_account_ref=ACCOUNT_REF,
        now=_now(),
    )


async def _handoff_row(handoff_id: int) -> ExecutionExitHandoff:
    async with AsyncSessionLocal() as session:
        row = await session.get(ExecutionExitHandoff, handoff_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_o1_o3_a_second_click_cannot_post_while_the_first_attempt_is_unexplained(
    proven: None,
) -> None:
    """An approval reached the POST and rolled back: the card is clickable again.
    The committed count is what refuses the second request."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, card = await _claimed_card(sleeve, mandate)

    # The attempt is counted on the claim the card holds.
    assert await _fence(handoff_id, card) == 1
    with pytest.raises(ExitHandoffError) as refused:
        await _fence(handoff_id, card)

    assert refused.value.code == "exit_handoff_prior_attempt_unresolved"
    # An unexplained attempt: this refusal is no proof that the card is unsent.
    assert refused.value.unsent_claim_count is None
    row = await _handoff_row(handoff_id)
    assert row.send_attempts == 1 and row.send_attempts_proven_unsent == 0


@pytest.mark.asyncio
async def test_o1_an_attempt_proven_unsent_lets_the_card_try_again(
    proven: None,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, card = await _claimed_card(sleeve, mandate)

    claim_count = await _fence(handoff_id, card)
    assert (await _handoff_row(handoff_id)).fills_unmeasured is True
    # A proof bound to another claim of this handoff explains nothing here.
    stale = await exit_handoffs.record_card_proven_unsent(
        AsyncSessionLocal,
        handoff_id=handoff_id,
        proposal_id=card.proposal_id,
        claim_count=claim_count + 1,
        now=_now(),
    )
    assert stale is False
    assert (await _handoff_row(handoff_id)).send_attempts_proven_unsent == 0
    recorded = await exit_handoffs.record_card_proven_unsent(
        AsyncSessionLocal,
        handoff_id=handoff_id,
        proposal_id=card.proposal_id,
        claim_count=claim_count,
        now=_now(),
    )

    assert recorded is True
    row = await _handoff_row(handoff_id)
    assert row.send_attempts == 1 and row.send_attempts_proven_unsent == 1
    # Every attempt is explained again, so the sleeve is measured again.
    assert row.fills_unmeasured is False
    await _fence(handoff_id, card)
    row = await _handoff_row(handoff_id)
    assert row.send_attempts == 2 and row.fills_unmeasured is True


@pytest.mark.asyncio
async def test_o4_the_send_fence_unmeasures_the_sleeve_before_anybody_settles(
    proven: None,
) -> None:
    """The card sold XRP, the ledger lags and no resolver has run. A BTC buy of
    the same sleeve is refused from the moment the card may have posted."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    buy = _intent(
        mandate,
        symbol="KRW-BTC",
        limit_price_krw=150_000_000,
        max_quantity_decimal="0.001",
    )
    await store.store_intent(buy, now=_now(), reads=_reads())
    handoff_id, card = await _claimed_card(sleeve, mandate)

    await _fence(handoff_id, card)

    assert (await _handoff_row(handoff_id)).fills_unmeasured is True
    async with AsyncSessionLocal() as session:
        inputs = await sleeve_store.load_sleeve_inputs(session, sleeve)
    book = compute_sleeve_book(orders=inputs.orders, fills=inputs.fills, marks={})
    assert book.unproven_orders and book.net_pnl is None and book.used is None
    path, _shadow, live = _path(store)
    key = _key()
    outcome = await path.handle(
        UpbitSubmitChild(
            action="submit",
            intent_id=buy.intent_id,
            idempotency_key=key,
            quantity_decimal="0.001",
            limit_price_krw=150_000_000,
        )
    )
    assert outcome.status == "refused"
    assert outcome.reason == "sleeve_pnl_unmeasured"
    assert live.calls == []


@pytest.mark.asyncio
async def test_the_handoff_guard_refuses_a_send_attempt_over_an_unexplained_one(
    proven: None,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, card = await _claimed_card(sleeve, mandate)

    async def bump(**values: Any) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(ExecutionExitHandoff)
                .where(ExecutionExitHandoff.id == handoff_id)
                .values(**values)
            )
            await session.commit()

    # A first attempt that does not mark the sleeve unmeasured.
    with pytest.raises(DBAPIError):
        await bump(send_attempts=1)
    # Two at once.
    with pytest.raises(DBAPIError):
        await bump(send_attempts=2, fills_unmeasured=True)
    await _fence(handoff_id, card)
    # A second attempt over the unexplained first, written by hand.
    with pytest.raises(DBAPIError):
        await bump(send_attempts=2, fills_unmeasured=True)
    # More proven unsent than were ever attempted.
    with pytest.raises(DBAPIError):
        await bump(send_attempts_proven_unsent=2)
    row = await _handoff_row(handoff_id)
    assert row.send_attempts == 1 and row.send_attempts_proven_unsent == 0


@pytest.mark.asyncio
async def test_a_settle_decides_on_the_handoff_as_it_is_under_the_lock(
    proven: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver read the handoff with no send attempt, then waited for the
    sleeve lock while an approval committed its fence and posted. The card then
    expired. Releasing it as unsent would let a second card sell again."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id, card = await _claimed_card(sleeve, mandate)
    await _expire(card)
    real_lock = exit_handoffs.acquire_sleeve_lock
    raced: list[bool] = []

    async def lock_after_a_fence(session: Any, sleeve_id: int) -> None:
        if not raced:
            raced.append(True)
            await _fence(handoff_id, card)
        await real_lock(session, sleeve_id)

    monkeypatch.setattr(exit_handoffs, "acquire_sleeve_lock", lock_after_a_fence)

    result = await _settle(handoff_id)

    assert raced == [True]
    assert result.outcome is SettleOutcome.HELD_UNMEASURED
    assert result.state == "card_claimed"
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)


@pytest.mark.asyncio
async def test_s6_the_trigger_keeps_the_mandates_price_band_on_an_operator_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    store = UpbitGatewayStore(AsyncSessionLocal)
    async with AsyncSessionLocal() as session:
        exit_row = await create_operator_exit(
            session,
            mandate_id=mandate.id,
            symbol=SYMBOL,
            max_quantity="10",
            reason="db test exit",
            actor="test-operator",
            now=_now(),
        )
        await session.commit()

    def exit_intent(**overrides: Any) -> ExecutionIntentV2:
        return _intent(
            mandate,
            side="sell",
            origin="operator_exit",
            operator_exit_id=exit_row.id,
            authorization_ref=f"operator-exit:{exit_row.id}",
            max_quantity_decimal="10",
            expires_at=exit_row.expires_at,
            inventory=DecimalInventoryConstraint(
                position_at_authorization="10",
                min_position_after="0",
                max_position_after="10",
            ),
            **overrides,
        )

    # The rule refuses a 31 bp exit under a 30 bp mandate ...
    with pytest.raises(IntentRefused) as refused:
        await store.store_intent(
            exit_intent(max_slippage_bps=31), now=_now(), reads=_reads()
        )
    assert refused.value.refusal.value == "mandate_scope_mismatch"
    # ... and so does the trigger, with the rule out of the way.
    monkeypatch.setattr(
        upbit_store_module, "check_storable", lambda *_a, **_k: UpbitDecision()
    )
    with pytest.raises(DBAPIError):
        await store.store_intent(
            exit_intent(max_slippage_bps=31), now=_now(), reads=_reads()
        )
    async with AsyncSessionLocal() as session:
        stored = await session.scalar(
            select(ExecutionIntent.id).where(
                ExecutionIntent.operator_exit_id == exit_row.id
            )
        )
    assert stored is None


@pytest.mark.asyncio
async def test_b12_the_store_reads_the_account_a_cancel_must_go_to() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())

    assert await store.mandate_account_ref(intent.intent_id) == ACCOUNT_REF
    assert await store.mandate_account_ref(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_b3_the_ledger_row_is_recovered_before_the_final_evidence() -> None:
    """Final evidence makes a child resolved, and a resolved child is never
    looked at again. A ledger write that failed must leave it pending."""
    from app.models.execution_mandates import ExecutionChildOrderEvidence
    from scripts import execution_intent_upbit_resolve as resolver

    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    path, _shadow, live = _path(store)
    live.result = ForwardResult(
        accepted=False, broker_order_id=None, result={"success": False}
    )
    key = _key()
    assert (await path.handle(_submit(intent, key))).status == "uncertain"
    child_id = (await _child(key)).id

    async def reader(_order_uuid: str | None, identifier: str | None) -> Any:
        if identifier != key:
            return None
        return {
            "uuid": "uuid-recovered",
            "state": "cancel",
            "executed_volume": "0",
            "remaining_volume": "10",
        }

    written: list[int] = []

    async def failing(order: Any, _reading: Any) -> str:
        raise RuntimeError("ledger down")

    async def working(order: Any, _reading: Any) -> str:
        written.append(order.child_id)
        return "recorded"

    async def run(writer: Any) -> dict[str, Any]:
        report = await resolver._resolve_orders(
            AsyncSessionLocal,
            reader=reader,
            commit=True,
            now=_now(),
            ledger_writer=writer,
        )
        return next(entry for entry in report if entry["child_id"] == child_id)

    async def evidence() -> Any:
        async with AsyncSessionLocal() as session:
            return await session.get(ExecutionChildOrderEvidence, child_id)

    first = await run(failing)
    assert first["result"] == "unresolved"
    assert first["detail"] == "live_order_ledger_not_written"
    assert await evidence() is None

    second = await run(working)
    assert second["result"] == "inserted" and second["live_order_ledger"] == "recorded"
    assert written == [child_id]
    row = await evidence()
    assert row is not None and row.broker_state == "cancel"


@pytest.mark.asyncio
async def test_the_mandate_list_reads_rows_that_exist() -> None:
    from scripts import execution_mandates as mandates_cli

    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)

    async with AsyncSessionLocal() as session:
        listed = await mandates_cli._list(session, now=_now())

    [row] = [m for m in listed["mandates"] if m["mandate_id"] == str(mandate.id)]
    assert row["state"] == "active" and row["sleeve_id"] == sleeve.id
    [handoff] = [
        h for h in listed["open_exit_handoffs"] if h["handoff_id"] == handoff_id
    ]
    assert handoff["state"] == "open" and handoff["fills_unmeasured"] is False


# ------------------------------------------------- review round 2 (PR 2486)


async def _reserved_live_buy(mandate: ExecutionMandate) -> tuple[Any, int]:
    """A live buy that passed its checks and has no outcome yet (``sending``)."""
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    reservation = await store.reserve(
        _submit(intent, _key(), quantity="1"),
        request_digest="digest",
        decide=lambda *_a: UpbitDecision(),
        now_fn=_now,
    )
    assert reservation.child is not None
    return store, reservation.child.child_id


@pytest.mark.asyncio
async def test_o4_the_card_fence_waits_for_a_live_buy_that_is_on_its_way_out(
    proven: None,
) -> None:
    """The buy read a measured sleeve and is between its last look and its
    request. A card that posted now would make that look stale, so the card is
    not sent until the buy has an outcome."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store, child_id = await _reserved_live_buy(mandate)
    handoff_id, card = await _claimed_card(sleeve, mandate)

    with pytest.raises(ExitHandoffError) as refused:
        await _fence(handoff_id, card)

    assert refused.value.code == "exit_handoff_buy_in_flight"
    # Nothing was counted and nothing is unexplained: the claim is named.
    assert refused.value.unsent_claim_count == 1
    row = await _handoff_row(handoff_id)
    assert row.send_attempts == 0 and row.fills_unmeasured is False

    assert await store.finish(
        child_id,
        state=ChildState.ACCEPTED,
        broker_order_id="uuid-buy",
        result={},
        now=_now(),
    )
    await _fence(handoff_id, card)
    assert (await _handoff_row(handoff_id)).send_attempts == 1


@pytest.mark.asyncio
async def test_o4_a_buy_whose_order_the_broker_ended_no_longer_holds_the_card_back(
    proven: None,
) -> None:
    """A journal row is never rewritten: a buy whose answer was lost stays
    ``sending``. The broker's final reading of its order is what ends the wait."""
    from app.services.execution_intent_gateway.order_evidence import (
        OrderReading,
        record_child_order_evidence,
    )

    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    _store, child_id = await _reserved_live_buy(mandate)
    handoff_id, card = await _claimed_card(sleeve, mandate)
    with pytest.raises(ExitHandoffError):
        await _fence(handoff_id, card)

    async with AsyncSessionLocal() as session:
        recorded = await record_child_order_evidence(
            session,
            child_id=child_id,
            reading=OrderReading(
                order_uuid="uuid-lost-answer",
                state="cancel",
                executed_volume=Decimal("0"),
                remaining_volume=Decimal("1"),
            ),
            observed_at=_now(),
            recorded_by="test",
        )
        await session.commit()
    assert recorded == "inserted"

    await _fence(handoff_id, card)
    assert (await _handoff_row(handoff_id)).send_attempts == 1


@pytest.mark.asyncio
async def test_a_reused_key_is_found_before_a_shape_refusal() -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    key = _key()
    assert await store.key_is_journaled(key) is False
    path, _shadow, _live = _path(store)
    assert (await path.handle(_submit(intent, key))).status == "accepted"

    assert await store.key_is_journaled(key) is True


# ------------------------------------------------- review round 3 (PR 2486)


@pytest.mark.asyncio
async def test_a_shape_refusal_waits_for_a_reservation_of_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 2486 review (65wMjP): a valid submit has inserted its ``sending`` row
    for key K and not committed yet, and a malformed request with K asks
    whether K is journaled. A plain read does not see the row and would answer
    a definite refusal; the reservation then commits and sends. The lookup
    waits for the key the reservation holds, and then finds the row."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    key = _key()
    inserted, release = asyncio.Event(), asyncio.Event()

    async def pause_before_the_commit(*_args: Any, **_kwargs: Any) -> None:
        # ``reserve`` awaits this after it flushed the row and before it commits.
        inserted.set()
        await release.wait()

    monkeypatch.setattr(store, "_stop_sleeve", pause_before_the_commit)
    reservation = asyncio.create_task(
        store.reserve(
            _submit(intent, key, quantity="1"),
            request_digest="digest",
            decide=lambda *_a: UpbitDecision(),
            now_fn=_now,
        )
    )
    try:
        await asyncio.wait_for(inserted.wait(), timeout=10)
        lookup = asyncio.create_task(
            UpbitGatewayStore(AsyncSessionLocal).key_is_journaled(key)
        )
        _done, pending = await asyncio.wait({lookup}, timeout=0.5)
        assert lookup in pending, "the key was read past an uncommitted reservation"
    finally:
        release.set()

    reserved = await reservation
    assert reserved.is_new is True and reserved.child is not None
    assert await lookup is True


@pytest.mark.asyncio
async def test_a_reservation_takes_its_key_before_any_other_lock() -> None:
    """The key lock is the first thing a reservation waits for: while another
    transaction holds the key, the reservation has not taken the sleeve lock,
    so a different key of the same sleeve is reserved meanwhile."""
    from app.services.execution_intent_gateway.store import lock_child_key

    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    store = UpbitGatewayStore(AsyncSessionLocal)
    intent = _intent(mandate)
    await store.store_intent(intent, now=_now(), reads=_reads())
    held, free = _key(), _key()

    async def reserve(key: str) -> Any:
        return await store.reserve(
            _submit(intent, key, quantity="1"),
            request_digest="digest",
            decide=lambda *_a: UpbitDecision(),
            now_fn=_now,
        )

    async with AsyncSessionLocal() as holder:
        await lock_child_key(holder, held)
        waiting = asyncio.create_task(reserve(held))
        _done, pending = await asyncio.wait({waiting}, timeout=0.5)
        assert waiting in pending
        other = await asyncio.wait_for(reserve(free), timeout=10)
        assert other.is_new is True
        await holder.rollback()
    assert (await waiting).is_new is True


async def _operator_exit(mandate: ExecutionMandate) -> Any:
    async with AsyncSessionLocal() as session:
        exit_row = await create_operator_exit(
            session,
            mandate_id=mandate.id,
            symbol=SYMBOL,
            max_quantity="10",
            reason="db test exit",
            actor="test-operator",
            now=_now(),
        )
        await session.commit()
        return exit_row


def _exit_intent_stored_at(
    mandate: ExecutionMandate, exit_row: Any, stored_at: datetime, **overrides: Any
) -> ExecutionIntentV2:
    """What ``execution_mandates.py exit`` builds for the store's clock."""
    values: dict[str, Any] = {
        "side": "sell",
        "origin": "operator_exit",
        "operator_exit_id": exit_row.id,
        "authorization_ref": f"operator-exit:{exit_row.id}",
        "max_quantity_decimal": "10",
        "expires_at": min(exit_row.expires_at, stored_at + mandate.max_intent_ttl),
        "inventory": DecimalInventoryConstraint(
            position_at_authorization="10",
            min_position_after="0",
            max_position_after="10",
        ),
    }
    return _intent(mandate, **{**values, **overrides})


async def _intent_row(intent_id: uuid.UUID) -> ExecutionIntent:
    async with AsyncSessionLocal() as session:
        row = await session.get(ExecutionIntent, intent_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_s6_the_wait_for_the_store_does_not_use_up_an_exit_intents_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 2486 review (65wMjR): the mandate lets an intent live three seconds, and
    the store waits longer than that for the sleeve lock. An expiry fixed
    before the wait is past when the row is inserted. The intent is built from
    the clock read after the wait, the row's ``created_at`` is that instant,
    and the trigger measures the lifetime from it."""
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow", max_intent_ttl_seconds="3")
    exit_row = await _operator_exit(mandate)
    store = UpbitGatewayStore(AsyncSessionLocal)
    # The rule is out of the way: the trigger alone judges the row.
    monkeypatch.setattr(
        upbit_store_module, "check_storable", lambda *_a, **_k: UpbitDecision()
    )
    built: list[datetime] = []

    def build(stored_at: datetime) -> ExecutionIntentV2:
        built.append(stored_at)
        return _exit_intent_stored_at(mandate, exit_row, stored_at)

    async with AsyncSessionLocal() as holder:
        locked_at = await holder.scalar(select(func.clock_timestamp()))
        await sleeve_store.acquire_sleeve_lock(holder, sleeve.id)
        storing = asyncio.create_task(
            store.store_intent_for_operator_exit(
                build, sleeve_id=sleeve.id, mandate_id=mandate.id, reads=_reads()
            )
        )
        await asyncio.sleep(3.5)
        assert not storing.done() and built == []
        await holder.rollback()
    stored = await storing

    assert len(built) == 1
    # The wait alone was longer than the intent may live.
    assert built[0] - locked_at > mandate.max_intent_ttl
    row = await _intent_row(stored.intent_id)
    assert row.created_at == built[0]
    assert row.expires_at == built[0] + mandate.max_intent_ttl == stored.expires_at


@pytest.mark.asyncio
async def test_s6_an_exit_intent_stored_at_the_stores_clock_can_then_be_sent() -> None:
    """The stored ``created_at`` is what every later check measures the
    lifetime from: an intent that lives exactly ``max_intent_ttl`` from its
    storage is accepted by the rule and the trigger at the store, and by the
    rule again when its child is reserved."""
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow", max_intent_ttl_seconds="60")
    store = UpbitGatewayStore(AsyncSessionLocal)
    buy = _intent(mandate, expires_at=_now() + timedelta(seconds=45))
    await store.store_intent(buy, now=_now(), reads=_reads())
    path, shadow, _live = _path(store, avg="2000")
    assert (await path.handle(_submit(buy, _key(), quantity="10"))).status == "shadowed"
    exit_row = await _operator_exit(mandate)

    stored = await store.store_intent_for_operator_exit(
        lambda stored_at: _exit_intent_stored_at(mandate, exit_row, stored_at),
        sleeve_id=sleeve.id,
        mandate_id=mandate.id,
        reads=_reads(),
    )

    row = await _intent_row(stored.intent_id)
    assert row.expires_at - row.created_at == mandate.max_intent_ttl
    key = _key()
    outcome = await path.handle(_submit(stored, key, quantity="10"))
    assert outcome.status == "shadowed" and key in shadow.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("shift", [timedelta(minutes=5), -timedelta(hours=1)])
async def test_s6_the_trigger_believes_created_at_only_inside_its_own_transaction(
    monkeypatch: pytest.MonkeyPatch, shift: timedelta
) -> None:
    """``created_at`` is what an exit intent's lifetime is measured from, so it
    cannot be a time the writer made up: not later than the clock at the
    INSERT, which would lengthen the lifetime, and not before the transaction
    began."""
    sleeve = await _sleeve("shadow")
    mandate = await _mandate(sleeve, "shadow")
    exit_row = await _operator_exit(mandate)
    store = UpbitGatewayStore(AsyncSessionLocal)
    monkeypatch.setattr(
        upbit_store_module, "check_storable", lambda *_a, **_k: UpbitDecision()
    )
    claimed = _now() + shift
    intent = _exit_intent_stored_at(
        mandate, exit_row, claimed, expires_at=_now() + timedelta(minutes=30)
    )

    with pytest.raises(DBAPIError):
        async with AsyncSessionLocal() as session:
            await store._store_locked(
                session, intent, now=_now(), reads=_reads(), created_at=claimed
            )
    async with AsyncSessionLocal() as session:
        assert await session.get(ExecutionIntent, intent.intent_id) is None
