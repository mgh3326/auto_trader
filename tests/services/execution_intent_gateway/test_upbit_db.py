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

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
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
from app.services.kis_mock_runner.control import KillMode
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
from tests.services.execution_intent_gateway.support import FakeKillSwitch
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
async def _account_control_row() -> None:
    """The seed row of the kill table.

    The guards themselves come from the test schema bootstrap, which installs
    this revision's ``GUARD_DDL``. No DDL runs here: a per-test ``DROP TRIGGER``
    on ``order_proposals`` would take locks other DB tests are waiting behind.
    """
    hk5441 = _migration("20261120_hk5441_upbit_intent_gateway.py")
    async with engine.begin() as conn:
        for statement in hk5441.SEED_DDL:
            await conn.execute(text(statement))


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


async def _mandate(sleeve: StrategySleeve, mode: str) -> ExecutionMandate:
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
            max_intent_ttl_seconds="43200",
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

    for proposal_id, account_ref, code in (
        (uuid.uuid4(), ACCOUNT_REF, "exit_handoff_claim_lost"),
        (card.proposal_id, "upbit:upbit:" + "c" * 64, "exit_handoff_account_mismatch"),
        (card.proposal_id, None, "exit_handoff_account_mismatch"),
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
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "DELETE FROM review.execution_account_control WHERE account = 'upbit_live'"
                )
            )
