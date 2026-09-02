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

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from scripts.b0x.broker_truth import BrokerTruth, OwnPendingResubmitBlocked
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount, resting

pytestmark = pytest.mark.unit

CLEAN_TRUTH = BrokerTruth(position_symbols=(), own_pending=())


def _planned(  # noqa: PLR0913
    symbol: str = "005930",
    quantity: int = 1,
    price: int = 70_000,
    *,
    cycle_id: str = "b0x-test-cycle",
    order_key: str = "deadbeefdeadbeef",
    leg: str = "buy_l1",
):  # noqa: ANN202
    return kiwoom_lane.PlannedOrder(
        cycle_id=cycle_id,
        order_key=order_key,
        client_order_id=f"b0xkw-{order_key}",
        symbol=symbol,
        side="buy",
        leg=leg,
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


async def _run_with_exception_observer(
    account: FakeAccount,
    *,
    now,
    record_order_no=None,  # noqa: ANN001
    **kwargs: Any,
) -> tuple[
    list[dict[str, Any]], kiwoom_lane.RoundTripResult | None, BaseException | None
]:
    observations: list[dict[str, Any]] = []

    async def _observe(
        result: kiwoom_lane.RoundTripResult,
        remaining_quantity: int,
        stage: str,
        error: BaseException,
    ) -> None:
        observations.append(
            {
                "stage": stage,
                "error_type": type(error).__name__,
                "order_no": result.order_no,
                "quantity": remaining_quantity,
            }
        )

    try:
        result = await kiwoom_lane.submit_and_cancel(
            account,
            planned=_planned(),
            broker_truth=CLEAN_TRUTH,
            record_order_no=(_sink() if record_order_no is None else record_order_no),
            now=now,
            on_post_ack_exception=_observe,
            **kwargs,
        )
    except BaseException as error:  # test harness captures exact propagation
        return observations, None, error
    return observations, result, None


def _readback_row(
    *,
    order_no: str = "0000123456",
    symbol: str = "005930",
    ordered_quantity: int = 3,
    filled_quantity: int = 1,
    remaining_quantity: int = 2,
    unfilled_quantity: int = 2,
    ordered_price: int = 70_000,
    average_price: int = 70_100,
) -> dict[str, object]:
    """A broker-detail row for one partial DAY fill."""

    return {
        "order_id": order_no,
        "symbol": symbol,
        "status": "partial",
        "ordered_quantity": ordered_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": remaining_quantity,
        "unfilled_quantity": unfilled_quantity,
        "ordered_price": ordered_price,
        "average_price": average_price,
    }


@pytest.mark.asyncio
async def test_ordering_day_submission_never_calls_cancel(now) -> None:  # noqa: ANN001
    """ORDERING records acceptance, not a synthetic fill or cleanup."""

    account = FakeAccount()
    writer = _sink()

    result = await kiwoom_lane.submit_day_order(
        account,
        planned=_planned(),
        broker_truth=CLEAN_TRUTH,
        record_order_no=writer,
        now=now,
    )

    assert result.submitted is True
    assert result.order_no == "0000123456"
    assert account.buy_calls == [{"symbol": "005930", "quantity": 1, "price": 70_000}]
    assert account.cancel_calls == []
    assert result.canonical() == {
        "correlation_id": "b0xkw-deadbeefdeadbeef",
        "symbol": "005930",
        "side": "buy",
        "price": 70_000,
        "quantity": 1,
        "notional_krw": 70_000,
        "submitted": True,
        "order_no": "0000123456",
        "submit_response": {"return_code": 0, "ord_no": "0000123456"},
        "time_in_force": "DAY",
        "automatic_cancel": False,
        "fill_status": "unverified",
    }
    assert writer.written == [  # type: ignore[attr-defined]
        {"order_no": "0000123456", "symbol": "005930", "at": now}
    ]


@pytest.mark.asyncio
async def test_ordering_day_submission_blocks_same_symbol_own_pending(now) -> None:  # noqa: ANN001
    """E7: the DAY path repeats the dedup check at its mutation boundary."""

    account = FakeAccount()
    blocked_truth = BrokerTruth(position_symbols=(), own_pending=("005930",))

    with pytest.raises(OwnPendingResubmitBlocked):
        await kiwoom_lane.submit_day_order(
            account,
            planned=_planned(symbol="005930"),
            broker_truth=blocked_truth,
            record_order_no=_sink(),
            now=now,
        )

    assert account.buy_calls == [], "a blocked DAY resubmit must not reach the venue"


@pytest.mark.asyncio
async def test_same_cycle_buy_batch_checks_the_common_gate_once(
    now, monkeypatch
) -> None:  # noqa: ANN001
    """The batch proof is exact and the unchanged common gate runs once."""

    calls: list[str] = []
    real_assert = kiwoom_lane.assert_resubmit_allowed

    def counted_assert(truth, *, symbol, lane):  # noqa: ANN001, ANN202
        calls.append(symbol)
        return real_assert(truth, symbol=symbol, lane=lane)

    monkeypatch.setattr(kiwoom_lane, "assert_resubmit_allowed", counted_assert)
    first = _planned(order_key="l1", leg="buy_l1")
    second = _planned(order_key="l2", leg="buy_l2", price=68_000)
    authorization = kiwoom_lane.authorize_same_cycle_buy_batch(
        cycle_id="b0x-test-cycle",
        planned=(first, second),
        broker_truth=CLEAN_TRUTH,
    )
    account = FakeAccount()

    await kiwoom_lane.submit_day_order_in_batch(
        account,
        planned=first,
        authorization=authorization,
        record_order_no=_sink(),
        now=now,
    )
    await kiwoom_lane.submit_day_order_in_batch(
        account,
        planned=second,
        authorization=authorization,
        record_order_no=_sink(),
        now=now,
    )

    assert calls == ["005930"]
    assert authorization.attempted_count == 2
    assert [call["price"] for call in account.buy_calls] == [70_000, 68_000]


@pytest.mark.parametrize(
    "second",
    [
        _planned(cycle_id="b0x-other-cycle", order_key="l2", leg="buy_l2"),
        _planned(symbol="000660", order_key="l2", leg="buy_l2"),
    ],
    ids=("cross-cycle", "cross-symbol"),
)
def test_same_cycle_buy_batch_rejects_boundary_widening(second) -> None:  # noqa: ANN001
    first = _planned(order_key="l1", leg="buy_l1")

    with pytest.raises(kiwoom_lane.SameCycleBuyBatchViolation):
        kiwoom_lane.authorize_same_cycle_buy_batch(
            cycle_id="b0x-test-cycle",
            planned=(first, second),
            broker_truth=CLEAN_TRUTH,
        )


def test_same_cycle_buy_batch_proof_cannot_be_forged() -> None:
    with pytest.raises(
        kiwoom_lane.SameCycleBuyBatchViolation, match="not minted by the gate"
    ):
        kiwoom_lane.SameCycleBuyBatchAuthorization(
            cycle_id="b0x-test-cycle",
            symbol="005930",
            order_keys=("l1", "l2"),
            _proof=object(),
        )


@pytest.mark.asyncio
async def test_ordering_readback_missing_acknowledged_row_is_unavailable(
    now,
) -> None:  # noqa: ANN001
    """E2: an ACK without its kt00007 row is not a fabricated fill."""

    account = FakeAccount(
        order_detail={kiwoom_attr.kst_order_date(now): []},
    )

    with pytest.raises(
        kiwoom_lane.BrokerOrderReadbackUnavailable, match="did not return"
    ):
        await kiwoom_lane.read_order_readback(
            account,
            planned=_planned(quantity=3),
            order_no="0000123456",
            order_date=kiwoom_attr.kst_order_date(now),
            at=now,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("order_id", "0000000999", "order_no"),
        ("symbol", "000660", "symbol"),
        ("ordered_quantity", 4, "ordered_quantity"),
        ("ordered_price", 70_100, "ordered_price"),
        ("filled_quantity", 4, "filled_quantity"),
        ("remaining_quantity", 3, "remaining_quantity"),
    ),
)
def test_ordering_readback_rejects_echo_or_quantity_inconsistency(
    now, field: str, value: str | int, expected: str
) -> None:  # noqa: ANN001
    """E5: every broker echo and fill arithmetic mismatch is fail-closed."""

    row = _readback_row()
    row[field] = value

    with pytest.raises(kiwoom_lane.BrokerEchoMismatch, match=expected):
        kiwoom_lane._readback_from_detail_row(  # noqa: SLF001 - exact guard vector
            row=row,
            planned=_planned(quantity=3),
            order_no="0000123456",
            at=now,
        )


