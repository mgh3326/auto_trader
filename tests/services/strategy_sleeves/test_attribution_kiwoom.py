"""hk 5529 T1m-a: Kiwoom mock fill attribution (pure + loader statements)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models.trading import InstrumentType
from app.services.strategy_sleeves import store, vocabulary
from tests.services.strategy_sleeves.support import D, order, sleeve

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # 10:00 KST, trading day 20261102
DAY0 = "20261102"
ACCOUNT = "kiwoom_mock"


def _row(ledger_id: int, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": ledger_id,
        "broker": "kiwoom",
        "account_mode": "mock",
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": "0000123",
        "filled_qty": D("3"),
        "filled_price": D("70000"),
        "fee_amount": None,
        "fee_currency": None,
        "filled_at": T0 + timedelta(minutes=5),
        "currency": "KRW",
    }
    return SimpleNamespace(**{**values, **overrides})


def _sleeve():
    return sleeve(
        rule_id="rob1458.kr_cut_minus20",
        account_mode=ACCOUNT,
        market="equity_kr",
        currency="KRW",
    )


def _ref(rung_id: int, *, order_day: Any = DAY0, created: datetime = T0, **kw: Any):
    return store._RungRef(order(rung_id, symbol="005930", **kw), created, order_day)


def _send(
    order_no: str,
    trading_day: str | None,
    *,
    idem: str | None = "idem-10",
    account_mode: str = ACCOUNT,
    operation_kind: str = "place",
) -> SimpleNamespace:
    return SimpleNamespace(
        broker_order_id=order_no,
        idempotency_key=idem,
        trading_day=trading_day,
        account_mode=account_mode,
        operation_kind=operation_kind,
    )


def _days(rows: list[SimpleNamespace], order_id: str = "123") -> dict[int, str]:
    return store.kiwoom_rung_order_days(
        rungs=[store._SendKeys(10, order_id, "idem-10", None)],
        send_rows=rows,
        account_mode=ACCOUNT,
    )


# --- the rung's own order day: account + idempotency key + number --------------


def test_the_order_day_comes_from_the_rungs_own_place_row() -> None:
    assert _days([_send("0000123", DAY0)]) == {10: DAY0}


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_send("123", DAY0, idem="idem-99")],  # number matches, key does not
        [_send("999", DAY0)],  # key matches, number does not
        [_send("123", DAY0, account_mode="kiwoom_mock_us")],  # another account
        [_send("123", DAY0, operation_kind="cancel")],  # not the place row
        [_send("123", None)],  # no day recorded
        [_send("123", "20261102"), _send("123", "20261103")],  # two days
    ],
)
def test_no_unambiguous_own_row_means_no_order_day(rows) -> None:
    assert _days(rows) == {}


def test_a_rung_without_an_idempotency_key_never_gets_a_day() -> None:
    days = store.kiwoom_rung_order_days(
        rungs=[store._SendKeys(10, "123", None, "corr-10")],
        send_rows=[_send("123", DAY0, idem=None)],
        account_mode=ACCOUNT,
    )
    assert days == {}


# --- fills: account, day, number, key ------------------------------------------


def test_a_fill_on_the_rungs_order_day_is_the_rungs_fill() -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [_ref(10)]}, sleeve=_sleeve()
    )
    assert [(f.ledger_id, f.rung_id, f.quantity) for f in fills] == [(1, 10, D("3"))]
    assert not skipped and not unattributed


def test_the_same_number_on_another_trading_day_is_not_the_rungs_fill() -> None:
    later = _row(1, filled_at=T0 + timedelta(days=1))
    fills, skipped, unattributed = store.attribute_fills(
        rows=[later], by_order_id={"123": [_ref(10)]}, sleeve=_sleeve()
    )
    assert fills == []
    assert skipped == {"order_id_reused_on_another_day": 1}
    assert not unattributed


def test_the_trading_day_is_the_kst_day_of_the_observation_instant() -> None:
    # 23:50 UTC is 08:50 KST the next calendar day: day 20261103, not 20261102.
    late = _row(1, filled_at=datetime(2026, 11, 2, 23, 50, tzinfo=UTC))
    fills, skipped, _ = store.attribute_fills(
        rows=[late],
        by_order_id={"123": [_ref(10, created=T0 - timedelta(hours=1))]},
        sleeve=_sleeve(),
    )
    assert fills == []
    assert skipped == {"order_id_reused_on_another_day": 1}


def test_a_rung_without_a_known_order_day_is_unmeasured_not_zero() -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)],
        by_order_id={"123": [_ref(10, order_day=None)]},
        sleeve=_sleeve(),
    )
    assert fills == []
    assert skipped == {"kiwoom_order_day_unknown": 1}
    assert unattributed == {10}


def test_two_rungs_on_one_account_day_number_are_ambiguous_and_never_guessed() -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)],
        by_order_id={"123": [_ref(10), _ref(11, created=T0 + timedelta(minutes=1))]},
        sleeve=_sleeve(),
    )
    assert fills == []
    assert skipped == {"kiwoom_order_match_ambiguous": 1}
    assert unattributed == {10, 11}


def test_the_rung_whose_day_matches_owns_a_shared_number() -> None:
    old = _ref(10, order_day="20261030", created=T0 - timedelta(days=3))
    new = _ref(11)
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [old, new]}, sleeve=_sleeve()
    )
    assert [f.rung_id for f in fills] == [11]
    assert not skipped and not unattributed


def test_an_unknown_day_candidate_blocks_a_day_proven_one() -> None:
    proven = _ref(10)
    unknown = _ref(11, order_day=None, created=T0 - timedelta(days=1))
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [proven, unknown]}, sleeve=_sleeve()
    )
    assert fills == []
    assert skipped == {"kiwoom_order_day_unknown": 1}
    assert unattributed == {11}


def test_a_fill_of_another_side_or_symbol_never_attaches() -> None:
    fills, skipped, _ = store.attribute_fills(
        rows=[_row(1, side="sell"), _row(2, symbol="000660", raw_symbol="000660")],
        by_order_id={"123": [_ref(10)]},
        sleeve=_sleeve(),
    )
    assert fills == []
    assert skipped == {"no_matching_order": 2}


# --- loader: ledger filters and the sleeve's own account ------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, *results: list[Any]) -> None:
        self._results = list(results)
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(
            str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return _Result(self._results.pop(0))


@pytest.mark.asyncio
async def test_the_loader_reads_the_own_account_place_rows_and_the_in_effect_ledger() -> (
    None
):
    proposal = SimpleNamespace(
        id=1, symbol="005930", root_proposal_id="root-1", created_at=T0
    )
    rung = SimpleNamespace(
        id=10,
        side="buy",
        state="resting",
        quantity=D("3"),
        limit_price=D("70000"),
        notional=None,
        filled_qty=None,
        broker_order_id="0000123",
        idempotency_key="idem-10",
        correlation_id="corr-10",
    )
    session = _Session(
        [(proposal, rung)],
        [_send("123", DAY0)],
        [_row(1)],
    )

    inputs = await store.load_sleeve_inputs(session, _sleeve())  # type: ignore[arg-type]

    assert [(f.ledger_id, f.rung_id) for f in inputs.fills] == [(1, 10)]
    assert inputs.unattributed_rungs == ()
    send_sql, ledger_sql = session.statements[1], session.statements[2]
    assert "kiwoom_mock_order_ledger" in send_sql
    assert "account_mode = 'kiwoom_mock'" in send_sql
    assert "operation_kind = 'place'" in send_sql
    assert "idempotency_key IN ('idem-10')" in send_sql
    assert "quarantined_at IS NULL" in ledger_sql
    assert "broker = 'kiwoom'" in ledger_sql
    assert "account_mode = 'mock'" in ledger_sql


def test_the_kiwoom_mock_combination_is_in_the_vocabulary() -> None:
    """T1m-b owns the account combination; the mapping is the real one."""
    assert vocabulary.LEDGER_IDENTITY[ACCOUNT] == ("kiwoom", "mock")
    assert store.LEDGER_IDENTITY[ACCOUNT] == ("kiwoom", "mock")
    assert (ACCOUNT, "equity_kr") in vocabulary.SLEEVE_ACCOUNT_MARKETS
    assert (ACCOUNT, "equity_kr") in vocabulary.ACCOUNT_MARKETS


# --- the sleeve's cash used and P&L are measured from the Kiwoom attribution ----


def _proposal_and_rung(
    *,
    rung_id: int,
    order_no: str,
    idem: str,
    side: str,
    qty: str,
    limit: str,
    state: str,
):
    proposal = SimpleNamespace(
        id=rung_id,
        symbol="005930",
        root_proposal_id=f"root-{rung_id}",
        created_at=T0,
    )
    rung = SimpleNamespace(
        id=rung_id,
        side=side,
        state=state,
        quantity=D(qty),
        limit_price=D(limit),
        notional=None,
        filled_qty=None,
        broker_order_id=order_no,
        idempotency_key=idem,
        correlation_id=f"corr-{rung_id}",
    )
    return proposal, rung


@pytest.mark.asyncio
async def test_a_kiwoom_mock_sleeves_used_and_pnl_come_from_the_attributed_fills() -> (
    None
):
    """Buy 10 (filled 6 so far), sell 4 of those; one fill is another account's."""
    from app.services.strategy_sleeves.book import compute_sleeve_book

    buy = _proposal_and_rung(
        rung_id=10,
        order_no="0000123",
        idem="idem-10",
        side="buy",
        qty="10",
        limit="70000",
        state="partially_filled",
    )
    sell = _proposal_and_rung(
        rung_id=11,
        order_no="0000456",
        idem="idem-11",
        side="sell",
        qty="4",
        limit="72000",
        state="filled",
    )
    session = _Session(
        [buy, sell],
        [_send("123", DAY0, idem="idem-10"), _send("456", DAY0, idem="idem-11")],
        [
            _row(1, filled_qty=D("4"), filled_price=D("70000"), id=1),
            _row(2, filled_qty=D("2"), filled_price=D("70500"), id=2),
            _row(
                3,
                side="sell",
                broker_order_id="0000456",
                filled_qty=D("4"),
                filled_price=D("72000"),
                filled_at=T0 + timedelta(minutes=30),
                id=3,
            ),
            # Same number on another day: never this rung's fill.
            _row(4, filled_at=T0 + timedelta(days=1), filled_qty=D("9"), id=4),
        ],
    )

    inputs = await store.load_sleeve_inputs(session, _sleeve())  # type: ignore[arg-type]

    assert sorted((f.ledger_id, f.rung_id) for f in inputs.fills) == [
        (1, 10),
        (2, 10),
        (3, 11),
    ]
    assert inputs.diagnostics == {"order_id_reused_on_another_day": 1}
    assert inputs.unattributed_rungs == ()

    book = compute_sleeve_book(
        orders=inputs.orders,
        fills=inputs.fills,
        marks={"005930": D("71000")},
        unattributed_rungs=inputs.unattributed_rungs,
    )
    # FIFO: 4 @ 70000 sold at 72000 -> +8000; 2 @ 70500 held, marked at 71000.
    assert book.realized_pnl == D("8000")
    assert book.unrealized_pnl == D("1000")
    assert book.net_pnl == D("9000")
    assert book.held_cost_basis == D("141000")
    # The partially filled buy still has 10 - 6 = 4 resting at its limit.
    assert book.open_order_notional == D("280000")
    assert book.used == D("421000")


@pytest.mark.asyncio
async def test_an_unattributable_kiwoom_fill_leaves_the_sleeve_unmeasured() -> None:
    from app.services.strategy_sleeves.book import compute_sleeve_book

    buy = _proposal_and_rung(
        rung_id=10,
        order_no="0000123",
        idem="idem-10",
        side="buy",
        qty="10",
        limit="70000",
        state="filled",
    )
    # No place row on this account carries the rung's key: its day is unknown.
    session = _Session([buy], [], [_row(1)])

    inputs = await store.load_sleeve_inputs(session, _sleeve())  # type: ignore[arg-type]

    assert inputs.fills == ()
    assert inputs.unattributed_rungs == (10,)
    book = compute_sleeve_book(
        orders=inputs.orders,
        fills=inputs.fills,
        marks={},
        unattributed_rungs=inputs.unattributed_rungs,
    )
    assert book.used is None
    assert book.unattributed_rungs == (10,)
