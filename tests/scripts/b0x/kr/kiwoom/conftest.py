"""Shared doubles for the ``kiwoom_mock`` B0-X lane tests.

Two rules this package holds itself to, both learned from the kis lane's
conftest:

* **A test that forgets to inject must fail loudly.** Every fake below raises
  on an un-stubbed call rather than returning an empty/success-shaped answer,
  because this lane's failure branches are all fail-*closed* — a silent fall
  into them would keep a test green while proving nothing.
* **No test may reach a real broker or the application database.** The lane's
  only DB-capable import (:mod:`scripts.b0x.kr.attribution`, for its pure
  helpers) has its reader replaced with a raising stub here, which is also the
  mutant ② guard at runtime: if any kiwoom code path ever calls the kis ledger
  reader, these tests explode instead of quietly passing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import kiwoom as kiwoom_lane


@pytest.fixture(autouse=True)
def _forbid_kis_ledger_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 mutant ② runtime guard — the kis ledger reader is off-limits here."""

    async def _refuse(*, correlation_prefix: str) -> Any:
        raise AssertionError(
            "a kiwoom lane test reached the kis_mock 원장 reader "
            f"(prefix={correlation_prefix!r}). 계약 v1.6 ① 예외는 브로커 표면 "
            "부재(kis_mock) 한정이며 kiwoom 에 쓰면 계약 위반이다 (§39차 2항)."
        )

    monkeypatch.setattr(kr_attribution, "read_own_attribution", _refuse)


@pytest.fixture(autouse=True)
def _arm_lane_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the env gate *unset* by default so its default-off state is real."""

    monkeypatch.delenv("B0X_KR_KIWOOM_ENABLED", raising=False)


class FakeAccount:
    """Duck-typed stand-in for :class:`kiwoom.ReadOnlyKiwoomMockAccount`.

    Records every call so a test can assert what did and did not reach the
    venue — in particular that a blocked leg produced **zero** order calls.
    """

    def __init__(
        self,
        *,
        cash: Decimal = Decimal("10000000"),
        positions: tuple[kiwoom_lane.RawPosition, ...] = (),
        resting: list[tuple[kiwoom_lane.RestingOrder, ...]] | None = None,
        order_detail: dict[str, list[dict[str, Any]]] | None = None,
        buy_response: dict[str, Any] | None = None,
        cancel_response: dict[str, Any] | None = None,
        buy_error: Exception | None = None,
        cancel_error: Exception | None = None,
        resting_error: Exception | None = None,
        detail_error: Exception | None = None,
    ) -> None:
        self._cash = cash
        self._positions = positions
        #: A *sequence* of answers: index N is returned by the N-th call, and
        #: the last entry repeats. Round-trip tests need the resting set to
        #: change between "before cancel" and "after cancel".
        self._resting = resting if resting is not None else [()]
        self._order_detail = order_detail or {}
        self._buy_response = buy_response or {"return_code": 0, "ord_no": "0000123456"}
        self._cancel_response = cancel_response or {"return_code": 0}
        self._buy_error = buy_error
        self._cancel_error = cancel_error
        self._resting_error = resting_error
        self._detail_error = detail_error
        self.resting_calls = 0
        self.diagnostic_calls = 0
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []

    async def read_cash(self) -> Decimal:
        return self._cash

    async def read_positions(self) -> tuple[kiwoom_lane.RawPosition, ...]:
        return self._positions

    async def read_resting_orders(self) -> tuple[kiwoom_lane.RestingOrder, ...]:
        if self._resting_error is not None:
            raise self._resting_error
        index = min(self.resting_calls, len(self._resting) - 1)
        self.resting_calls += 1
        return self._resting[index]

    async def read_order_status_diagnostic(self) -> dict[str, Any]:
        """kt00009 diagnostic. Deliberately returns the *measured* empty answer.

        The real mock answers ``return_code=0`` with no rows even while orders
        rest (2026-08-12), so the double here mirrors that rather than a
        hypothetical working surface — a test that assumed kt00009 worked would
        be testing a venue this lane does not have.
        """

        self.diagnostic_calls += 1
        return {"api": "kt00009", "readable": True, "row_count": 0, "open_row_count": 0}

    async def read_order_detail(
        self, *, order_date: str | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        if self._detail_error is not None:
            raise self._detail_error
        self.detail_calls.append({"order_date": order_date, "symbol": symbol})
        if order_date is not None:
            return list(self._order_detail.get(order_date, []))
        if self._resting_error is not None:
            raise self._resting_error
        # Legacy acceptance tests describe their broker-pending snapshots with
        # ``resting=``.  The runtime now reads kt00007 directly, so translate
        # that fixture shorthand into normalized detail rows only for its
        # order-date-unspecified pending read.  Same-day foreign/readback
        # vectors must supply explicit ``order_detail`` evidence.
        index = min(self.resting_calls, len(self._resting) - 1)
        self.resting_calls += 1
        return [
            {
                "order_id": item.order_id,
                "symbol": item.symbol,
                "status": item.status,
                "ordered_quantity": item.remaining_quantity,
                "filled_quantity": 0,
                "remaining_quantity": item.remaining_quantity,
                "unfilled_quantity": item.remaining_quantity,
                "ordered_price": item.ordered_price,
                "average_price": 0,
            }
            for item in self._resting[index]
        ]

    async def place_limit_buy(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        if self._buy_error is not None:
            raise self._buy_error
        return dict(self._buy_response)

    async def place_limit_sell(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        self.sell_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        if self._buy_error is not None:
            raise self._buy_error
        return dict(self._buy_response)

    async def cancel(
        self, *, original_order_no: str, symbol: str, cancel_quantity: int
    ) -> dict[str, Any]:
        self.cancel_calls.append(
            {
                "original_order_no": original_order_no,
                "symbol": symbol,
                "cancel_quantity": cancel_quantity,
            }
        )
        if self._cancel_error is not None:
            raise self._cancel_error
        return dict(self._cancel_response)


def position(
    symbol: str,
    quantity: int,
    *,
    average_price: int = 10_000,
    evaluation_amount: int | None = None,
) -> kiwoom_lane.RawPosition:
    return kiwoom_lane.RawPosition(
        symbol=symbol,
        quantity=Decimal(quantity),
        average_price=Decimal(average_price),
        evaluation_amount=Decimal(
            quantity * average_price if evaluation_amount is None else evaluation_amount
        ),
    )


def resting(
    order_id: str,
    symbol: str,
    *,
    remaining: int = 1,
    price: int = 10_000,
    status: str = "open",
) -> kiwoom_lane.RestingOrder:
    return kiwoom_lane.RestingOrder(
        order_id=order_id,
        symbol=symbol,
        status=status,
        remaining_quantity=remaining,
        ordered_price=price,
    )


@pytest.fixture
def now() -> dt.datetime:
    # 2026-08-12 (Wed) 03:00Z == 12:00 KST — inside the KRX regular session.
    return dt.datetime(2026, 8, 12, 3, 0, tzinfo=dt.UTC)
