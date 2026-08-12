"""Mutants ① (legacy sell) and ② (ledger exception), plus the pending source.

The two questions this file answers, both of which have an irreversible wrong
answer:

* **Whose shares are these?** A legacy holding must never become a sell or an
  averaging candidate, and must never enter the §4 cap inputs or NAV.
* **Where did 자기 미체결 come from?** From ``kt00009``, the broker. If that read
  fails the lane blocks every symbol; it does **not** reach for the kis ledger.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.b0x.broker_truth import PendingUnreadable
from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.kr import kiwoom_cycle
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount, position, resting

pytestmark = pytest.mark.unit


def _journal(tmp_path, records=()):  # noqa: ANN001, ANN202 — local test helper
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    for record in records:
        journal.append(record)
    return journal


def _record(**kwargs):  # noqa: ANN003, ANN202
    base = {
        "at": "2026-08-12T03:00:00+00:00",
        "order_no": "0000000001",
        "correlation_id": "b0xkw-deadbeefdeadbeef",
        "symbol": "005930",
        "side": "buy",
        "price": 70_000,
        "quantity": 1,
        "order_date": "20260812",
    }
    base.update(kwargs)
    return kiwoom_attr.OwnOrderRecord(**base)


# ---------------------------------------------------------------------------
# 🔴 mutant ① — legacy must never become sellable inventory.
# ---------------------------------------------------------------------------


def test_legacy_holdings_are_excluded_from_every_derivation_input() -> None:
    """An empty journal on a populated account ⇒ everything is legacy."""

    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("1000000"),
        nav=Decimal("9000000"),
        positions=(
            position("005930", 10, average_price=70_000),
            position("000660", 3, average_price=200_000),
        ),
    )
    state = kiwoom_cycle.broker_state(
        fresh=fresh,
        pending=kiwoom_lane.BrokerPending(account_orders=(), own_order_ids=frozenset()),
        attribution=kr_attribution.OwnFillAttribution(lots=()),
    )

    assert state.positions == (), "legacy holdings leaked into derivation inventory"
    assert state.broker_truth.position_symbols == ()
    # NAV excludes legacy market value: the §4 kill is pct_of_nav, so including
    # it would *widen* the absolute threshold.
    assert state.nav == Decimal("1000000")
    assert state.nav == state.cash


def test_legacy_sell_is_blocked_at_the_submission_boundary() -> None:
    """🔴 mutant ① — the redundant second line still refuses."""

    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("1000000"),
        nav=Decimal("1700000"),
        positions=(position("005930", 10, average_price=70_000),),
    )
    scoped = kiwoom_cycle.scoped_positions(
        fresh=fresh, attribution=kr_attribution.OwnFillAttribution(lots=())
    )
    assert scoped.legacy_symbols == ("005930",)

    with pytest.raises(kr_attribution.LegacyPositionSellBlocked):
        kr_attribution.assert_sell_is_own(
            scoped, symbol="005930", quantity=Decimal("1"), lane=kiwoom_lane.LANE
        )


def test_attributed_quantity_is_capped_by_the_broker_holding() -> None:
    """The journal cannot conjure shares the account does not hold."""

    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("0"),
        nav=Decimal("0"),
        positions=(position("005930", 2, average_price=70_000),),
    )
    over_claiming = kr_attribution.OwnFillAttribution(
        lots=(
            kr_attribution.AttributedLot(
                symbol="005930",
                quantity=Decimal("999"),
                average_price=Decimal("70000"),
                buy_fill_rows=1,
                sell_rows=0,
            ),
        )
    )
    scoped = kiwoom_cycle.scoped_positions(fresh=fresh, attribution=over_claiming)
    assert [pos.quantity for pos in scoped.own_positions] == [Decimal("2")]

    # Selling more than the broker says we hold is still refused.
    with pytest.raises(kr_attribution.LegacyPositionSellBlocked):
        kr_attribution.assert_sell_is_own(
            scoped, symbol="005930", quantity=Decimal("3"), lane=kiwoom_lane.LANE
        )


def test_unreadable_attribution_closes_both_directions() -> None:
    """Unknown ownership ⇒ no own positions AND account-wide caps."""

    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("500000"),
        nav=Decimal("2000000"),
        positions=(position("005930", 10), position("000660", 5)),
    )
    scoped = kiwoom_cycle.scoped_positions(
        fresh=fresh, attribution=kr_attribution.attribution_unreadable("OSError")
    )
    assert scoped.own_positions == ()
    assert scoped.cap_position_symbols == ("000660", "005930")
    assert scoped.cap_basis == "account_wide_fail_closed"
    assert scoped.attributed_evaluation == Decimal("0")


def test_omitting_the_attribution_argument_is_fail_closed() -> None:
    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("1"), nav=Decimal("1"), positions=(position("005930", 10),)
    )
    scoped = kiwoom_cycle.scoped_positions(fresh=fresh, attribution=None)
    assert scoped.own_positions == ()
    assert scoped.unreadable is kiwoom_cycle.ATTRIBUTION_NOT_WIRED


# ---------------------------------------------------------------------------
# 🔴 mutant ② — evidence source is the broker + own journal, never the ledger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribution_reads_broker_fills_not_the_kis_ledger(tmp_path) -> None:  # noqa: ANN001
    """The conftest autouse fixture explodes if the kis reader is touched."""

    journal = _journal(tmp_path, [_record(order_no="0000000042", quantity=3)])
    account = FakeAccount(
        order_detail={
            "20260812": [
                {"order_id": "0000000042", "symbol": "005930", "filled_quantity": 3},
                # Someone else's fill on the same day — not in the journal, so
                # it must not be attributed to this lane.
                {"order_id": "0000000099", "symbol": "000660", "filled_quantity": 7},
            ]
        }
    )

    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.OwnFillAttribution)
    assert attribution.symbols == ("005930",)
    assert attribution.quantity("005930") == Decimal("3")
    assert attribution.quantity("000660") == Decimal("0")


@pytest.mark.asyncio
async def test_buy_without_broker_fill_evidence_is_not_attributed(tmp_path) -> None:  # noqa: ANN001
    """Accepted ≠ filled. An un-filled buy contributes zero (과소 귀속)."""

    journal = _journal(tmp_path, [_record(order_no="0000000042", quantity=3)])
    account = FakeAccount(order_detail={"20260812": []})
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.OwnFillAttribution)
    assert attribution.symbols == ()


@pytest.mark.asyncio
async def test_sell_is_subtracted_regardless_of_fill_state(tmp_path) -> None:  # noqa: ANN001
    """🔴 The asymmetry: an unfilled sell still reduces our attributed amount."""

    journal = _journal(
        tmp_path,
        [
            _record(order_no="0000000042", side="buy", quantity=5),
            _record(order_no="0000000043", side="sell", quantity=5),
        ],
    )
    account = FakeAccount(
        order_detail={
            "20260812": [
                {"order_id": "0000000042", "symbol": "005930", "filled_quantity": 5}
            ]
        }
    )
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.OwnFillAttribution)
    assert attribution.quantity("005930") == Decimal("0")


@pytest.mark.asyncio
async def test_corrupt_journal_is_unreadable_not_empty(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "own-orders.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    journal = kiwoom_attr.OwnOrderJournal(path=path)

    with pytest.raises(kiwoom_attr.JournalUnreadable):
        journal.read_all()

    account = FakeAccount()
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.AttributionUnreadable)


@pytest.mark.asyncio
async def test_broker_detail_failure_is_unreadable_not_empty(tmp_path) -> None:  # noqa: ANN001
    journal = _journal(tmp_path, [_record()])
    account = FakeAccount(detail_error=RuntimeError("kt00007 down"))
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.AttributionUnreadable)


@pytest.mark.asyncio
async def test_missing_journal_file_is_readable_and_empty(tmp_path) -> None:  # noqa: ANN001
    """A lane that never traded is a *readable* answer: everything is legacy."""

    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "absent.jsonl")
    account = FakeAccount()
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    assert isinstance(attribution, kr_attribution.OwnFillAttribution)
    assert attribution.lots == ()


# ---------------------------------------------------------------------------
# 자기 미체결 = 브로커 직접 조회 (§39차 ②)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_comes_from_the_broker_and_splits_own_vs_account() -> None:
    account = FakeAccount(
        resting=[
            (
                resting("111", "005930"),
                resting("222", "000660"),
            )
        ]
    )
    pending = await kiwoom_lane.read_broker_pending(
        account, own_order_ids=frozenset({"111"})
    )
    assert isinstance(pending, kiwoom_lane.BrokerPending)
    assert pending.account_symbols == ("000660", "005930")
    assert pending.own_symbols == ("005930",)
    assert len(pending.foreign_orders) == 1
    assert "kt00009" in pending.canonical()["source"]


@pytest.mark.asyncio
async def test_pending_read_failure_is_tri_state_and_blocks_every_symbol() -> None:
    account = FakeAccount(resting_error=RuntimeError("kt00009 timeout"))
    pending = await kiwoom_lane.read_broker_pending(account, own_order_ids=frozenset())
    assert isinstance(pending, PendingUnreadable)
    assert pending.reason == "kiwoom_mock_pending_read_failed"

    truth = kiwoom_lane.broker_truth_from(position_symbols=(), pending=pending)
    assert truth.resubmit_block("005930") is not None
    assert truth.resubmit_block("000660") is not None


@pytest.mark.asyncio
async def test_resting_order_blocks_a_resubmit_on_that_symbol() -> None:
    """🔴 dedup: the second submission on the same symbol is refused."""

    account = FakeAccount(resting=[(resting("111", "005930"),)])
    pending = await kiwoom_lane.read_broker_pending(
        account, own_order_ids=frozenset({"111"})
    )
    truth = kiwoom_lane.broker_truth_from(position_symbols=(), pending=pending)
    assert truth.resubmit_block("005930") is not None
    assert truth.resubmit_block("035420") is None


@pytest.mark.asyncio
async def test_foreign_resting_order_also_blocks_conservatively() -> None:
    """The superset choice: KR-B1's resting order blocks that symbol too."""

    account = FakeAccount(resting=[(resting("999", "005930"),)])
    pending = await kiwoom_lane.read_broker_pending(account, own_order_ids=frozenset())
    assert isinstance(pending, kiwoom_lane.BrokerPending)
    assert pending.own_symbols == ()
    truth = kiwoom_lane.broker_truth_from(position_symbols=(), pending=pending)
    assert truth.resubmit_block("005930") is not None


