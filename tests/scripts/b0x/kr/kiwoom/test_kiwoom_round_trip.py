"""Mutant ⑤ — a cancel that did not happen must never read as success.

The acceptance claim this lane makes is "제출 → 조회 → 취소 → reconcile", and the
only part of it that can be faked cheaply is the last two steps. Three distinct
ways a fake could creep in, each covered below:

1. Trusting ``kt10003``'s own response. Kiwoom answers HTTP 200 with an in-body
   ``return_code``; a non-zero one is a rejection, and even a zero one is only
   an acknowledgement, not proof the order left the book.
2. Skipping the post-cancel read. Then ``cancel_confirmed`` would be a constant.
3. Swallowing the reconcile read's own failure. "Could not check" is not
   "confirmed gone".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.b0x.broker_truth import BrokerTruth
from scripts.b0x.kr import kiwoom as kiwoom_lane
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount, resting

pytestmark = pytest.mark.unit

CLEAN_TRUTH = BrokerTruth(position_symbols=(), own_pending=())


def _planned(symbol: str = "005930", quantity: int = 1, price: int = 70_000):  # noqa: ANN202
    return kiwoom_lane.PlannedOrder(
        order_key="deadbeefdeadbeef",
        client_order_id="b0xkw-deadbeefdeadbeef",
        symbol=symbol,
        side="buy",
        leg="buy_l1",
        price=price,
        quantity=quantity,
        notional=Decimal(price * quantity),
    )


def _sink():  # noqa: ANN202
    written: list[dict] = []

    def _record(*, order_no, planned, at):  # noqa: ANN001, ANN003
        written.append({"order_no": order_no, "symbol": planned.symbol, "at": at})

    _record.written = written  # type: ignore[attr-defined]
    return _record


@pytest.mark.asyncio
async def test_happy_round_trip_confirms_cancellation_from_the_broker(now) -> None:  # noqa: ANN001
    planned = _planned()
    account = FakeAccount(
        resting=[
            (
                resting("0000123456", "005930", remaining=1, price=70_000),
            ),  # after submit
            (),  # after cancel — the order is gone
        ]
    )
    writer = _sink()

    result = await kiwoom_lane.submit_and_cancel(
        account,
        planned=planned,
        broker_truth=CLEAN_TRUTH,
        record_order_no=writer,
        now=now,
    )

    assert result.submitted is True
    assert result.order_no == "0000123456"
    assert result.observed_resting is True
    assert result.cancel_attempted is True
    assert result.cancel_confirmed is True
    assert result.canonical()["round_trip_complete"] is True
    assert account.cancel_calls == [
        {
            "original_order_no": "0000123456",
            "symbol": "005930",
            "cancel_quantity": 1,
        }
    ]
    # 🔴 The journal is written the moment the order number exists, before
    # anything downstream can fail.
    assert writer.written == [  # type: ignore[attr-defined]
        {"order_no": "0000123456", "symbol": "005930", "at": now}
    ]


@pytest.mark.asyncio
async def test_still_resting_after_cancel_is_a_failure_not_a_success(now) -> None:  # noqa: ANN001
    """🔴 mutant ⑤ — the order is still on the book; refuse to call it clean."""

    account = FakeAccount(
        resting=[
            (resting("0000123456", "005930", price=70_000),),
            (resting("0000123456", "005930", price=70_000),),  # cancel did nothing
        ]
    )

    with pytest.raises(kiwoom_lane.RoundTripIncomplete) as excinfo:
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(),
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )
    assert "still reported as resting" in str(excinfo.value)
    assert account.cancel_calls, "cancel must still have been attempted"


@pytest.mark.asyncio
async def test_broker_rejected_cancel_is_not_laundered(now) -> None:  # noqa: ANN001
    """A non-zero ``return_code`` from kt10003 cannot produce a clean run."""

    account = FakeAccount(
        resting=[
            (resting("0000123456", "005930", price=70_000),),
            (resting("0000123456", "005930", price=70_000),),
        ],
        cancel_error=kiwoom_lane.KiwoomBrokerRejected(
            api="kt10003", return_code=2, return_msg="필수입력 파라미터"
        ),
    )
    with pytest.raises(kiwoom_lane.RoundTripIncomplete):
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(),
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )


@pytest.mark.asyncio
async def test_reconcile_read_failure_is_not_confirmation(now) -> None:  # noqa: ANN001
    """ "조회 불가" ≠ "취소 확인". A failed reconcile read must raise."""

    class _FlakyAccount(FakeAccount):
        async def read_resting_orders(self):  # noqa: ANN201
            self.resting_calls += 1
            if self.resting_calls == 1:
                return (resting("0000123456", "005930", price=70_000),)
            raise RuntimeError("kt00009 timeout")

    account = _FlakyAccount()
    with pytest.raises(kiwoom_lane.RoundTripIncomplete) as excinfo:
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(),
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )
    assert "unproven" in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_order_number_stops_before_anything_is_claimed(now) -> None:  # noqa: ANN001
    """No ``ord_no`` ⇒ nothing can be cancelled or attributed later."""

    account = FakeAccount(buy_response={"return_code": 0})
    with pytest.raises(kiwoom_lane.BrokerEchoMismatch):
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(),
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )
    assert account.cancel_calls == []


@pytest.mark.asyncio
async def test_resting_row_that_does_not_echo_the_request_is_rejected(now) -> None:  # noqa: ANN001
    """A row for a different symbol is not evidence for our order."""

    account = FakeAccount(
        resting=[(resting("0000123456", "000660", remaining=1, price=70_000),)]
    )
    with pytest.raises(kiwoom_lane.BrokerEchoMismatch):
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(symbol="005930"),
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )


@pytest.mark.asyncio
async def test_dedup_blocks_the_second_submission_on_the_same_symbol(now) -> None:  # noqa: ANN001
    """🔴 The right answer to a repeat is refusal — with zero venue calls."""

    account = FakeAccount()
    blocked_truth = BrokerTruth(position_symbols=(), own_pending=("005930",))

    from scripts.b0x.broker_truth import OwnPendingResubmitBlocked

    with pytest.raises(OwnPendingResubmitBlocked):
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(symbol="005930"),
            broker_truth=blocked_truth,
            record_order_no=_sink(),
            now=now,
        )
    assert account.buy_calls == [], "a blocked resubmit must not reach the venue"


@pytest.mark.asyncio
async def test_sell_side_is_refused_by_the_acceptance_lever(now) -> None:  # noqa: ANN001
    """A sell cannot be taken back by the mandatory cancel, so it is not wired."""

    sell = kiwoom_lane.PlannedOrder(
        order_key="k",
        client_order_id="b0xkw-k",
        symbol="005930",
        side="sell",
        leg="sell_r1",
        price=70_000,
        quantity=1,
        notional=Decimal("70000"),
    )
    account = FakeAccount()
    with pytest.raises(ValueError, match="buy-only"):
        await kiwoom_lane.submit_and_cancel(
            account,
            planned=sell,
            broker_truth=CLEAN_TRUTH,
            record_order_no=_sink(),
            now=now,
        )
    assert account.buy_calls == []


def test_false_green_cancel_confirmed_is_not_a_constant() -> None:
    """FALSE-GREEN probe for mutant ⑤.

    ``cancel_confirmed`` defaults to ``False`` and is only ever assigned from a
    post-cancel broker read. A mutation that hard-codes it ``True`` would make
    ``test_still_resting_after_cancel_is_a_failure_not_a_success`` fail, because
    that test asserts the raise, not the flag. This documents the coupling.
    """

    fresh = kiwoom_lane.RoundTripResult(
        correlation_id="b0xkw-x", symbol="005930", side="buy", price=1, quantity=1
    )
    assert fresh.cancel_confirmed is False
    assert fresh.canonical()["round_trip_complete"] is False


@pytest.mark.asyncio
async def test_cancel_order_number_is_journalled_as_ours(now) -> None:  # noqa: ANN001
    """🔴 Regression for the 2026-08-12 12:13 KST false CONTAMINATED.

    A Kiwoom cancel is itself an order with its own ``ord_no`` (buy ``0107387``
    → cancel ``0107388``). The first acceptance attempt journalled only the buy,
    so the next cycle's same-day foreign-trace gate saw this lane's own cancel
    as a second writer and refused to start. Both numbers must be recorded.
    """

    account = FakeAccount(
        resting=[
            (resting("0107387", "005930", price=70_000),),
            (),
        ],
        buy_response={"return_code": 0, "ord_no": "0107387"},
        cancel_response={"return_code": 0, "ord_no": "0107388"},
    )
    writer = _sink()

    result = await kiwoom_lane.submit_and_cancel(
        account,
        planned=_planned(),
        broker_truth=CLEAN_TRUTH,
        record_order_no=writer,
        now=now,
    )

    assert result.cancel_confirmed is True
    assert result.cancel_order_no == "0107388"
    assert [entry["order_no"] for entry in writer.written] == [  # type: ignore[attr-defined]
        "0107387",
        "0107388",
    ]


@pytest.mark.asyncio
async def test_cancel_without_an_echoed_order_number_is_recorded_not_invented(
    now,
) -> None:  # noqa: ANN001
    account = FakeAccount(
        resting=[(resting("0107387", "005930", price=70_000),), ()],
        buy_response={"return_code": 0, "ord_no": "0107387"},
        cancel_response={"return_code": 0},
    )
    writer = _sink()
    result = await kiwoom_lane.submit_and_cancel(
        account,
        planned=_planned(),
        broker_truth=CLEAN_TRUTH,
        record_order_no=writer,
        now=now,
    )
    assert result.cancel_order_no is None
    assert [entry["order_no"] for entry in writer.written] == ["0107387"]  # type: ignore[attr-defined]


def test_cancel_rows_never_move_an_attributed_quantity() -> None:
    """A ``side="cancel"`` journal row proves ownership and nothing else."""

    from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr

    records = (
        kiwoom_attr.OwnOrderRecord(
            at="2026-08-12T03:11:55+00:00",
            order_no="0107387",
            correlation_id="b0xkw-x",
            symbol="000100",
            side="buy",
            price=83_000,
            quantity=3,
            order_date="20260812",
        ),
        kiwoom_attr.OwnOrderRecord(
            at="2026-08-12T03:11:56+00:00",
            order_no="0107388",
            correlation_id="b0xkw-x",
            symbol="000100",
            side="cancel",
            price=83_000,
            quantity=3,
            order_date="20260812",
        ),
    )
    attribution = kiwoom_attr.build_attribution(
        journal=records, filled_by_order_no={"0107387": 0, "0107388": 3}
    )
    # The buy never filled and the cancel is not a trade — nothing attributed.
    assert attribution.lots == ()
