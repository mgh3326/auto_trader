from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import Insert

from app.models.daily_plan import rows, versions
from app.services.daily_plan import store
from app.services.daily_plan.risk import (
    EVALUATED_SKIPS,
    NOT_APPLICABLE_SKIPS,
    UNAVAILABLE_SKIPS,
)
from app.services.mechanical_loss_cut.rules import SKIP_REASONS

DAY = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, 1, tzinfo=UTC)
CLOSE = datetime(2026, 11, 26, 6, 30, tzinfo=UTC)
PRICE = "90100.010000000000000000001"


def request(**overrides):
    return {
        "expected_prev_seq": 0,
        "idempotency_key": "missing-price",
        "asof": NOW.isoformat(),
        "strategy_id": "kr-plan",
        "strategy_rule_version": "v1",
        "kind": "session",
        "rows": [
            {
                "row_kind": "candidate",
                "market": "kr",
                "symbol": "278470",
                "venue": "KRX",
                "session": "regular",
                "action": "new",
                "action_basis": "scored",
                "side": "buy",
                "thesis": "Wait for new evidence",
                "max_buy_price": PRICE,
                "would_be_limit_price": PRICE,
                "would_be_quantity": "1",
                "carry_allowed": True,
            }
        ],
        **overrides,
    }


@dataclass
class Values:
    records: list[dict]

    def mappings(self):
        return self

    def one_or_none(self):
        return self.records[0] if self.records else None

    def all(self):
        return self.records

    def __iter__(self):
        return iter(self.records)


def fake_db(*, current: int):
    saved: list[dict] = []

    async def execute(stmt, *_args):
        if isinstance(stmt, Insert):
            body = dict(stmt.compile().params)
            if stmt.table is rows:
                saved.append({**body, "execution_state": "plan_only"})
            return Values([])
        if rows in stmt.get_final_froms():
            return Values(saved)
        return Values([])

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        scalar=AsyncMock(side_effect=[uuid4(), current]),
    )
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("row_kind", ["candidate", "holding"])
async def test_missing_input_publishes_watch_with_exact_price(monkeypatch, row_kind):
    monkeypatch.setattr(
        store, "previous_trading_session", lambda *_: date(2026, 11, 25)
    )
    monkeypatch.setattr(
        store,
        "regular_session_bounds",
        lambda _, day: (NOW.replace(day=day.day), CLOSE.replace(day=day.day)),
    )
    policy = SimpleNamespace(
        version="v1", daily_plan=SimpleNamespace(no_add_floor_pct=-8)
    )
    proposal = request()
    if row_kind == "holding":
        proposal["rows"][0].update(
            row_kind="holding", account_ref="a" * 64, action="add"
        )
    result = await store.write_version(
        fake_db(current=0),
        day=DAY,
        request=proposal,
        now=NOW,
        policy=policy,
        policy_hash="hash",
    )
    assert result["status"] == "PUBLISHED"
    [row] = result["rows"]
    assert row["action"] == "watch"
    assert row["not_executable_reason"] == "input_missing"
    assert row["max_buy_price"] == PRICE
    assert row["valid_until"] == CLOSE.isoformat()


@pytest.mark.asyncio
async def test_stale_request_does_not_insert_anything():
    db = fake_db(current=2)
    with pytest.raises(ValueError, match="stale_version"):
        await store.write_version(
            db,
            day=DAY,
            request=request(),
            now=NOW,
            policy=None,
            policy_hash="hash",
        )
    assert all(
        not isinstance(call.args[0], Insert) or call.args[0].table is not versions
        for call in db.execute.call_args_list
    )


def test_every_risk_skip_has_one_evidence_class():
    classes = (UNAVAILABLE_SKIPS, EVALUATED_SKIPS, NOT_APPLICABLE_SKIPS)
    assert set.union(*(set(group) for group in classes)) == SKIP_REASONS
    assert sum(len(group) for group in classes) == len(SKIP_REASONS)


@pytest.mark.asyncio
async def test_adoption_is_deterministic_and_conflict_is_rejected():
    version = {
        "head_id": uuid4(),
        "id": uuid4(),
        "input_bundle_sha256": "a" * 64,
    }
    row = {**request()["rows"][0], "disposition": "adopted"}
    db = SimpleNamespace(execute=AsyncMock(return_value=Values([])))
    first = await store._prepare_insert(db, version, row)
    assert isinstance(first, dict)
    second = await store._prepare_insert(db, {**version, "id": uuid4()}, row)
    assert isinstance(second, dict)
    assert first["adoption_key"] == second["adoption_key"]
    assert first["forecast_id"] == second["forecast_id"]

    db.execute.return_value = Values([first])
    assert await store._prepare_insert(db, version, row) == first["id"]
    with pytest.raises(ValueError, match="adoption_conflict"):
        await store._prepare_insert(db, version, {**row, "thesis": "changed"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forecast", "decisions", "rungs", "expected"),
    [
        (None, [], [], "pending"),
        ("valid", [], [], "link_no_decision"),
        ("valid", ["valid", "valid"], [], "link_ambiguous"),
        ("invalid", ["valid"], ["valid"], "link_mismatch"),
        ("valid", ["valid"], ["invalid"], "link_mismatch"),
        ("valid", ["valid"], ["valid"], "confirmed"),
    ],
)
async def test_link_confirmation_reads_only_matching_declined_evidence(
    forecast,
    decisions,
    rungs,
    expected,
):
    from decimal import Decimal

    decision_id = uuid4()
    row = {
        "forecast_id": uuid4(),
        "adoption_key": "adoption",
        "symbol": "278470",
        "body": {
            "would_be_limit_price": PRICE,
            "would_be_quantity": "1",
            "would_be_account_mode": "kis_live",
        },
    }
    forecast_rows = (
        []
        if forecast is None
        else [
            {
                "decision_bucket": "deferred_no_action",
                "market": "equity_kr",
                "symbol": row["symbol"],
                "correlation_id": row["adoption_key"]
                if forecast == "valid"
                else "other",
            }
        ]
    )
    decision_rows = [
        {
            "decision_id": decision_id,
            "market": "equity_kr",
            "symbol": row["symbol"],
            "side": "buy",
            "account_mode": "kis_live",
        }
        for _ in decisions
    ]
    rung_rows = [
        {
            "limit_price": Decimal(PRICE),
            "planned_quantity": Decimal(1 if rung == "valid" else 2),
            "planned_notional": None,
            "side": "buy",
        }
        for rung in rungs
    ]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Values(forecast_rows),
                Values(decision_rows),
                Values(rung_rows),
            ]
        )
    )
    assert await store._link_state(db, row) == (
        expected,
        decision_id if expected == "confirmed" else None,
    )
    assert all(
        str(call.args[0]).lstrip().startswith("SELECT")
        for call in db.execute.call_args_list
    )