def test_false_green_inverting_the_legacy_gate_would_fail_the_suite() -> None:
    """FALSE-GREEN probe for mutant ①.

    If ``scope_positions`` were mutated to treat unattributed holdings as own,
    the assertion below — the same one the real tests rely on — flips. Written
    as an explicit inversion so a reader can see the test has teeth rather than
    trusting that it does.
    """

    fresh = kiwoom_lane.FreshTruth(
        cash=Decimal("0"), nav=Decimal("0"), positions=(position("005930", 10),)
    )
    scoped = kiwoom_cycle.scoped_positions(
        fresh=fresh, attribution=kr_attribution.OwnFillAttribution(lots=())
    )
    mutated_would_pass = bool(scoped.own_positions)
    assert not mutated_would_pass, (
        "own_positions is non-empty with an empty attribution — the legacy gate "
        "is inverted and every legacy holding is now a sell candidate"
    )


# ---------------------------------------------------------------------------
# 🔴 The kt00009 measurement, codified so it cannot be quietly undone.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_gate_reads_kt00007_not_kt00009() -> None:
    """Measured 2026-08-12 on ``mockapi.kiwoom.com``: kt00009 answers empty.

    While B0-X orders ``0107387``/``0108695``/``0109507`` were live on the
    account, kt00009 (계좌별주문체결현황요청) returned ``return_code=0`` with **no
    rows**, and kt00007 returned every row for the same day. An answer that can
    be empty while orders rest cannot prove that none do — the property
    contract v1.5 ① disqualifies, and the same defect class ROB-341 found in
    KIS's daily-execution inquiry.

    Building the resubmit gate on kt00009 would make it *vacuous*: every symbol
    would look free and every "cancel confirmed" would be trivially true,
    because the order was never in the list being checked. This test pins the
    gate to the surface that answers.
    """

    import inspect

    source = inspect.getsource(
        kiwoom_lane.ReadOnlyKiwoomMockAccount.read_resting_orders
    )
    assert "read_order_detail" in source
    assert "get_order_status" not in source

    # kt00009 survives only as a recorded, non-gating diagnostic.
    diagnostic = inspect.getsource(
        kiwoom_lane.ReadOnlyKiwoomMockAccount.read_order_status_diagnostic
    )
    assert "get_order_status" in diagnostic
    assert "never" in diagnostic.lower()

    assert "kt00007" in kiwoom_lane.OWN_PENDING_SOURCE
    assert "kt00009" in kiwoom_lane.OWN_PENDING_SOURCE  # the caveat is stated too


@pytest.mark.asyncio
async def test_resting_predicate_is_broker_ord_remnq() -> None:
    """``ord_remnq > 0`` is the predicate; a settled row is not resting."""

    class _DetailAccount(FakeAccount):
        async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
            return [
                # Live, resting: broker says 3 remain.
                {
                    "order_id": "0109507",
                    "symbol": "000100",
                    "status": "open",
                    "ordered_price": 83_000,
                    "remaining_quantity": 3,
                    "unfilled_quantity": 3,
                },
                # Already cancelled: ord_remnq is 0 even though ord_qty−cntr_qty
                # is still 3. The broker's own remaining figure wins.
                {
                    "order_id": "0108695",
                    "symbol": "000100",
                    "status": "open",
                    "ordered_price": 83_000,
                    "remaining_quantity": 0,
                    "unfilled_quantity": 3,
                },
            ]

    account = _DetailAccount()
    orders = await kiwoom_lane.ReadOnlyKiwoomMockAccount.read_resting_orders(
        account, order_date="20260812"
    )
    assert [order.order_id for order in orders] == ["0109507"]
    assert orders[0].remaining_quantity == 3
    assert orders[0].unfilled_quantity == 3