def test_ordering_partial_readback_conserves_remaining_vwap_and_slippage(now) -> None:  # noqa: ANN001
    """The partial-fill artifact is broker-derived, not completion-shaped."""

    readback = kiwoom_lane._readback_from_detail_row(  # noqa: SLF001 - exact guard vector
        row=_readback_row(),
        planned=_planned(quantity=3),
        order_no="0000123456",
        at=now,
    )

    assert readback.partial is True
    assert readback.complete is False
    assert readback.remaining_quantity == 2
    assert readback.canonical()["fill_vwap"] == "70100"
    assert readback.canonical()["slippage_krw"] == "100"


def test_ordering_filled_readback_without_broker_vwap_is_unavailable(now) -> None:  # noqa: ANN001
    """A fill with no positive broker VWAP cannot preserve its artifact."""

    with pytest.raises(
        kiwoom_lane.BrokerOrderReadbackUnavailable, match="positive broker VWAP"
    ):
        kiwoom_lane._readback_from_detail_row(  # noqa: SLF001 - exact guard vector
            row=_readback_row(average_price=0),
            planned=_planned(quantity=3),
            order_no="0000123456",
            at=now,
        )


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
        {"order_no": "0000123456", "symbol": "005930", "at": now},
        {"order_no": "0000123457", "symbol": "005930", "at": now},
    ]


