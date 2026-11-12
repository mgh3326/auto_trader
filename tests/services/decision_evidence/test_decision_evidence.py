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
