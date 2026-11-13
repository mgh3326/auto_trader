"""Shared fixtures for the retro/v1 reader tests: fake session + contract validator."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
from referencing import Registry, Resource
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.models.execution_ledger import ExecutionLedger
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import TradeForecast, TradeRetrospective
from app.models.screener_pick_log import ScreenerPickLog
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType

_CONTRACT_TEST = (
    Path(__file__).resolve().parents[2] / "contracts" / "test_retro_v1_contract.py"
)
_spec = importlib.util.spec_from_file_location(
    "retro_v1_contract_loader", _CONTRACT_TEST
)
assert _spec and _spec.loader
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)

SPEC = _loader.document()
_REGISTRY = Registry().with_resource(
    "urn:retro",
    Resource.from_contents(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", **SPEC}
    ),
)


def validate(response: dict[str, Any], schema_name: str) -> None:
    """Validate a reader response against the contract's response schema.

    The same checks the contract test applies to its examples: schema validation
    with date-time format checking, plus "every null row value is explained by a
    row reason or by an unknown entry with its dotted path".
    """
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"#/components/schemas/{schema_name}"},
        registry=_REGISTRY,
        _resolver=_REGISTRY.resolver("urn:retro"),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    validator.validate(response)
    unknowns = {item["field"] for item in response["unknowns"]}
    for index, row in enumerate(response["data"]):
        for path in _loader.null_paths(row, f"data.{index}"):
            assert row.get("reason") or row.get("missing_reason") or path in unknowns, (
                path
            )


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0]

    def scalar_one(self) -> Any:
        return self._rows[0]

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


def operational_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("raw driver text must not leak"))


class WithParams:
    """A route that also receives the statement's compiled bind parameters."""

    def __init__(self, fn: Callable[[str, dict[str, Any]], list[Any]]) -> None:
        self.fn = fn


