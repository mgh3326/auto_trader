from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.core import db
from app.services.kis_futures_mock import reconcile
from scripts import kis_futures_mock_order as cli

DAY = "20261126"


def order(ledger_id, side, effect):
    return SimpleNamespace(
        id=ledger_id,
        broker_order_id=f"{ledger_id:010d}",
        broker_symbol="A05612",
        side=side,
        position_effect=effect,
        quantity=Decimal(1),
        status="accepted",
        filled_qty=None,
        avg_fill_price=None,
        fee=None,
        realized_pnl_krw=None,
        reconciled_at=None,
    )


def fill(ledger_id, side_code, time, price):
    # KIS official inquire_ccnl output1 row shape.
    return {
        "ord_dt": DAY,
        "odno": f"{ledger_id:010d}",
        "orgn_odno": "0000000000",
        "sll_buy_dvsn_cd": side_code,
        "trad_dvsn_name": "매매",
        "pdno": "A05612",
        "ord_qty": "1",
        "qty": "0",
        "ord_tmd": time,
        "tot_ccld_qty": "1",
        "avg_idx": price,
        "rjct_qty": "0",
    }


class FakeSession:
    def __init__(self, orders):
        self.orders = orders
        self.commits = 0
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def scalars(self, query):
        compiled = query.compile(dialect=postgresql.dialect())
        assert set(compiled.params.values()) == {"kis_futures_mock", DAY, "place"}
        self.queries.append(str(compiled))
        return SimpleNamespace(all=lambda: self.orders)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def run_reconcile(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_MOCK_ENABLED", "true")
    monkeypatch.setattr(db, "engine", SimpleNamespace(dispose=_noop), raising=False)

    def run(orders, history, *flags):
        session = FakeSession(orders)

        class Client:
            async def daily_history(self, *, account, trading_day):
                assert (account, trading_day) == ("12345678", DAY)
                return history

            async def close(self):
                pass

        monkeypatch.setattr(
            reconcile,
            "default_deps",
            lambda: SimpleNamespace(
                new_order_client=Client, account_no=lambda: "12345678"
            ),
        )
        monkeypatch.setattr(db, "AsyncSessionLocal", lambda: session)
        assert cli.main(["reconcile", "--day", "2026-11-26", *flags]) == 0
        return session

    return run


async def _noop():
    pass


@pytest.mark.parametrize(
    ("opening_side", "opening_code", "closing_side", "closing_code", "price"),
    [("buy", "02", "sell", "01", "728.00"), ("sell", "01", "buy", "02", "730.56")],
)
def test_preview_commit_and_rerun(
    run_reconcile, capsys, opening_side, opening_code, closing_side, closing_code, price
):
    opening, closing = order(1, opening_side, "open"), order(2, closing_side, "close")
    orders = [opening, closing]
    history = [
        fill(2, closing_code, "150800", price) | {"fee": "12.5"},
        fill(1, opening_code, "150700", "729.28"),
    ]
    preview = run_reconcile(orders, history)
    assert preview.commits == 0 and "FOR UPDATE" not in preview.queries[0]
    assert "would_book" in capsys.readouterr().out
    assert [row.status for row in orders] == ["accepted", "accepted"]
    committed = run_reconcile(orders, history, "--commit")
    assert committed.commits == 1 and "FOR UPDATE" in committed.queries[0]
    assert [row.status for row in orders] == ["filled", "filled"]
    assert [row.filled_qty for row in orders] == [1, 1]
    assert [row.avg_fill_price for row in orders] == [Decimal("729.28"), Decimal(price)]
    assert opening.realized_pnl_krw == 0
    assert closing.realized_pnl_krw == Decimal(-64000)
    assert opening.fee is None and closing.fee == Decimal("12.5")
    assert opening.reconciled_at is not None
    before = [vars(row).copy() for row in orders]
    assert run_reconcile(orders, history, "--commit").commits == 0
    assert [vars(row) for row in orders] == before


@pytest.mark.parametrize(
    "history",
    [
        [],
        [fill(99, "02", "150700", "729.28")],
        [fill(1, "02", "150700", "729.28") | {"ord_dt": "20261125"}],
        [fill(1, "02", "150700", "729.28")] * 2,
        [fill(1, "02", "230700", "729.28")],
        [fill(1, "02", "150700", "729.28") | {"qty": "1"}],
    ],
)
def test_missing_or_ambiguous_evidence_is_noop(run_reconcile, history):
    opening = order(1, "buy", "open")
    before = vars(opening).copy()
    assert run_reconcile([opening], history, "--commit").commits == 0
    assert vars(opening) == before


def test_unmatched_fill_blocks_close_basis(run_reconcile, capsys):
    opening, closing = order(1, "buy", "open"), order(2, "sell", "close")
    history = [
        fill(1, "02", "150700", "729.28"),
        fill(2, "01", "150800", "728"),
        fill(9, "01", "150730", "729"),
    ]
    run_reconcile([opening, closing], history, "--commit")
    assert opening.status == "filled"
    assert closing.status == "accepted"
    assert closing.realized_pnl_krw is None
    assert "entry_fill_unverified" in capsys.readouterr().out


def test_external_close_keeps_open_unsettled_until_attributed(run_reconcile):
    opening = order(1, "buy", "open")
    opening_fill = fill(1, "02", "150700", "729.28")
    closing_fill = fill(2, "01", "150800", "700")
    assert run_reconcile([opening], [opening_fill], "--commit").commits == 1
    assert opening.status == "filled"
    assert opening.realized_pnl_krw is None
    history = [opening_fill, closing_fill]
    assert run_reconcile([opening], history, "--commit").commits == 0
    assert opening.realized_pnl_krw is None

    closing = order(2, "sell", "close")
    assert run_reconcile([opening, closing], history, "--commit").commits == 1
    assert opening.realized_pnl_krw == 0
    assert closing.realized_pnl_krw == Decimal(-1464000)
    assert run_reconcile([opening, closing], history, "--commit").commits == 0


def test_multiple_filled_closes_leave_loss_unsettled(run_reconcile):
    orders = [
        order(1, "buy", "open"),
        order(2, "sell", "close"),
        order(3, "sell", "close"),
    ]
    history = [
        fill(1, "02", "150700", "729.28"),
        fill(2, "01", "150800", "730"),
        fill(3, "01", "150900", "729"),
    ]
    run_reconcile(orders, history, "--commit")
    assert all(row.realized_pnl_krw is None for row in orders)


def test_proven_zero_fill_cancel_does_not_block_round_trip(run_reconcile):
    cancelled = order(1, "buy", "open")
    opening, closing = order(3, "buy", "open"), order(4, "sell", "close")
    original = fill(1, "02", "150500", "0") | {
        "trad_dvsn_name": "취소완료",
        "tot_ccld_qty": "0",
    }
    cancel = original | {
        "odno": "0000000002",
        "orgn_odno": cancelled.broker_order_id,
    }
    history = [
        original,
        cancel,
        fill(3, "02", "150700", "729.28"),
        fill(4, "01", "150800", "728"),
    ]
    run_reconcile([cancelled, opening, closing], history, "--commit")
    assert cancelled.status == "cancelled"
    assert opening.realized_pnl_krw == 0
    assert closing.realized_pnl_krw == Decimal(-64000)


@pytest.mark.parametrize(
    ("label", "status"),
    [("취소완료", "cancelled"), ("만료", "expired"), ("매매", "accepted")],
)
def test_terminal_no_fill_requires_explicit_evidence(run_reconcile, label, status):
    opening = order(1, "buy", "open")
    history = [
        fill(1, "02", "150700", "0") | {"trad_dvsn_name": label, "tot_ccld_qty": "0"}
    ]
    run_reconcile([opening], history, "--commit")
    assert opening.status == status
    assert opening.realized_pnl_krw is None
    assert opening.filled_qty == (None if status == "accepted" else 0)


def test_uncertain_order_is_preserved(run_reconcile):
    opening = order(1, "buy", "open")
    opening.status = "uncertain"
    history = [fill(1, "02", "150700", "729.28")]
    assert run_reconcile([opening], history, "--commit").commits == 0
    assert opening.status == "uncertain"
