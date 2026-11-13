"""DB-free tests for the seven independent inference gates."""

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.kis_us_inference_expiry import (
    CONDITIONS,
    Facts,
    classify,
    deadline_for,
)

NOW = datetime(2026, 11, 14, tzinfo=UTC)
ACCEPT = datetime(2026, 6, 10, 15, tzinfo=UTC)
BASE = Facts(
    ledger_id=1,
    broker="kis",
    account_scope="kis_live",
    market="us",
    status="accepted",
    lifecycle_state="accepted",
    order_kind="limit",
    accepted_at=ACCEPT,
    filled_qty=None,
    identity_valid=True,
    order_fills=0,
    symbol_fills=0,
    covering_run=True,
)


def test_eligible_and_seven_facts_reported():
    row = classify(BASE, NOW)
    assert row["eligible"]
    assert set(row["conditions"]) == set(CONDITIONS)
    assert all(row["conditions"].values())
    assert "not a broker expiry contract" in row["deadline_reason"]
    assert "ORD_DVSN 00/01" in row["day_reason"]


@pytest.mark.parametrize(
    ("changes", "now", "failure"),
    [
        ({"status": "rejected"}, NOW, "open_owned"),
        ({"order_kind": "other"}, NOW, "day_order"),
        ({}, deadline_for(ACCEPT), "deadline_passed"),
        ({}, deadline_for(ACCEPT) + timedelta(days=89), "inquiry_window_passed"),
        ({"order_fills": 1}, NOW, "no_order_fills"),
        ({"symbol_fills": 1}, NOW, "no_symbol_fills"),
        ({"covering_run": False}, NOW, "reconcile_coverage"),
    ],
)
def test_each_failed_condition_blocks(changes, now, failure):
    result = classify(replace(BASE, **changes), now)
    assert result["conditions"][failure] is False
    assert not result["eligible"]
    if failure != "deadline_passed":
        assert [k for k, v in result["conditions"].items() if not v] == [failure]


def test_unknown_facts_fail_closed():
    for change, key in [
        ({"accepted_at": None}, "deadline_passed"),
        ({"order_kind": None}, "day_order"),
        ({"order_fills": None}, "no_order_fills"),
        ({"symbol_fills": None}, "no_symbol_fills"),
        ({"covering_run": None}, "reconcile_coverage"),
    ]:
        assert not classify(replace(BASE, **change), NOW)["conditions"][key]


def test_deadline_dst():
    assert deadline_for(ACCEPT) == datetime(2026, 6, 11, 0, tzinfo=UTC)
    assert deadline_for(datetime(2026, 1, 10, 15, tzinfo=UTC)) == datetime(
        2026, 1, 11, 1, tzinfo=UTC
    )


def test_send_path_has_only_day_order_codes():
    tree = ast.parse(Path("app/services/brokers/kis/overseas_orders.py").read_text())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "OverseasOrderClient"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "order_overseas_stock"
    )
    assignments = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "ord_dvsn" for t in n.targets)
    ]
    assert len(assignments) == 1
    assert ast.dump(assignments[0].value) == ast.dump(
        ast.parse('"01" if price == 0 else "00"', mode="eval").body
    )
    body_assignments = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "ORD_DVSN" for k in n.keys)
    ]
    assert len(body_assignments) == 1
    body = body_assignments[0]
    assert any(
        isinstance(k, ast.Constant)
        and k.value == "ORD_DVSN"
        and isinstance(v, ast.Name)
        and v.id == "ord_dvsn"
        for k, v in zip(body.keys, body.values, strict=True)
    )


@pytest.mark.asyncio
async def test_preview_only_reads(monkeypatch):
    from types import SimpleNamespace

    from app.services import kis_us_inference_expiry_service as service

    row = SimpleNamespace(id=1)

    async def candidates(session, *, lock=False):
        assert not lock
        return [row]

    async def decide(session, candidate, now):
        assert candidate is row
        return {"ledger_id": 1, "eligible": False}

    class ReadOnly:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected DB operation: {name}")

    monkeypatch.setattr(service, "_candidates", candidates)
    monkeypatch.setattr(service, "_decide", decide)
    assert await service.preview(ReadOnly(), now=NOW) == [
        {"ledger_id": 1, "eligible": False}
    ]


@pytest.mark.asyncio
async def test_invalid_commit_never_opens_transaction():
    from app.services import kis_us_inference_expiry_service as service

    class Trap:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected operation: {name}")

    with pytest.raises(ValueError):
        await service.commit(
            Trap(), ids=[1], decision_ref="wrong", actor="a", reason="r"
        )


