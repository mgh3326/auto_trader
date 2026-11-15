"""hk 4754 (3797 T-P8) — US Kiwoom mock place pipeline.

Unit tests with a stub US order client and in-memory recorders: no database, no
network, no broker. Every refused path is proven to never reach the broker by a
POST count of zero, and every accepted path is proven to write the attribution
signal before the reservation and the ledger row after the broker answered.

Invariant map (design §6.1, US lane):

* I2 — a duplicate ``(account_scope='kiwoom_mock_us', key)`` reservation refuses
  before any broker call: ``test_duplicate_intent_refuses_before_the_send``.
* I3 — a send whose outcome is unknown is recorded ``uncertain``, keeps its
  reservation and is never resent: ``test_dispatch_unknown_is_uncertain_and_kept``.
* I5 — an order id that the US validator cannot track is not an acknowledgement:
  ``test_untrackable_order_id_is_uncertain_not_accepted``.
* I7 — the default-disabled US gate is the caller's; the pipeline itself never
  sends without a committed signal row and a committed reservation.
* Attribution — no strategy, no send: ``test_missing_strategy_sends_nothing``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.tooling.orders_kiwoom_shared import (
    finalize_place_broker_response,
)
from app.services.brokers.kiwoom.client import KiwoomPreDispatchError
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK_US,
    build_us_client_order_id,
    us_trading_day,
)
from app.services.kiwoom_mock_place import (
    KiwoomMockUsPlaceDeps,
    UsPlacePreflight,
    place_kiwoom_mock_us_order,
)
from tests.test_mcp_kiwoom_order_variants import (
    FakeKiwoomPlaceRecorder,
    FakeKiwoomSignalRecorder,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

#: 2026-11-16 14:00 UTC = 09:00 ET (EST) — inside the regular session.
FROZEN_NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
ACK = {"return_code": 0, "return_msg": "모의투자 매수주문완료", "ord_no": "000000063"}
REJECT = {"return_code": 20, "return_msg": "주문가능금액 부족"}


class StubOrderClient:
    """Counts broker POSTs; ``behaviour`` decides each answer in order."""

    def __init__(self, behaviour: list[Any]) -> None:
        self.behaviour = behaviour
        self.posts: list[dict[str, Any]] = []

    def factory(self):
        stub = self

        class _OrderClient:
            def __init__(self, _client):
                del _client

            async def _post(self, method: str, **kwargs):
                stub.posts.append({"method": method, **kwargs})
                step = stub.behaviour[min(len(stub.posts), len(stub.behaviour)) - 1]
                if isinstance(step, BaseException):
                    raise step
                return step

            async def place_buy_order(self, **kwargs):
                return await self._post("buy", **kwargs)

            async def place_sell_order(self, **kwargs):
                return await self._post("sell", **kwargs)

        return _OrderClient


def _deps(
    behaviour: list[Any],
    *,
    clock: datetime = FROZEN_NOW,
    preflight: UsPlacePreflight | None = None,
) -> tuple[KiwoomMockUsPlaceDeps, StubOrderClient, FakeKiwoomPlaceRecorder]:
    stub = StubOrderClient(behaviour)
    place = FakeKiwoomPlaceRecorder()
    signal = FakeKiwoomSignalRecorder()

    async def _preflight(**_kwargs: Any) -> UsPlacePreflight:
        return preflight or UsPlacePreflight(ok=True, stex_tp="ND")

    deps = KiwoomMockUsPlaceDeps(
        new_client=lambda: object(),
        order_client_cls=stub.factory(),
        preflight=_preflight,
        place_recorder=lambda: place,
        signal_recorder=lambda: signal,
        utc_now=lambda: clock,
        finalize=finalize_place_broker_response,
    )
    place.signal = signal  # type: ignore[attr-defined]
    return deps, stub, place


async def _place(deps: KiwoomMockUsPlaceDeps, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "symbol": "NVDA",
        "side": "buy",
        "quantity": 1,
        "price": Decimal("213.04"),
        "trde_tp": "00",
        "market": "us",
        "dry_run": False,
        "strategy": "unit-test",
        "deps": deps,
    }
    kwargs.update(overrides)
    return await place_kiwoom_mock_us_order(**kwargs)


# --- accepted ---------------------------------------------------------------


async def test_accepted_place_writes_signal_reservation_and_ledger_row() -> None:
    deps, stub, place = _deps([ACK])

    result = await _place(deps)

    assert result["success"] is True
    assert result["status"] == "submitted"
    assert result["order_id"] == "000000063"
    assert result["account_mode"] == ACCOUNT_MODE_KIWOOM_MOCK_US
    assert result["stex_tp"] == "ND"
    assert result["ledger_recorded"] is True
    assert result["ledger_status"] == "accepted"
    # Attribution is committed before the reservation, which is committed
    # before the POST, which is recorded after the broker answered.
    assert place.events == ["reserve", "record:accepted"]
    signal = place.signal
    assert signal.events[0] == "signal"
    assert signal.rows[0]["account_mode"] == ACCOUNT_MODE_KIWOOM_MOCK_US
    assert signal.rows[0]["instrument_type"] == "equity_us"
    row = place.rows[0]
    assert row["price"] == Decimal("213.04")
    assert row["trading_day"] == "20261116"
    assert row["order_type"] == "limit"
    assert stub.posts == [
        {
            "method": "buy",
            "symbol": "NVDA",
            "stex_tp": "ND",
            "quantity": 1,
            "trde_tp": "00",
            "price": Decimal("213.04"),
        }
    ]


async def test_market_order_carries_no_price() -> None:
    deps, stub, place = _deps([ACK])

    result = await _place(deps, trde_tp="03", price=None)

    assert result["success"] is True
    assert stub.posts[0]["price"] is None
    assert stub.posts[0]["trde_tp"] == "03"
    assert place.rows[0]["order_type"] == "market"
    assert place.rows[0]["price"] is None


async def test_nine_digit_order_id_is_the_tracked_identifier() -> None:
    deps, _stub, place = _deps([{"return_code": 0, "ord_no": "000000619"}])

    result = await _place(deps, side="sell", quantity=2)

    assert result["order_id"] == "000000619"
    assert place.rows[0]["broker_order_id"] == "000000619"
    assert place.rows[0]["side"] == "sell"


# --- pre-send refusals: zero POSTs -----------------------------------------


async def test_missing_strategy_sends_nothing() -> None:
    deps, stub, place = _deps([ACK])

    result = await _place(deps, strategy=None)

    assert result["success"] is False
    assert result["error_code"] == "attribution_required"
    assert result["status"] == "not_submitted"
    assert "kiwoom_mock_us order is unattributed" in result["error"]
    assert stub.posts == []
    assert place.reserved == {}
    assert place.rows == []


async def test_signal_record_failure_sends_nothing() -> None:
    deps, stub, place = _deps([ACK])
    place.signal.record_error = RuntimeError("db down")  # type: ignore[attr-defined]

    result = await _place(deps)

    assert result["error_code"] == "signal_record_unavailable"
    assert stub.posts == []
    assert place.reserved == {}


async def test_preflight_refusal_sends_nothing_and_is_terminal() -> None:
    deps, stub, place = _deps(
        [ACK],
        preflight=UsPlacePreflight(
            ok=False,
            error_code="place_order_validation_failed",
            error_detail="kiwoom_mock_us_place_order failed: KiwoomUsOrderRejected",
        ),
    )

    result = await _place(deps)

    assert result["success"] is False
    assert result["error_code"] == "place_order_validation_failed"
    assert result["status"] == "not_submitted"
    assert result["reconcile_required"] is False
    assert stub.posts == []
    assert place.reserved == {}
    assert place.signal.events == [  # type: ignore[attr-defined]
        "signal",
        "signal_not_sent:preflight_refused",
    ]


async def test_non_us_market_and_non_positive_quantity_are_refused() -> None:
    deps, stub, place = _deps([ACK])

    wrong_market = await _place(deps, market="kr")
    zero = await _place(deps, quantity=0)

    for result in (wrong_market, zero):
        assert result["success"] is False
        assert "market='us' and quantity > 0" in result["error"]
    assert stub.posts == []
    assert place.reserved == {}


# --- I2: one send per key ---------------------------------------------------


async def test_duplicate_intent_refuses_before_the_send() -> None:
    deps, stub, place = _deps([ACK])

    first = await _place(deps)
    second = await _place(deps)

    assert first["success"] is True
    assert second["success"] is False
    assert second["error"] == "kiwoom_mock_duplicate_order_intent"
    assert second["reconcile_required"] is True
    assert len(stub.posts) == 1


async def test_deliberate_second_order_same_day_uses_the_rung_discriminator() -> None:
    deps, stub, place = _deps([ACK])

    first = await _place(deps, rung=None)
    second = await _place(deps, rung=1)

    assert first["client_order_id"] != second["client_order_id"]
    assert len(stub.posts) == 2


async def test_reservation_failure_sends_nothing() -> None:
    deps, stub, place = _deps([ACK])
    place.reserve_error = RuntimeError("intent table down")

    result = await _place(deps)

    assert result["error"] == "kiwoom_mock_us_send_intent_unavailable: RuntimeError"
    assert result["status"] == "not_submitted"
    assert result["reconcile_required"] is False
    assert stub.posts == []


# --- I3 / I5: ambiguity is never a success and never resent ----------------


async def test_dispatch_unknown_is_uncertain_and_kept() -> None:
    deps, stub, place = _deps([TimeoutError("no answer")])

    result = await _place(deps)

    assert result["success"] is False
    assert result["status"] == "acceptance_uncertain"
    assert result["reconcile_required"] is True
    assert result["retry_allowed"] is False
    assert result["ledger_status"] == "uncertain"
    assert place.events == ["reserve", "record:uncertain"]
    # The reservation stays: the same key must never be sent again.
    assert list(place.reserved) == [result["client_order_id"]]
    assert len(stub.posts) == 1


async def test_pre_dispatch_error_is_not_submitted_and_releases() -> None:
    deps, stub, place = _deps(
        [KiwoomPreDispatchError(stage="auth", api_id="ust20000", cause_type="token")]
    )

    result = await _place(deps)

    assert result["success"] is False
    assert result["status"] == "not_submitted"
    assert result["reconcile_required"] is False
    assert result["retry_allowed"] is False
    assert result["reservation_released"] is True
    assert place.reserved == {}
    assert place.rows == []
    assert len(stub.posts) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"return_code": 0},
        {"return_code": 0, "ord_no": ""},
        {"return_code": 0, "ord_no": "not-digits"},
        {"return_code": 0, "ord_no": 282},
        {"return_code": 0, "ord_no": "1" * 19},
    ],
)
async def test_untrackable_order_id_is_uncertain_not_accepted(raw) -> None:
    deps, _stub, place = _deps([raw])

    result = await _place(deps)

    assert result["success"] is False
    # The US envelope separates "the broker accepted but no order id can be
    # tracked" from a transport failure; both are ambiguous, neither is success.
    assert result["status"] == "accepted_untracked"
    assert result["reconcile_required"] is True
    assert result["ledger_status"] == "uncertain"
    assert result["retry_allowed"] is False
    assert place.rows[0]["broker_order_id"] is None


async def test_explicit_rejection_is_terminal() -> None:
    deps, _stub, place = _deps([REJECT])

    result = await _place(deps)

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["reconcile_required"] is False
    assert place.rows[0]["status"] == "rejected"
    assert place.rows[0]["response_code"] == "20"


# --- dry run ---------------------------------------------------------------


async def test_dry_run_previews_without_attribution_or_reservation() -> None:
    deps, stub, place = _deps([ACK])

    result = await _place(deps, dry_run=True, strategy=None)

    assert result["success"] is True
    assert result["preview"] is True
    assert result["stex_tp"] == "ND"
    assert stub.posts == []
    assert place.reserved == {}
    assert place.rows == []
    assert place.signal.rows == []  # type: ignore[attr-defined]


async def test_pre_send_hook_block_releases_and_propagates() -> None:
    deps, stub, place = _deps([ACK])

    class _Blocked(RuntimeError):
        pass

    async def _hook() -> None:
        raise _Blocked("outside the session")

    with pytest.raises(_Blocked):
        await _place(deps, pre_send_hook=_hook)

    assert stub.posts == []
    assert place.events == ["reserve", "release"]
    assert place.rows == []


# --- ET trading day (design §2.2) -----------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected_day"),
    [
        # 2026-11-16 is a Monday; EST (UTC-5) so 20:00 ET = 01:00Z next day.
        (datetime(2026, 11, 17, 0, 59, 59, tzinfo=UTC), "20261116"),  # 19:59 ET
        (datetime(2026, 11, 17, 1, 0, tzinfo=UTC), "20261117"),  # 20:00 ET rollover
        # DST: 2026-07-06 is a Monday in EDT (UTC-4), 20:00 ET = 00:00Z.
        (datetime(2026, 7, 6, 23, 59, 59, tzinfo=UTC), "20260706"),  # 19:59 EDT
        (datetime(2026, 7, 7, 0, 0, tzinfo=UTC), "20260707"),  # 20:00 EDT
    ],
)
async def test_trading_day_rolls_over_at_2000_et(moment, expected_day) -> None:
    deps, _stub, place = _deps([ACK], clock=moment)

    result = await _place(deps)

    assert us_trading_day(moment) == expected_day
    assert place.rows[0]["trading_day"] == expected_day
    assert result["client_order_id"] == build_us_client_order_id(
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=Decimal("213.04"),
        exchange="ND",
        order_type="limit",
        trading_day=expected_day,
    )


async def test_send_key_is_stable_within_a_trading_day_and_new_the_next() -> None:
    first_deps, _stub, _place_recorder = _deps(
        [ACK], clock=datetime(2026, 11, 17, 0, 59, tzinfo=UTC)
    )
    second_deps, _stub2, _place2 = _deps(
        [ACK], clock=datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
    )

    before = await _place(first_deps)
    after = await _place(second_deps)

    assert before["client_order_id"] != after["client_order_id"]
    assert before["client_order_id"].startswith("kwoomus-")
    assert after["client_order_id"].startswith("kwoomus-")


async def test_client_order_id_is_exact_decimal_text() -> None:
    # ``100`` and ``100.00`` are one order; a float round trip would split them.
    assert build_us_client_order_id(
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=Decimal("100"),
        exchange="ND",
        order_type="limit",
        trading_day="20261116",
    ) == build_us_client_order_id(
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=Decimal("100.00"),
        exchange="ND",
        order_type="limit",
        trading_day="20261116",
    )


async def test_kr_send_key_is_unchanged_by_the_us_work() -> None:
    from app.services.kiwoom_mock_order_ledger_service import build_client_order_id

    assert (
        build_client_order_id(
            symbol="005930",
            side="buy",
            quantity=3,
            price=70000,
            exchange="KRX",
            trading_day="20261116",
        )
        == "kwoomm-feed3f60634f74d6"
    )
