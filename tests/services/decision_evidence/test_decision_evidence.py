from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.decision_evidence import (
    DecisionEvidence,
    DecisionEvidenceService,
    DecisionRung,
    ReferencePrice,
    account_scope,
    evidence_hash,
)

pytestmark = pytest.mark.unit


def sample():
    return DecisionEvidence(
        decision_kind="proposal",
        decided_by="session",
        market="equity_us",
        symbol="BRK.B",
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        stop_price=Decimal("1.2300"),
        stop_source="exit_plan",
    )


def test_hash_is_stable_for_equivalent_decimal_and_instant_and_changes_with_evidence():
    first = sample()
    equivalent = replace(
        first,
        stop_price=Decimal("1.23"),
        decided_at=first.decided_at.astimezone(timezone(timedelta(hours=9))),
    )
    assert evidence_hash(first) == evidence_hash(equivalent)
    assert evidence_hash(first) != evidence_hash(replace(first, symbol="OTHER"))
    assert len(evidence_hash(first)) == 64


def test_hash_rejects_floats_and_naive_times():
    with pytest.raises(TypeError):
        evidence_hash(replace(sample(), stop_price=1.2))
    with pytest.raises(ValueError):
        evidence_hash(replace(sample(), decided_at=datetime(2026, 1, 1)))


@pytest.mark.asyncio
async def test_record_writes_evidence_rungs_and_prices_in_caller_transaction():
    session = AsyncMock()
    service = DecisionEvidenceService(session)
    decision_id = await service.record(
        sample(),
        [DecisionRung(rung_index=0, side="buy", planned_quantity=Decimal("2"))],
        [ReferencePrice(kind="quote_at_decision", price=Decimal("1.23"))],
    )
    calls = session.execute.await_args_list
    assert len(calls) == 3
    assert [str(call.args[0]).split(" (")[0] for call in calls] == [
        "INSERT INTO review.decision_evidence",
        "INSERT INTO review.decision_evidence_rungs",
        "INSERT INTO review.decision_reference_prices",
    ]
    assert all(call.args[1]["decision_id"] == decision_id for call in calls)
    assert calls[0].args[1]["evidence_hash"] == evidence_hash(sample())
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_and_outcome_are_separate_appends_and_scope_uses_ledger_vocabulary():
    session = AsyncMock()
    service = DecisionEvidenceService(session)
    attempt_id = uuid4()
    await service.link_order(
        attempt_id=attempt_id,
        decision_id=uuid4(),
        account_scope=account_scope("upbit"),
        send_key="key",
    )
    await service.record_outcome(attempt_id, "sent")
    assert session.execute.await_args_list[0].args[1]["account_scope"] == "upbit_live"
    assert session.execute.await_args_list[1].args[1] == {
        "attempt_id": attempt_id,
        "outcome": "sent",
    }
    session.commit.assert_not_awaited()


