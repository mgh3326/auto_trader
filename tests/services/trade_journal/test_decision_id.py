from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.mcp_server.tooling import trade_retrospective_tools as tools
from app.models.review import TradeRetrospective
from app.services.trade_journal import trade_retrospective_service as service


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def scalar_one_or_none(self):
        return None


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, decision):
        self.decision = decision
        self.queries = []
        self.added = None

    async def execute(self, statement, params=None):
        self.queries.append((str(statement), params))
        return _Result(self.decision)

    def add(self, row):
        self.added = row

    async def flush(self):
        pass

    def begin_nested(self):
        return _NestedTransaction()


@pytest.mark.asyncio
async def test_repository_stores_matching_caller_decision_id():
    decision_id = uuid4()
    db = _Session(
        {"symbol": "005930", "market": "equity_kr", "account_mode": "kis_live"}
    )

    action, row = await service.TradeRetrospectiveRepository(db).upsert(
        {
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "market": "kr",
            "account_mode": "kis_live",
            "outcome": "filled",
            "decision_id": decision_id,
        }
    )

    assert action == "created"
    assert row.decision_id == decision_id
    assert len(db.queries) == 1
    assert "FROM review.decision_evidence WHERE decision_id" in db.queries[0][0]


@pytest.mark.asyncio
async def test_repository_accepts_upbit_market_alias_for_decision_identity():
    decision_id = uuid4()
    db = _Session(
        {"symbol": "BTC", "market": "crypto", "account_mode": "upbit"}
    )

    _, row = await service.TradeRetrospectiveRepository(db).upsert(
        {
            "symbol": "BTC",
            "instrument_type": "crypto",
            "market": "upbit",
            "account_mode": "upbit_live",
            "outcome": "filled",
            "decision_id": decision_id,
        }
    )

    assert row.decision_id == decision_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retrospective", "decision", "_field"),
    [
        (
            {"symbol": "AAPL", "market": "us", "account_mode": "toss_live"},
            {"symbol": "MSFT", "market": "equity_us", "account_mode": "toss_live"},
            "symbol",
        ),
        (
            {"symbol": "AAPL", "market": "kr", "account_mode": "toss_live"},
            {"symbol": "AAPL", "market": "equity_us", "account_mode": "toss_live"},
            "market",
        ),
        (
            {"symbol": "AAPL", "market": "us", "account_mode": "kis_live"},
            {"symbol": "AAPL", "market": "equity_us", "account_mode": "toss_live"},
            "account_mode",
        ),
    ],
)
async def test_repository_rejects_mismatched_decision_identity(
    retrospective, decision, _field
):
    db = _Session(decision)
    payload = {
        **retrospective,
        "instrument_type": "equity_us",
        "outcome": "filled",
        "decision_id": uuid4(),
    }

    with pytest.raises(
        service.RetrospectiveValidationError,
        match="does not match retrospective",
    ):
        await service.TradeRetrospectiveRepository(db).upsert(payload)

    assert db.added is None


@pytest.mark.asyncio
async def test_repository_rejects_unknown_decision_id():
    db = _Session(None)
    with pytest.raises(service.RetrospectiveValidationError, match="does not reference"):
        await service.TradeRetrospectiveRepository(db).upsert(
            {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "market": "kr",
                "account_mode": "kis_live",
                "outcome": "filled",
                "decision_id": uuid4(),
            }
        )
    assert db.added is None


@pytest.mark.asyncio
async def test_repository_does_not_infer_a_decision_when_id_is_omitted():
    db = _Session({"symbol": "005930", "market": "equity_kr", "account_mode": "kis_live"})
    _, row = await service.TradeRetrospectiveRepository(db).upsert(
        {
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "market": "kr",
            "account_mode": "kis_live",
            "outcome": "filled",
            "correlation_id": "any-correlation",
        }
    )

    assert row.decision_id is None
    assert all("review.decision_evidence" not in query for query, _ in db.queries)


@pytest.mark.asyncio
async def test_save_tool_forwards_caller_decision_id(monkeypatch):
    decision_id = uuid4()
    captured = {}

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def refresh(self, _row):
            pass

        async def commit(self):
            pass

    class FakeActionRepository:
        def __init__(self, _db):
            pass

        async def read_actions(self, _row_id):
            return []

    async def fake_save(_db, **kwargs):
        captured.update(kwargs)
        return "created", SimpleNamespace(id=1)

    monkeypatch.setattr(tools, "_session_factory", lambda: lambda: FakeDb())
    monkeypatch.setattr(tools, "_env", lambda _name: "default")
    monkeypatch.setattr(tools, "save_retrospective", fake_save)
    monkeypatch.setattr(tools, "RetrospectiveActionRepository", FakeActionRepository)
    monkeypatch.setattr(tools, "serialize_retrospective", lambda *_args, **_kwargs: {})

    result = await tools.save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        decision_id=decision_id,
    )

    assert result["success"] is True
    assert captured["decision_id"] == decision_id


def test_model_and_migration_add_nullable_indexed_column_without_backfill():
    column = TradeRetrospective.__table__.columns["decision_id"]
    assert column.nullable is True
    assert any(
        index.name == "ix_trade_retrospectives_decision_id"
        for index in TradeRetrospective.__table__.indexes
    )

    migration = Path(
        "alembic/versions/20261112_hk4187_retro_decision_id.py"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN decision_id uuid NULL" in migration
    assert "GRANT SELECT (decision_id), INSERT (decision_id), UPDATE (decision_id)" in migration
    assert "WHERE decision_id IS NOT NULL" in migration
    assert "cannot downgrade: trade retrospective decision links exist" in migration
    assert "UPDATE review.trade_retrospectives" not in migration