def test_post_ack_exception_stage_vocabulary_is_closed_and_complete() -> None:
    assert kiwoom_lane.POST_ACK_EXCEPTION_STAGES == (
        "buy_ack_journal",
        "pre_cancel_resting_read",
        "pre_cancel_resting_parse",
        "cancel_authority_check",
        "cancel_transport_guard",
        "cancel_request",
        "cancel_ack_parse",
        "cancel_ack_journal",
        "post_cancel_reconcile_read",
        "post_cancel_reconcile_parse",
        "post_cancel_terminal_classification",
    )


@pytest.mark.asyncio
async def test_every_post_ack_exception_exit_reaches_the_closed_observer(now) -> None:  # noqa: ANN001, PLR0915
    """S-E: exercise every fallible stage in the exact ACK→terminal window."""

    order = resting("0000123456", "005930", price=70_000)
    observed_stages: set[str] = set()

    async def _expect(
        expected_stage: str,
        account: FakeAccount,
        **kwargs: Any,
    ) -> None:
        observations, _result, _error = await _run_with_exception_observer(
            account, now=now, **kwargs
        )
        assert [item["stage"] for item in observations] == [expected_stage]
        assert observations[0]["order_no"] == "0000123456"
        assert observations[0]["quantity"] == 1
        observed_stages.add(expected_stage)

    journal_calls = 0

    def _fail_buy_journal_once(**_kwargs: Any) -> None:
        nonlocal journal_calls
        journal_calls += 1
        if journal_calls == 1:
            raise OSError("synthetic buy journal failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_BUY_JOURNAL,
        FakeAccount(resting=[(order,), ()]),
        record_order_no=_fail_buy_journal_once,
        continue_after_journal_error=True,
    )

    class _FirstReadFails(FakeAccount):
        async def read_resting_orders(self):  # noqa: ANN201
            self.resting_calls += 1
            if self.resting_calls == 1:
                raise TimeoutError("synthetic pre-cancel read timeout")
            return ()

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_PRE_CANCEL_READ,
        _FirstReadFails(),
    )

    class _BrokenPreCancelRow:
        order_id = "0000123456"

        @property
        def symbol(self):  # noqa: ANN201
            raise ValueError("synthetic resting-row parse failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_PRE_CANCEL_PARSE,
        FakeAccount(resting=[(_BrokenPreCancelRow(),)]),  # type: ignore[list-item]
    )

    async def _authority_error(
        _result: kiwoom_lane.RoundTripResult,
    ) -> bool:
        raise RuntimeError("synthetic authority callback failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_CANCEL_AUTHORITY,
        FakeAccount(resting=[(order,)]),
        cancel_authority_decision=_authority_error,
    )

    async def _guard_error() -> None:
        raise RuntimeError("synthetic cancel guard failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_CANCEL_GUARD,
        FakeAccount(resting=[(order,)]),
        before_cancel_send=_guard_error,
    )

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_CANCEL_REQUEST,
        FakeAccount(
            resting=[(order,), ()],
            cancel_error=TimeoutError("synthetic cancel timeout"),
        ),
    )

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_CANCEL_ACK_PARSE,
        FakeAccount(
            resting=[(order,), ()],
            cancel_response={"return_code": 0},
        ),
        require_cancel_order_no=True,
    )

    cancel_journal_calls = 0

    def _fail_cancel_journal_once(**_kwargs: Any) -> None:
        nonlocal cancel_journal_calls
        cancel_journal_calls += 1
        if cancel_journal_calls == 2:
            raise OSError("synthetic cancel journal failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_CANCEL_JOURNAL,
        FakeAccount(resting=[(order,), ()]),
        record_order_no=_fail_cancel_journal_once,
        continue_after_journal_error=True,
    )

    class _SecondReadFails(FakeAccount):
        async def read_resting_orders(self):  # noqa: ANN201
            self.resting_calls += 1
            if self.resting_calls == 1:
                return (order,)
            raise ConnectionError("synthetic reconcile network failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_RECONCILE_READ,
        _SecondReadFails(),
    )

    class _BrokenPostCancelRow:
        @property
        def order_id(self):  # noqa: ANN201
            raise ValueError("synthetic reconcile parse failure")

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_RECONCILE_PARSE,
        FakeAccount(
            resting=[(order,), (_BrokenPostCancelRow(),)]  # type: ignore[list-item]
        ),
    )

    await _expect(
        kiwoom_lane.POST_ACK_STAGE_TERMINAL_CLASSIFICATION,
        FakeAccount(resting=[(order,), (order,)]),
    )

    assert observed_stages == set(kiwoom_lane.POST_ACK_EXCEPTION_STAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_error",
    (
        asyncio.CancelledError("synthetic cancellation"),
        KeyboardInterrupt("synthetic interrupt"),
        SystemExit(17),
    ),
)
async def test_process_control_exceptions_are_observed_then_propagated_exactly(
    now, control_error: BaseException
) -> None:  # noqa: ANN001
    """S-E: BaseException observation must never become a swallow boundary."""

    async def _raise_control() -> None:
        raise control_error

    account = FakeAccount(resting=[(resting("0000123456", "005930", price=70_000),)])
    observations, result, propagated = await _run_with_exception_observer(
        account,
        now=now,
        before_cancel_send=_raise_control,
    )

    assert result is None
    assert propagated is control_error
    assert observations == [
        {
            "stage": kiwoom_lane.POST_ACK_STAGE_CANCEL_GUARD,
            "error_type": type(control_error).__name__,
            "order_no": "0000123456",
            "quantity": 1,
        }
    ]
    assert account.cancel_calls == []