def test_migration_adds_immutable_tables_unique_keys_and_conditional_grants(
    monkeypatch,
):
    path = Path("alembic/versions/20261112_hk4182_decision_evidence.py")
    spec = spec_from_file_location("migration_4182", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    monkeypatch.setattr(module.op, "execute", statements.append)
    module.upgrade()
    sql = "\n".join(statements)
    for table in module.TABLES:
        assert f"CREATE TABLE review.{table}" in sql
        assert f"BEFORE UPDATE OR DELETE ON review.{table}" in sql
        assert f"BEFORE TRUNCATE ON review.{table}" in sql
        assert f"GRANT SELECT, INSERT ON review.{table} TO at_app" in sql
        assert f"review.{table}_id_seq" in sql
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in sql
    assert "GRANT USAGE ON SCHEMA review TO at_app" in sql
    assert sql.count("REFERENCES review.decision_evidence(decision_id)") == 3
    assert sql.count("CHECK (rung_index >= 0)") == 3
    assert "decision_id uuid NOT NULL UNIQUE" in sql
    assert "UNIQUE (decision_id, rung_index)" in sql
    assert "(decision_id, COALESCE(rung_index, -1), kind)" in sql
    assert sql.count("attempt_id uuid NOT NULL UNIQUE") == 2
    assert "UNIQUE (account_scope, send_key)" not in sql
    statements.clear()
    module.downgrade()
    assert all("IF EXISTS (SELECT 1 FROM review." in s for s in statements[:5])


@pytest.mark.asyncio
async def test_float_reference_price_is_rejected_before_any_write():
    session = AsyncMock()
    service = DecisionEvidenceService(session)
    with pytest.raises(ValueError):
        await service.record(
            sample(), [], [ReferencePrice(kind="candidate_price", price=1.25)]
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_naive_reference_timestamp_is_rejected_before_any_write():
    session = AsyncMock()
    service = DecisionEvidenceService(session)
    price = ReferencePrice(
        kind="quote_at_decision", price=Decimal("1"), asof=datetime(2026, 1, 1)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        await service.record(sample(), [], [price])
    with pytest.raises(ValueError, match="timezone-aware"):
        await service.add_reference_price(uuid4(), price)
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_proposal_records_explicit_candidate_and_prices_in_same_session(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as proposal_evidence
    from app.services.order_proposals.service import RungInput

    now = datetime(2026, 11, 12, 2, tzinfo=UTC)
    candidate_id = uuid4()
    pick = SimpleNamespace(
        call_id=candidate_id, decision_price_text="12.30", recorded_at=now
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: pick)
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(proposal_evidence.DecisionEvidenceService, "record", record)
    result = await proposal_evidence.record_proposal_decision(
        session,
        symbol="BRK.B",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        action="place",
        proposer="session",
        strategy="test",
        sleeve_id=None,
        rungs=[RungInput(0, "buy", Decimal("2"), Decimal("13"), None)],
        source_asof={"exit_plan": {"stop_price": "10", "stop_basis": "support"}},
        candidate_ref={"call_id": str(candidate_id), "symbol": "BRK.B"},
        decision_price_text="12.5",
        now=now,
    )
    evidence, rungs, prices = record.await_args.args
    assert result == record.return_value
    assert evidence.candidate_call_id == candidate_id
    assert evidence.candidate_link_basis == "recorded"
    assert evidence.stop_price == Decimal("10") and evidence.stop_source == "exit_plan"
    assert rungs[0].planned_quantity == Decimal("2")
    assert {p.kind: p.price for p in prices} == {
        "limit": Decimal("13"),
        "quote_at_decision": Decimal("12.5"),
        "candidate_price": Decimal("12.30"),
    }
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_candidate_inference_is_admitted_and_bounded_by_kst_day(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as proposal_evidence
    from app.services.order_proposals.service import RungInput

    now = datetime(2026, 11, 12, 2, tzinfo=UTC)
    session = AsyncMock()
    pick = SimpleNamespace(call_id=uuid4(), decision_price_text=None, recorded_at=now)
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: pick)
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(proposal_evidence.DecisionEvidenceService, "record", record)
    await proposal_evidence.record_proposal_decision(
        session,
        symbol="TEST",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        action="place",
        proposer="session",
        strategy=None,
        sleeve_id=None,
        rungs=[RungInput(0, "buy", Decimal("1"), None, None)],
        source_asof={},
        candidate_ref=None,
        decision_price_text=None,
        now=now,
    )
    query = session.execute.await_args.args[0]
    params = query.compile().params
    assert "admitted" in params.values()
    assert now in params.values()
    assert (
        datetime(2026, 11, 12, tzinfo=timezone(timedelta(hours=9))) in params.values()
    )
    assert record.await_args.args[0].candidate_link_basis == "inference"
    assert record.await_args.args[0].candidate_call_id == pick.call_id


@pytest.mark.asyncio
async def test_proposal_evidence_failure_prevents_proposal_insert(monkeypatch):
    from app.services.order_proposals import service as module

    session = AsyncMock()
    service = module.OrderProposalsService(session)
    service._repo = AsyncMock()
    monkeypatch.setattr(service, "_validate_exit_binding", AsyncMock())
    monkeypatch.setattr(
        module,
        "record_proposal_decision",
        AsyncMock(side_effect=RuntimeError("failed")),
    )
    with pytest.raises(RuntimeError, match="failed"):
        await service.create_proposal(
            symbol="TEST",
            market="equity_us",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="session",
            rungs=[module.RungInput(0, "buy", Decimal("1"), Decimal("12"), None)],
            now=datetime(2026, 11, 12, tzinfo=UTC),
        )
    service._repo.insert_group.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_candidate_leaves_link_empty_and_price_must_be_exact(monkeypatch):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as proposal_evidence
    from app.services.order_proposals.service import RungInput

    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(proposal_evidence.DecisionEvidenceService, "record", record)
    kwargs = {
        "session": session,
        "symbol": "TEST",
        "market": "equity_us",
        "account_mode": "kis_live",
        "side": "buy",
        "action": "place",
        "proposer": "session",
        "strategy": None,
        "sleeve_id": None,
        "rungs": [RungInput(0, "buy", Decimal("1"), None, None)],
        "source_asof": {},
        "candidate_ref": None,
        "now": datetime(2026, 11, 12, tzinfo=UTC),
    }
    await proposal_evidence.record_proposal_decision(**kwargs, decision_price_text=None)
    evidence = record.await_args.args[0]
    assert evidence.candidate_call_id is None and evidence.candidate_link_basis is None
    with pytest.raises(ValueError, match="decimal string"):
        await proposal_evidence.record_proposal_decision(
            **kwargs, decision_price_text=1.5
        )
    assert record.await_count == 1


def test_proposal_decision_migration_is_additive_and_immutable(monkeypatch):
    path = Path("alembic/versions/20261112_hk4184_proposal_decision.py")
    spec = spec_from_file_location("migration_4184", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    monkeypatch.setattr(module.op, "execute", statements.append)
    module.upgrade()
    sql = "\n".join(statements)
    assert "FOREIGN KEY (decision_id)" in sql
    assert "OLD.decision_id IS DISTINCT FROM NEW.decision_id" in sql
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in sql
    assert "GRANT EXECUTE" in sql
    assert "UPDATE review.order_proposals SET" not in sql
    for column in (
        "stop_price",
        "target_price",
        "planned_quantity",
        "planned_notional",
        "limit_price",
        "price",
    ):
        assert f"ALTER COLUMN {column} TYPE numeric(38,12)" in sql
    statements.clear()
    module.downgrade()
    assert "EXISTS (SELECT 1 FROM review.decision_evidence)" in statements[0]
    assert all("TYPE numeric" not in statement for statement in statements)


@pytest.mark.asyncio
async def test_numeric_precision_accepts_twelve_places_and_rejects_unrepresentable_values():
    session = AsyncMock()
    service = DecisionEvidenceService(session)
    exact = Decimal("12345678901234567890123456.123456789012")
    evidence = replace(sample(), stop_price=exact)
    await service.record(
        evidence,
        [
            DecisionRung(
                rung_index=0,
                side="buy",
                planned_quantity=Decimal("0.000000000001"),
                limit_price=exact,
            )
        ],
        [ReferencePrice(kind="quote_at_decision", price=exact)],
    )
    assert session.execute.await_args_list[0].args[1]["stop_price"] == exact
    adjacent = Decimal("12345678901234567890123456.123456789013")
    assert evidence_hash(evidence) != evidence_hash(
        replace(evidence, stop_price=adjacent)
    )
    for invalid in (Decimal("0.0000000000001"), Decimal("100000000000000000000000000")):
        untouched = AsyncMock()
        with pytest.raises(ValueError, match=r"numeric\(38,12\)"):
            await DecisionEvidenceService(untouched).record(
                replace(sample(), stop_price=invalid), [], []
            )
        untouched.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_explicit_candidate_is_rejected_before_evidence(monkeypatch):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as module
    from app.services.order_proposals.service import RungInput

    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    record = AsyncMock()
    monkeypatch.setattr(module.DecisionEvidenceService, "record", record)
    with pytest.raises(ValueError, match="does not identify a candidate"):
        await module.record_proposal_decision(
            session,
            symbol="TEST",
            market="equity_us",
            account_mode="kis_live",
            side="buy",
            action="place",
            proposer="test",
            strategy=None,
            sleeve_id=None,
            rungs=[RungInput(0, "buy", Decimal("1"), None, None)],
            source_asof={},
            candidate_ref={"call_id": str(uuid4()), "symbol": "TEST"},
            decision_price_text=None,
            now=datetime(2026, 11, 12, tzinfo=UTC),
        )
    record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "action"), [("sell", "place"), ("buy", "cancel"), ("buy", "replace")]
)
async def test_inference_excludes_exit_and_target_actions(monkeypatch, side, action):
    from app.services.order_proposals import decision_evidence as module
    from app.services.order_proposals.service import RungInput

    session = AsyncMock()
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(module.DecisionEvidenceService, "record", record)
    await module.record_proposal_decision(
        session,
        symbol="TEST",
        market="equity_us",
        account_mode="kis_live",
        side=side,
        action=action,
        proposer="test",
        strategy=None,
        sleeve_id=None,
        rungs=[RungInput(0, side, Decimal("1"), None, None)],
        source_asof={},
        candidate_ref=None,
        decision_price_text=None,
        now=datetime(2026, 11, 12, tzinfo=UTC),
    )
    session.execute.assert_not_awaited()
    evidence, _, prices = record.await_args.args
    assert evidence.candidate_call_id is None and evidence.candidate_link_basis is None
    assert not any(price.kind == "candidate_price" for price in prices)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_price", ["0", "-1", "0.0000000000001", "100000000000000000000000000"]
)
async def test_invalid_candidate_observation_preserves_link_without_price(
    monkeypatch, bad_price
):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as module
    from app.services.order_proposals.service import RungInput

    now = datetime(2026, 11, 12, tzinfo=UTC)
    candidate_id = uuid4()
    pick = SimpleNamespace(
        call_id=candidate_id, decision_price_text=bad_price, recorded_at=now
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: pick)
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(module.DecisionEvidenceService, "record", record)
    await module.record_proposal_decision(
        session,
        symbol="TEST",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        action="place",
        proposer="test",
        strategy=None,
        sleeve_id=None,
        rungs=[RungInput(0, "buy", Decimal("1"), None, None)],
        source_asof={},
        candidate_ref=None,
        decision_price_text=None,
        now=now,
    )
    evidence, _, prices = record.await_args.args
    assert (
        evidence.candidate_call_id == candidate_id
        and evidence.candidate_link_basis == "inference"
    )
    assert not any(price.kind == "candidate_price" for price in prices)


@pytest.mark.asyncio
async def test_explicit_candidate_is_honoured_for_sell(monkeypatch):
    from types import SimpleNamespace

    from app.services.order_proposals import decision_evidence as module
    from app.services.order_proposals.service import RungInput

    now = datetime(2026, 11, 12, tzinfo=UTC)
    candidate_id = uuid4()
    pick = SimpleNamespace(
        call_id=candidate_id, decision_price_text="5", recorded_at=now
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: pick)
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(module.DecisionEvidenceService, "record", record)
    await module.record_proposal_decision(
        session,
        symbol="TEST",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        action="place",
        proposer="test",
        strategy=None,
        sleeve_id=None,
        rungs=[RungInput(0, "sell", Decimal("1"), None, None)],
        source_asof={},
        candidate_ref={"call_id": str(candidate_id), "symbol": "TEST"},
        decision_price_text=None,
        now=now,
    )
    evidence, _, prices = record.await_args.args
    assert (
        evidence.candidate_call_id == candidate_id
        and evidence.candidate_link_basis == "recorded"
    )
    assert any(
        price.kind == "candidate_price" and price.price == Decimal("5")
        for price in prices
    )