class FakeSession:
    """Routes each SELECT by the table it reads; records every statement.

    ``routes`` maps a ``FROM <table>`` fragment to rows, a callable
    ``(sql) -> rows``, a ``WithParams`` callable ``(sql, params) -> rows`` or an
    exception instance to raise. Unrouted statements (``SET TRANSACTION``,
    ``set_config``) return nothing.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.statements: list[str] = []
        self._in_tx = False
        self.begun = 0
        self.nested = 0

    def in_transaction(self) -> bool:
        return self._in_tx

    @asynccontextmanager
    async def begin(self):
        self._in_tx = True
        self.begun += 1
        try:
            yield
        finally:
            self._in_tx = False

    @asynccontextmanager
    async def begin_nested(self):
        self.nested += 1
        yield

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.statements.append(sql)
        for fragment, value in self.routes.items():
            if fragment in sql:
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, WithParams):
                    rows = value.fn(sql, dict(compiled.params))
                else:
                    rows = value(sql) if callable(value) else value
                if (
                    fragment == "FROM review.execution_ledger"
                    and "quarantined_at IS NULL" in sql
                ):
                    rows = [
                        r for r in rows if getattr(r, "quarantined_at", None) is None
                    ]
                return FakeResult(list(rows))
        return FakeResult([])


UTC_NOW = datetime(2026, 1, 20, 3, 0, tzinfo=UTC)


def pick(id_: int, symbol: str, at: datetime, **kw: Any) -> ScreenerPickLog:
    values: dict[str, Any] = {
        "id": id_,
        "symbol": symbol,
        "recorded_at": at,
        "market": "kr",
        "admission": "admitted",
        "admission_reason": "round_robin",
        "decision_price_text": "70000",
    }
    values.update(kw)
    return ScreenerPickLog(**values)


def forecast(id_: int, symbol: str, at: datetime, **kw: Any) -> TradeForecast:
    values: dict[str, Any] = {
        "id": id_,
        "symbol": symbol,
        "created_at": at,
        "decision_bucket": "deferred_no_action",
    }
    values.update(kw)
    return TradeForecast(**values)


def proposal(id_: int, symbol: str, at: datetime, **kw: Any) -> OrderProposal:
    values: dict[str, Any] = {
        "id": id_,
        "symbol": symbol,
        "created_at": at,
        "lifecycle_state": "filled",
        "void_reason": None,
        "market": "equity_kr",
        "account_mode": "kis_live",
    }
    values.update(kw)
    return OrderProposal(**values)


def rung(id_: int, proposal_pk: int, **kw: Any) -> OrderProposalRung:
    values: dict[str, Any] = {
        "id": id_,
        "proposal_pk": proposal_pk,
        "rung_index": 0,
        "side": "buy",
        "quantity": Decimal("3"),
        "limit_price": Decimal("70000"),
        "state": "filled",
        "broker_order_id": "0000123456",
        "idempotency_key": f"idem-{id_}",
        "created_at": datetime(2026, 1, 5, 1, 5, tzinfo=UTC),
        "validated_at": datetime(2026, 1, 5, 1, 10, tzinfo=UTC),
        "void_reason": None,
        "void_reason_group": None,
    }
    values.update(kw)
    return OrderProposalRung(**values)


def ledger(id_: int, at: datetime, **kw: Any) -> ExecutionLedger:
    values: dict[str, Any] = {
        "id": id_,
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": "0000123456",
        "fill_seq": 0,
        "filled_qty": Decimal("3"),
        "filled_price": Decimal("70000"),
        "filled_notional": Decimal("210000"),
        "filled_at": at,
        "currency": "KRW",
        "source": "reconciler",
        "quarantined_at": None,
    }
    values.update(kw)
    return ExecutionLedger(**values)


def bar_row(symbol: str, at: datetime, close: str, **kw: Any) -> dict[str, Any]:
    row = {
        "time": at,
        "symbol": symbol,
        "venue": "KRX",
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "volume": Decimal("1000"),
        "source": "kis",
    }
    row.update(kw)
    return row


def ns(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


Route = Callable[[str], list[Any]]


FILL_AT = datetime(2026, 1, 5, 2, 0, tzinfo=UTC)  # 11:00 KST on 2026-01-05


def live_order(id_: int = 100, at: datetime = FILL_AT, **kw: Any) -> Any:
    values = {
        "id": id_,
        "trade_date": at,
        "broker": "kis",
        "symbol": "005930",
        "side": "buy",
        "currency": "KRW",
        "order_no": "0000123456",
        "quantity": Decimal("3"),
        "price": Decimal("70000"),
        "status": "accepted",
        "idempotency_key": f"idem-{id_}",
        "correlation_id": None,
        "response_code": None,
        "response_message": None,
    }
    values.update(kw)
    return ns(**values)


def kr_day_session(**extra_routes: Any) -> FakeSession:
    """One KR trading day, 005930: pick, rejected forecast, proposal → rung →
    order → fill, a superseded websocket duplicate and a quarantined phantom."""
    fills = [
        ledger(1000, FILL_AT),
        # websocket duplicate of the reconciled order (ROB-1379): superseded.
        ledger(
            1001,
            FILL_AT.replace(second=1),
            source="websocket",
            fill_seq=7,
            venue="krx",
        ),
        # quarantined accept-notice phantom (#1175): not a fill.
        ledger(
            1002,
            FILL_AT.replace(minute=5),
            source="websocket",
            broker_order_id="0000999999",
            quarantined_at=FILL_AT,
        ),
    ]
    bars = [
        bar_row("005930", datetime(2026, 1, 5, 0, 0, tzinfo=UTC), "71000"),
        *[
            bar_row(
                "005930",
                datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
                str(72000 + i),
            )
            for i in range(25)
        ],
    ]
    routes: dict[str, Any] = {
        "FROM review.screener_pick_log": [
            pick(1, "005930", datetime(2026, 1, 5, 0, 30, tzinfo=UTC))
        ],
        "FROM review.trade_forecasts": [
            forecast(2, "000660", datetime(2026, 1, 5, 0, 40, tzinfo=UTC))
        ],
        "FROM review.order_proposals ": [
            proposal(10, "005930", datetime(2026, 1, 5, 1, 0, tzinfo=UTC))
        ],
        "FROM review.order_proposal_rungs": [rung(100, 10)],
        "FROM review.order_send_intents": [("idem-100", FILL_AT - timedelta(hours=1))],
        "FROM review.execution_ledger": fills,
        "FROM review.kis_live_order_ledger": [live_order()],
        "FROM public.kr_candles_1d": bars,
    }
    routes.update(extra_routes)
    return FakeSession(routes)


GATE_TRADE_EXIT = datetime(
    2026, 1, 5, 6, 0, tzinfo=UTC
)  # inside the gate's 2026-01-01..01-10 window


def gate_trade_journal() -> TradeJournal:
    """One closed live KR trade (synthetic values) for the consumer gate."""
    return TradeJournal(
        id=1,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        status="closed",
        account_type="live",
        strategy="S_ALPHA",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        exit_date=GATE_TRADE_EXIT,
        entry_price=Decimal("70000"),
        exit_price=Decimal("72000"),
        quantity=Decimal("3"),
        pnl_pct=Decimal("2.8571"),
        total_pnl_krw=Decimal("6000"),
    )


def gate_trade_review() -> TradeRetrospective:
    """The one review linked to gate_trade_journal (synthetic values)."""
    return TradeRetrospective(
        id=1,
        journal_id=1,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        account_mode="kis_live",
        outcome="filled",
        side="sell",
        fill_evidence_available=True,
        created_at=GATE_TRADE_EXIT,
        realized_pnl=Decimal("6000"),
        realized_pnl_currency="KRW",
        trigger_type="take_profit",
        root_cause_class="none",
        lesson="synthetic lesson",
        result_summary="synthetic result",
        rationale="synthetic rationale",
        next_strategy="synthetic next",
    )


def build_gate_session() -> FakeSession:
    """Deterministic session providing complete data across all 8 retro/v1 operations."""
    now = datetime.now(UTC)
    stale_run = SimpleNamespace(
        run_id="00000000-0000-0000-0000-000000000001",
        broker="kis",
        window_start=now - timedelta(days=1),
        window_end=now,
        started_at=now - timedelta(hours=5, minutes=1),
        finished_at=now - timedelta(hours=5),
        dry_run=False,
        would_insert=0,
        would_update=0,
        unchanged=0,
        committed_insert=0,
        committed_update=0,
        error_summary=None,
        notes=None,
    )
    experiment = SimpleNamespace(
        strategy_id="S_ALPHA",
        venue="alpaca_paper",
        total=4,
        sent=3,
        filled=2,
        unresolved=1,
        closed_count=2,
        closed_unpriced=0,
        pnl_sum=Decimal("12.50"),
        last_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    account_id = "upbit:upbit:" + "a" * 64
    snapshots = [
        SimpleNamespace(
            account=account_id,
            market="account",
            day=datetime(2026, 1, 1, tzinfo=UTC).date() + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
            observed_at=datetime(2026, 1, 8, 9, 30, tzinfo=UTC),
        )
        for i in range(8)
    ]
    bars = [
        bar_row(
            "005930",
            datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
            "71000",
            partition="KRX",
            adj_close=None,
            value=Decimal("71000000"),
        ),
        *[
            bar_row(
                "005930",
                datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
                str(72000 + i),
                partition="KRX",
                adj_close=None,
                value=Decimal("72000000"),
            )
            for i in range(25)
        ],
    ]

    def account_snapshots_route(sql: str):
        if "max(" in sql:
            return [datetime(2026, 1, 8, 9, 30, tzinfo=UTC)]
        if "DISTINCT" in sql or "distinct" in sql:
            return [(account_id, "account")]
        return snapshots

    def trade_forecast_route(sql: str):
        if "max(" in sql or "min(" in sql:
            return [None]
        return [forecast(2, "000660", datetime(2026, 1, 5, 0, 40, tzinfo=UTC))]

    routes = {
        "SHOW transaction_read_only": ["on"],
        "FROM review.execution_ledger_reconcile_runs": [stale_run],
        "execution_ledger_reconcile_runs": [stale_run],
        "krx_after_market_eligibility": [(0, None, None)],
        "investment_report_items": [None],
        "FROM review.mock_strategy_orders": [experiment],
        "FROM strategy_sleeves": [],
        "max(account_value_snapshots.observed_at)": [
            datetime(2026, 1, 8, 9, 30, tzinfo=UTC)
        ],
        "account_value_snapshots": account_snapshots_route,
        "account_external_flows": [],
        "FROM public.kr_candles_1d": bars,
        "FROM review.trade_journals": [gate_trade_journal()],
        "FROM review.trade_retrospectives": [gate_trade_review()],
    }

    base_session = FakeSession()
    kr_session = kr_day_session()
    full_routes = dict(routes)
    for k, v in kr_session.routes.items():
        if k == "FROM review.execution_ledger":
            full_routes["FROM review.execution_ledger "] = v
        elif k == "FROM review.trade_forecasts":
            full_routes[k] = trade_forecast_route
        elif k == "FROM public.kr_candles_1d":
            pass
        else:
            full_routes[k] = v
    base_session.routes = full_routes
    return base_session
