"""B0-X sidecar cancel — attribution gate tests.

The property under test is not "cancel works". It is **cancel does not reach
another writer's order**, on a Demo account whose credentials are shared.
Every test below is written so that the obvious wrong implementation (cancel
everything the account has resting) fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from scripts.b0x.crypto import cancel as cancel_module


@dataclass
class _Order:
    symbol: str
    client_order_id: str
    broker_order_id: str = "1"
    side: str = "BUY"
    qty: Decimal = Decimal("0.001")
    status: str = "NEW"


@dataclass
class _OpenOrders:
    orders: list[Any]


class _FakeClient:
    """Records every cancel it is asked to dispatch."""

    def __init__(self, orders: list[_Order]) -> None:
        self._orders = orders
        self.cancel_calls: list[dict[str, Any]] = []
        self.symbol_scoped_calls: list[str] = []

    async def get_all_open_orders(self) -> _OpenOrders:
        return _OpenOrders(orders=list(self._orders))

    async def get_open_orders(self, *, symbol: str) -> _OpenOrders:
        # Present so a regression to symbol-scoped scanning is visible rather
        # than an AttributeError that could be misread as an unrelated bug.
        self.symbol_scoped_calls.append(symbol)
        return _OpenOrders(orders=[o for o in self._orders if o.symbol == symbol])

    async def cancel_order(
        self, *, symbol: str, client_order_id: str, confirm: bool = False
    ) -> Any:
        self.cancel_calls.append(
            {"symbol": symbol, "client_order_id": client_order_id, "confirm": confirm}
        )

        class _Result:
            status = "CANCELED" if confirm else "DRY_RUN"

        return _Result()


_MINE_BTC = _Order(symbol="BTCUSDT", client_order_id="b0xc-40f2525f66712ec0")
_MINE_ETH = _Order(symbol="ETHUSDT", client_order_id="b0xc-612791b7a7e81b65")
# A foreign order on an allowlisted symbol — the dangerous case, because a
# symbol-based filter would happily cancel it.
_FOREIGN_SAME_SYMBOL = _Order(symbol="BTCUSDT", client_order_id="scalp-7781")
# A foreign order on a symbol B0-X does not even trade.
_FOREIGN_OTHER_SYMBOL = _Order(symbol="XRPUSDT", client_order_id="x-1")


# ---------------------------------------------------------------------------
# Attribution predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "client_order_id,expected",
    [
        ("b0xc-40f2525f66712ec0", True),
        ("b0xc-", False),  # prefix with no key is not a well-formed b0x id
        ("scalp-7781", False),
        ("", False),
        (None, False),
        # Near-misses that must not be read as ours.
        ("xb0xc-1", False),
        ("B0XC-1", False),
        ("b0xcabc", False),
    ],
)
def test_attribution_is_exact_prefix_match(
    client_order_id: str | None, expected: bool
) -> None:
    assert cancel_module.is_b0x_order(client_order_id) is expected


@pytest.mark.unit
def test_unattributable_order_is_foreign_not_ours() -> None:
    """Fail-closed: no client id means no attribution means hands off."""

    part = cancel_module.partition([_Order(symbol="BTCUSDT", client_order_id="")])
    assert part.mine == ()
    assert len(part.foreign) == 1


# ---------------------------------------------------------------------------
# The cancel gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancels_only_b0x_orders_and_leaves_foreign_untouched() -> None:
    client = _FakeClient(
        [_MINE_BTC, _FOREIGN_SAME_SYMBOL, _MINE_ETH, _FOREIGN_OTHER_SYMBOL]
    )

    outcome = await cancel_module.cancel_own(client, confirm=True)

    cancelled_ids = {call["client_order_id"] for call in client.cancel_calls}
    assert cancelled_ids == {_MINE_BTC.client_order_id, _MINE_ETH.client_order_id}
    # The foreign BTCUSDT order shares a symbol with one we cancelled; proving
    # it survived is the point of this test.
    assert _FOREIGN_SAME_SYMBOL.client_order_id not in cancelled_ids
    assert _FOREIGN_OTHER_SYMBOL.client_order_id not in cancelled_ids
    assert {o.client_order_id for o in outcome.partition.foreign} == {
        _FOREIGN_SAME_SYMBOL.client_order_id,
        _FOREIGN_OTHER_SYMBOL.client_order_id,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scan_is_account_wide_not_symbol_scoped() -> None:
    """A symbol-scoped scan would miss the XRPUSDT order entirely."""

    client = _FakeClient([_MINE_BTC, _FOREIGN_OTHER_SYMBOL])

    found = await cancel_module.scan(client)

    assert found.total == 2
    assert any(o.symbol == "XRPUSDT" for o in found.foreign)
    assert client.symbol_scoped_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_dispatches_no_mutation() -> None:
    client = _FakeClient([_MINE_BTC, _MINE_ETH])

    outcome = await cancel_module.cancel_own(client)

    assert all(call["confirm"] is False for call in client.cancel_calls)
    assert all(row["dispatched"] is False for row in outcome.cancelled)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_with_only_foreign_orders_cancels_nothing() -> None:
    client = _FakeClient([_FOREIGN_SAME_SYMBOL, _FOREIGN_OTHER_SYMBOL])

    outcome = await cancel_module.cancel_own(client, confirm=True)

    assert client.cancel_calls == []
    assert outcome.cancelled == ()
    assert len(outcome.partition.foreign) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_site_recheck_blocks_a_widened_partition() -> None:
    """Defense in depth: if partition regresses, the dispatcher still refuses.

    Simulates the exact refactor risk — someone loosens the filter — by
    handing ``cancel_own`` a partition that wrongly includes a foreign order.
    """

    client = _FakeClient([_FOREIGN_SAME_SYMBOL])
    bad = cancel_module.OrderPartition(mine=(_FOREIGN_SAME_SYMBOL,), foreign=())

    async def _widened(_client: Any) -> cancel_module.OrderPartition:
        return bad

    original = cancel_module.scan
    cancel_module.scan = _widened  # type: ignore[assignment]
    try:
        with pytest.raises(cancel_module.ForeignOrderCancelAttempt):
            await cancel_module.cancel_own(client, confirm=True)
    finally:
        cancel_module.scan = original  # type: ignore[assignment]

    assert client.cancel_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outcome_json_names_untouched_foreign_orders() -> None:
    """The report must make 'what I did not touch' visible, not just implied."""

    client = _FakeClient([_MINE_BTC, _FOREIGN_SAME_SYMBOL])

    outcome = await cancel_module.cancel_own(client, confirm=True)
    payload = outcome.to_json()

    assert [row["client_order_id"] for row in payload["not_mine_untouched"]] == [
        _FOREIGN_SAME_SYMBOL.client_order_id
    ]
    assert payload["open_orders"]["mine"][0]["attribution"] == "b0x"
    assert payload["open_orders"]["foreign"][0]["attribution"] == "foreign"