@pytest.mark.asyncio
async def test_exception_observer_does_not_change_cancel_state_semantics(now) -> None:  # noqa: ANN001
    """S-E is observation-only even for an internally contained API error."""

    def _account() -> FakeAccount:
        return FakeAccount(
            resting=[
                (resting("0000123456", "005930", price=70_000),),
                (),
            ],
            cancel_error=TimeoutError("synthetic cancel timeout"),
        )

    baseline_account = _account()
    baseline = await kiwoom_lane.submit_and_cancel(
        baseline_account,
        planned=_planned(),
        broker_truth=CLEAN_TRUTH,
        record_order_no=_sink(),
        now=now,
    )
    observed_account = _account()
    observations, observed, propagated = await _run_with_exception_observer(
        observed_account,
        now=now,
    )

    assert propagated is None
    assert observed is not None
    assert observed.canonical() == baseline.canonical()
    assert observed_account.cancel_calls == baseline_account.cancel_calls
    assert observations[0]["stage"] == kiwoom_lane.POST_ACK_STAGE_CANCEL_REQUEST


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
        cycle_id="b0x-test-cycle",
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
async def test_buy_journal_failure_does_not_skip_same_scope_mandatory_cancel(
    now,
) -> None:  # noqa: ANN001
    account = FakeAccount(
        buy_response={"return_code": 0, "ord_no": "0107387"},
        cancel_response={"return_code": 0, "ord_no": "0107388"},
        resting=[
            (resting("0107387", "005930", remaining=1, price=70_000),),
            (),
        ],
    )
    calls = 0

    def journal(**_kwargs):  # noqa: ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fake journal unavailable")

    result = await kiwoom_lane.submit_and_cancel(
        account,
        planned=_planned(),
        broker_truth=CLEAN_TRUTH,
        record_order_no=journal,
        now=now,
        continue_after_journal_error=True,
        require_cancel_order_no=True,
        raise_on_incomplete=False,
    )

    assert result.order_no == "0107387"
    assert result.journal_error_types == ["OSError"]
    assert result.cancel_attempted is True
    assert result.cancel_confirmed is True
    assert len(account.cancel_calls) == 1


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
