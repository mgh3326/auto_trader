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
async def test_missing_input_publishes_watch_with_exact_price(monkeypatch):
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
    result = await store.write_version(
        fake_db(current=0),
        day=DAY,
        request=request(),
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