def test_unknown_acceptance_fails_both_deadline_conditions():
    # d implies c: a passed deadline+90d necessarily passes the deadline itself.
    conditions = classify(replace(BASE, accepted_at=None), NOW)["conditions"]
    assert conditions["deadline_passed"] is False
    assert conditions["inquiry_window_passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "price", "expected"),
    [("buy", 0, "01"), ("buy", 100, "00"), ("sell", 0, "01"), ("sell", 100, "00")],
)
async def test_us_send_transport_receives_day_order_code(side, price, expected):
    from unittest.mock import AsyncMock, MagicMock

    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    parent = MagicMock()
    parent._hdr_base = {}
    parent._ensure_token = AsyncMock()
    parent._kis_url = lambda path: f"https://example.invalid{path}"
    parent._settings.kis_account_no = "1234567890"
    parent._settings.kis_access_token = "test-token"
    parent._request_with_rate_limit = AsyncMock(
        return_value={"rt_cd": "0", "output": {"ODNO": "1", "ORD_TMD": "090000"}}
    )
    await OverseasOrderClient(parent).order_overseas_stock(
        "AAPL", "NASD", side, 1, price
    )
    assert (
        parent._request_with_rate_limit.await_args.kwargs["json_body"]["ORD_DVSN"]
        == expected
    )


def test_unrepresentable_inquiry_window_fails_closed():
    result = classify(
        replace(BASE, accepted_at=datetime(9999, 12, 1, tzinfo=UTC)),
        datetime(9999, 12, 2, tzinfo=UTC),
    )
    assert not result["conditions"]["inquiry_window_passed"]
    assert not result["eligible"]


@pytest.mark.asyncio
async def test_commit_timeout_rolls_back_and_sets_database_timeouts(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from app.services import kis_us_inference_expiry_service as service

    class SlowSession:
        def __init__(self):
            self.statements = []
            self.rolled_back = False

        @asynccontextmanager
        async def begin(self):
            try:
                yield
            except BaseException:
                self.rolled_back = True
                raise

        async def execute(self, statement):
            self.statements.append(str(statement))
            return None

    async def slow_candidates(session, *, lock=False):
        assert lock
        await asyncio.Event().wait()

    session = SlowSession()
    monkeypatch.setattr(service, "_candidates", slow_candidates)
    monkeypatch.setattr(service, "COMMIT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        await service.commit(
            session,
            ids=[1],
            decision_ref=service.DECISION_REF,
            actor="operator",
            reason="test",
        )
    assert session.rolled_back
    assert session.statements == [
        "SET LOCAL lock_timeout = '2s'",
        "SET LOCAL statement_timeout = '5s'",
    ]


@pytest.mark.asyncio
async def test_fill_lookup_counts_identity_and_excludes_raw_payload():
    from types import SimpleNamespace

    from app.services import kis_us_inference_expiry_service as service

    fill = SimpleNamespace(
        broker="kis",
        account_mode="live",
        instrument_type="equity_us",
        symbol="AAPL",
        side="buy",
        currency="USD",
        broker_order_id="000123",
        filled_at=ACCEPT,
    )
    row = SimpleNamespace(
        id=1,
        broker="kis",
        account_scope="kis_live",
        market="us",
        status="accepted",
        lifecycle_state="accepted",
        order_kind="limit",
        trade_date=ACCEPT,
        order_time="000000",
        filled_qty=None,
        order_no="123",
        symbol="AAPL",
        side="buy",
        currency="USD",
    )

    # 15:00 UTC is 00:00 KST on the next calendar day.
    class Session:
        async def execute(self, statement):
            assert "raw_payload_json" not in str(statement)
            return SimpleNamespace(all=lambda: [fill])

        async def scalar(self, statement):
            return None

    result = await service._decide(Session(), row, NOW)
    assert result["order_fills"] == result["symbol_fills"] == 1
    assert not result["conditions"]["no_order_fills"]
    assert not result["conditions"]["no_symbol_fills"]


@pytest.mark.asyncio
async def test_final_recheck_refuses_entire_batch_when_fill_appears(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services import kis_us_inference_expiry_service as service

    rows = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    calls = []

    class FakeSession:
        rolled_back = False

        @asynccontextmanager
        async def begin(self):
            try:
                yield
            except BaseException:
                self.rolled_back = True
                raise

        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        def add(self, obj):
            raise AssertionError("a refused batch must not write")

    async def candidates(session, *, lock=False):
        assert lock
        return rows

    async def decide(session, row, now):
        calls.append(row.id)
        return {"ledger_id": row.id, "eligible": not (len(calls) == 4)}

    monkeypatch.setattr(service, "_candidates", candidates)
    monkeypatch.setattr(service, "_decide", decide)
    session = FakeSession()
    with pytest.raises(ValueError, match="final recheck"):
        await service.commit(
            session,
            ids=[1, 2],
            decision_ref=service.DECISION_REF,
            actor="a",
            reason="r",
        )
    assert calls == [1, 2, 1, 2]
    assert session.rolled_back


@pytest.mark.asyncio
async def test_check_conflicts_reports_later_fills_without_writes(monkeypatch):
    from types import SimpleNamespace

    from app.services import kis_us_inference_expiry_service as service

    rows = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]

    class ReadOnlySession:
        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        def __getattr__(self, name):
            raise AssertionError(f"unexpected write: {name}")

    async def decide(session, row, now):
        return {"order_fills": {1: 1, 2: 0, 3: None}[row.id], "symbol_fills": 0}

    monkeypatch.setattr(service, "_decide", decide)
    results = await service.check_conflicts(ReadOnlySession(), now=NOW)
    assert [result["state"] for result in results] == ["conflict", "clear", "unknown"]
