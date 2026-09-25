"""Fake-broker checks for every #728 live sell guard boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.protected_quantity_service import (
    ProtectionBlock,
    ProtectionDecision,
    normalize_protection_key,
)

pytestmark = pytest.mark.unit


def _blocked_decision(
    *,
    scope: str = "kis_live",
    market: str = "kr",
    symbol: str = "005930",
    code: str = "protected_quantity_exceeded",
) -> ProtectionDecision:
    key = normalize_protection_key(
        account_scope=scope,
        market=market,
        symbol=symbol,
    )
    block = ProtectionBlock(
        error_code=code,
        key=key,
        protected_quantity=Decimal("60"),
        broker_sellable=Decimal("100"),
        headroom=Decimal("40"),
        quantity=Decimal("41"),
    )
    return ProtectionDecision(False, "covered", Decimal("40"), block=block)


@dataclass
class _Lease:
    decisions: list[ProtectionDecision]
    active: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)
    release_calls: int = 0

    async def evaluate(self, **kwargs: Any) -> ProtectionDecision:
        self.calls.append(kwargs)
        return self.decisions.pop(0)

    async def release(self) -> None:
        self.release_calls += 1


def _execute_kwargs() -> dict[str, Any]:
    return {
        "normalized_symbol": "005930",
        "side": "sell",
        "order_type": "limit",
        "order_quantity": 41,
        "price": 70000,
        "market_type": "equity_kr",
        "current_price": 70000,
        "avg_price": 60000,
        "dry_run_result": {"price": 70000, "quantity": 41},
        "order_amount": 2_870_000,
        "reason": "fake guard test",
        "exit_reason": None,
        "thesis": None,
        "strategy": None,
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": False,
        "idempotency_key": "guard-wiring-key",
    }


@pytest.mark.asyncio
async def test_g1_blocks_before_intent_reservation_and_uses_raw_sellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import order_execution as execution

    lease = _Lease([_blocked_decision()])
    reserve = AsyncMock(return_value=1)
    broker_send = AsyncMock(return_value={"rt_cd": "0", "odno": "guard-test"})
    monkeypatch.setattr(
        execution,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                # C1's tactical field is deliberately lower. G1 must not
                # subtract P twice by reading it.
                "quantity": 40,
                "broker_sellable_quantity": 100,
                "total_quantity": 100,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(execution, "_execute_order", broker_send)
    monkeypatch.setattr(execution.OrderSendIntentService, "reserve", reserve)
    monkeypatch.setattr(execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(
        kis_live_ledger,
        "_record_kis_live_order",
        AsyncMock(return_value={"success": True}),
    )

    result = await execution._execute_and_record(**_execute_kwargs())

    assert lease.calls == [
        {
            "quantity": 41,
            "kind": "new",
            "fresh_broker_sellable": 100,
            "fresh_broker_held": 100,
            "sellable_observed": True,
        }
    ]
    assert lease.release_calls == 1
    reserve.assert_not_awaited()
    broker_send.assert_not_awaited()
    assert result.get("error_code") == "protected_quantity_exceeded", result


@pytest.mark.asyncio
async def test_g1_q15_policy_lookup_failure_never_reaches_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import order_execution as execution
    from app.services.protected_quantity_service import ProtectionStateUnavailable

    broker_send = AsyncMock()
    monkeypatch.setattr(
        execution,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=ProtectionStateUnavailable("test outage")),
    )
    monkeypatch.setattr(execution, "_execute_order", broker_send)

    result = await execution._execute_and_record(**_execute_kwargs())

    assert result["error_code"] == "protection_state_unavailable"
    broker_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_g2_g3_shared_q15_lookup_failure_stops_before_toss_broker_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.protected_quantity_service import ProtectionStateUnavailable

    find_holding = AsyncMock()
    monkeypatch.setattr(
        toss,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=ProtectionStateUnavailable("test outage")),
    )
    monkeypatch.setattr(toss, "_find_holding", find_holding)

    lease, error = await toss._prepare_toss_sell_protection(
        SimpleNamespace(),
        market="kr",
        symbol="005930",
        quantity=Decimal("1"),
        fresh_sellable_evidence={"fresh_sellable_quantity": "100"},
        order_amount_present=False,
        kind="amend_uncapped",
        base={"source": "toss"},
    )

    assert lease is None
    assert error is not None
    assert error["error_code"] == "protection_state_unavailable"
    find_holding.assert_not_awaited()


@pytest.mark.asyncio
async def test_g2_protected_toss_sell_blocks_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import orders_toss_variants as toss

    lease = _Lease(
        [
            _blocked_decision(
                scope="toss_live",
                market="kr",
                symbol="005930",
            )
        ]
    )
    client = SimpleNamespace(place_order=AsyncMock())

    @asynccontextmanager
    async def fake_client_context():
        yield client

    monkeypatch.setattr(toss, "_entry_guard", lambda *_: None)
    monkeypatch.setattr(toss, "_client_context", fake_client_context)
    monkeypatch.setattr(
        toss,
        "_snap_kr_limit_price",
        AsyncMock(return_value=(Decimal("70000"), None, {})),
    )
    monkeypatch.setattr(toss, "_live_mutation_disabled_error", lambda *_: None)
    monkeypatch.setattr(
        toss,
        "check_warnings_guard",
        AsyncMock(
            return_value=SimpleNamespace(ok=True, warnings=[], error_message=None)
        ),
    )
    monkeypatch.setattr(toss, "_opposite_pending_error", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_sell_loss_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_nxt_preflight_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        toss,
        "_fresh_sellable_preflight",
        AsyncMock(
            return_value=(
                {
                    "fresh_sellable_quantity": "100",
                    "sellable_quantity_source": "fake",
                },
                None,
            )
        ),
    )
    monkeypatch.setattr(
        toss,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        toss,
        "_find_holding",
        AsyncMock(return_value=SimpleNamespace(quantity=Decimal("100"))),
    )

    result = await toss._toss_place_order_impl(
        symbol="005930",
        side="sell",
        quantity="41",
        price="70000",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    client.place_order.assert_not_awaited()
    assert result.get("error_code") == "protected_quantity_exceeded", result
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g2_order_amount_is_unresolved_for_active_protected_toss_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import orders_toss_variants as toss

    lease = _Lease(
        [
            _blocked_decision(
                scope="toss_live",
                market="kr",
                symbol="005930",
                code="protected_quantity_unresolved",
            )
        ]
    )
    monkeypatch.setattr(
        toss,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    find_holding = AsyncMock()
    monkeypatch.setattr(toss, "_find_holding", find_holding)

    _, error = await toss._prepare_toss_sell_protection(
        SimpleNamespace(),
        market="kr",
        symbol="005930",
        quantity=None,
        fresh_sellable_evidence={"fresh_sellable_quantity": "100"},
        order_amount_present=True,
        kind="new",
        base={"source": "toss"},
    )

    assert error is not None
    assert error["error_code"] == "protected_quantity_unresolved"
    find_holding.assert_not_awaited()
    assert lease.calls[0]["quantity"] is None
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g2_order_amount_reaches_active_policy_before_legacy_shape_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real G2 caller must not short-circuit an active protected payload."""

    from app.mcp_server.tooling import orders_toss_variants as toss

    lease = _Lease(
        [
            _blocked_decision(
                scope="toss_live",
                market="kr",
                symbol="005930",
                code="protected_quantity_unresolved",
            )
        ]
    )
    client = SimpleNamespace(place_order=AsyncMock())

    @asynccontextmanager
    async def fake_client_context():
        yield client

    monkeypatch.setattr(toss, "_entry_guard", lambda *_: None)
    monkeypatch.setattr(toss, "_client_context", fake_client_context)
    monkeypatch.setattr(
        toss,
        "_snap_kr_limit_price",
        AsyncMock(return_value=(Decimal("70000"), None, {})),
    )
    monkeypatch.setattr(toss, "_live_mutation_disabled_error", lambda *_: None)
    monkeypatch.setattr(
        toss,
        "check_warnings_guard",
        AsyncMock(
            return_value=SimpleNamespace(ok=True, warnings=[], error_message=None)
        ),
    )
    monkeypatch.setattr(toss, "_opposite_pending_error", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_sell_loss_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_nxt_preflight_context", AsyncMock(return_value=None))
    fresh_preflight = AsyncMock(
        return_value=(
            {
                "fresh_sellable_quantity": "100",
                "sellable_quantity_source": "fake",
            },
            None,
        )
    )
    monkeypatch.setattr(toss, "_fresh_sellable_preflight", fresh_preflight)
    monkeypatch.setattr(toss, "protection_mode_for_scope", lambda *_: "shadow")
    monkeypatch.setattr(
        toss,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )

    result = await toss._toss_place_order_impl(
        symbol="005930",
        side="sell",
        quantity=None,
        order_amount="100000",
        price="70000",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["error_code"] == "protected_quantity_unresolved"
    fresh_preflight.assert_awaited_once()
    assert lease.calls[0]["quantity"] is None
    assert lease.release_calls == 1
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_g3_toss_modify_uses_uncapped_guard_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import orders_toss_variants as toss

    lease = _Lease(
        [
            _blocked_decision(
                scope="toss_live",
                market="kr",
                symbol="005930",
            )
        ]
    )
    client = SimpleNamespace(
        get_order=AsyncMock(
            return_value=SimpleNamespace(
                symbol="005930",
                side="sell",
                order_type="limit",
                quantity=Decimal("40"),
                price=Decimal("70000"),
                order_amount=None,
                time_in_force="DAY",
                currency="KRW",
            )
        ),
        modify_order=AsyncMock(),
    )

    @asynccontextmanager
    async def fake_client_context():
        yield client

    monkeypatch.setattr(toss, "_entry_guard", lambda *_: None)
    monkeypatch.setattr(toss, "_client_context", fake_client_context)
    monkeypatch.setattr(
        toss,
        "_snap_kr_limit_price",
        AsyncMock(return_value=(Decimal("71000"), None, {})),
    )
    monkeypatch.setattr(toss, "_live_mutation_disabled_error", lambda *_: None)
    monkeypatch.setattr(toss, "_sell_loss_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_nxt_preflight_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        toss,
        "_fresh_sellable_preflight",
        AsyncMock(return_value=({"fresh_sellable_quantity": "100"}, None)),
    )
    monkeypatch.setattr(
        toss,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        toss,
        "_find_holding",
        AsyncMock(return_value=SimpleNamespace(quantity=Decimal("100"))),
    )

    result = await toss.toss_modify_order(
        "order-1",
        new_price="71000",
        new_quantity="40",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    client.modify_order.assert_not_awaited()
    assert result.get("error_code") == "protected_quantity_exceeded", result
    assert lease.calls[0]["kind"] == "amend_uncapped"


@pytest.mark.asyncio
async def test_g4_upbit_post_cancel_block_withholds_reorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.brokers.upbit.orders as upbit_orders

    lease = _Lease(
        [
            ProtectionDecision(True, "covered", Decimal("40")),
            _blocked_decision(
                scope="upbit_live",
                market="crypto",
                symbol="KRW-BTC",
            ),
        ]
    )
    cancel = AsyncMock(return_value=[{"uuid": "order-1"}])
    place_sell = AsyncMock()
    monkeypatch.setattr(
        upbit_orders,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",
                "market": "KRW-BTC",
                "remaining_volume": "40",
            }
        ),
    )
    monkeypatch.setattr(
        upbit_orders,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        upbit_orders,
        "_fresh_upbit_sell_position",
        AsyncMock(side_effect=[(60, 100, True), (35, 75, True)]),
    )
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)
    monkeypatch.setattr(upbit_orders, "place_sell_order", place_sell)

    result = await upbit_orders.cancel_and_reorder(
        "order-1", new_price=56_000_000, new_quantity=40
    )

    cancel.assert_awaited_once_with(["order-1"])
    place_sell.assert_not_awaited()
    assert result["reorder_withheld"] is True
    assert result["error_code"] == "protected_quantity_exceeded"
    assert result["protection_phase"] == "post_cancel"
    assert [call["kind"] for call in lease.calls] == ["cancel_replace", "new"]
    assert lease.calls[1]["fresh_broker_sellable"] == 35
    assert lease.calls[1]["fresh_broker_held"] == 75
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g4_upbit_holds_protection_lease_through_reorder_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.brokers.upbit.orders as upbit_orders

    lease = _Lease(
        [
            ProtectionDecision(True, "covered", Decimal("40")),
            ProtectionDecision(True, "covered", Decimal("40")),
        ]
    )

    async def cancel_while_locked(order_ids: list[str]) -> list[dict[str, str]]:
        assert order_ids == ["order-1"]
        assert lease.release_calls == 0
        return [{"uuid": "order-1"}]

    async def place_while_locked(
        market: str, volume: str, price: str
    ) -> dict[str, str]:
        assert (market, volume, price) == ("KRW-BTC", "40.00000000", "56000000")
        assert lease.release_calls == 0
        return {"uuid": "replacement-1"}

    monkeypatch.setattr(
        upbit_orders,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",
                "market": "KRW-BTC",
                "remaining_volume": "40",
            }
        ),
    )
    monkeypatch.setattr(
        upbit_orders,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        upbit_orders,
        "_fresh_upbit_sell_position",
        AsyncMock(side_effect=[(60, 100, True), (100, 100, True)]),
    )
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel_while_locked)
    monkeypatch.setattr(upbit_orders, "place_sell_order", place_while_locked)

    result = await upbit_orders.cancel_and_reorder(
        "order-1", new_price=56_000_000, new_quantity=40
    )

    assert result["new_order"]["uuid"] == "replacement-1"
    assert [call["kind"] for call in lease.calls] == ["cancel_replace", "new"]
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g4_upbit_pre_cancel_block_never_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.brokers.upbit.orders as upbit_orders

    lease = _Lease(
        [
            _blocked_decision(
                scope="upbit_live",
                market="crypto",
                symbol="KRW-BTC",
            ),
            ProtectionDecision(True, "covered", Decimal("40")),
        ]
    )
    cancel = AsyncMock(return_value=[{"uuid": "order-1"}])
    place_sell = AsyncMock(return_value={"uuid": "replacement-1"})
    monkeypatch.setattr(
        upbit_orders,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",
                "market": "KRW-BTC",
                "remaining_volume": "40",
            }
        ),
    )
    monkeypatch.setattr(
        upbit_orders,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        upbit_orders,
        "_fresh_upbit_sell_position",
        AsyncMock(side_effect=[(60, 100, True), (100, 100, True)]),
    )
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)
    monkeypatch.setattr(upbit_orders, "place_sell_order", place_sell)

    result = await upbit_orders.cancel_and_reorder(
        "order-1", new_price=56_000_000, new_quantity=41
    )

    cancel.assert_not_awaited()
    assert result["error_code"] == "protected_quantity_exceeded"
    assert result["protection_phase"] == "pre_cancel"
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g4_q15_lookup_failure_stops_kis_amend_and_upbit_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.brokers.upbit.orders as upbit_orders
    from app.mcp_server.tooling import orders_modify_cancel as modify
    from app.services.protected_quantity_service import ProtectionStateUnavailable

    monkeypatch.setattr(
        modify,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=ProtectionStateUnavailable("test outage")),
    )
    lease, amend_error = await modify._prepare_kis_live_sell_modify_protection(
        normalized_symbol="005930",
        market_type="equity_kr",
        new_quantity=1,
    )
    assert lease is None
    assert amend_error is not None
    assert amend_error["error_code"] == "protection_state_unavailable"

    cancel = AsyncMock()
    monkeypatch.setattr(
        upbit_orders,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",
                "market": "KRW-BTC",
                "remaining_volume": "1",
            }
        ),
    )
    monkeypatch.setattr(
        upbit_orders,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=ProtectionStateUnavailable("test outage")),
    )
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)

    result = await upbit_orders.cancel_and_reorder(
        "order-1", new_price=56_000_000, new_quantity=1
    )

    assert result["error_code"] == "protection_state_unavailable"
    assert result["protection_phase"] == "pre_cancel"
    cancel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_type", "symbol", "market"),
    [
        ("equity_kr", "005930", "kr"),
        ("equity_us", "BRK-B", "us"),
    ],
)
async def test_g4_kis_amend_guard_uses_raw_fresh_holdings(
    monkeypatch: pytest.MonkeyPatch,
    market_type: str,
    symbol: str,
    market: str,
) -> None:
    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease = _Lease(
        [
            _blocked_decision(
                market=market,
                symbol="BRK.B" if market == "us" else symbol,
            )
        ]
    )
    prepare = AsyncMock(return_value=lease)
    monkeypatch.setattr(modify, "prepare_live_sell_lease", prepare)
    monkeypatch.setattr(
        modify,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "quantity": 40,
                "broker_sellable_quantity": 100,
                "total_quantity": 100,
                "sellable_observed": True,
            }
        ),
    )

    _, error = await modify._prepare_kis_live_sell_modify_protection(
        normalized_symbol=symbol,
        market_type=market_type,
        new_quantity=41,
    )

    assert error is not None
    assert error["error_code"] == "protected_quantity_exceeded"
    assert prepare.await_args.kwargs["market"] == market
    assert lease.calls[0]["kind"] == "amend_uncapped"
    assert lease.calls[0]["fresh_broker_sellable"] == 100
    assert lease.release_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_g4_kis_broker_amend_never_runs_after_protection_block(
    monkeypatch: pytest.MonkeyPatch,
    market_type: str,
) -> None:
    from app.mcp_server.tooling import orders_modify_cancel as modify

    protection_error = {
        "error": "Protected quantity floor blocks this sell modification.",
        "error_code": "protected_quantity_exceeded",
    }
    if market_type == "equity_kr":
        broker_modify = AsyncMock(return_value={"odno": "new-kr"})
        kis = SimpleNamespace(
            inquire_korea_orders=AsyncMock(
                return_value=[
                    {
                        "odno": "order-1",
                        "ord_unpr": "70000",
                        "ord_qty": "40",
                        "sll_buy_dvsn_cd": "01",
                    }
                ]
            ),
            modify_korea_order=broker_modify,
        )
        monkeypatch.setattr(modify, "_create_kis_client", lambda **_: kis)
        monkeypatch.setattr(
            modify,
            "_live_sell_reprice_floor_error",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            modify,
            "_kr_security_type_or_none",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(modify, "adjust_tick_size_kr", lambda price, *_: price)
        monkeypatch.setattr(
            modify,
            "_prepare_kis_live_sell_modify_protection",
            AsyncMock(return_value=(None, protection_error)),
        )
        result = await modify._modify_kis_domestic(
            "order-1",
            "005930",
            market_type,
            71_000,
            41,
            False,
        )
    else:
        broker_modify = AsyncMock(return_value={"odno": "new-us"})
        kis = SimpleNamespace(modify_overseas_order=broker_modify)
        monkeypatch.setattr(modify, "_create_kis_client", lambda **_: kis)
        monkeypatch.setattr(
            modify,
            "_find_us_open_order_by_id",
            AsyncMock(
                return_value=(
                    {
                        "ft_ord_unpr3": "100",
                        "ft_ord_qty": "40",
                        "sll_buy_dvsn_cd": "01",
                    },
                    "NASD",
                    ["NASD"],
                )
            ),
        )
        monkeypatch.setattr(
            modify,
            "_live_sell_reprice_floor_error",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            modify,
            "_prepare_kis_live_sell_modify_protection",
            AsyncMock(return_value=(None, protection_error)),
        )
        result = await modify._modify_kis_overseas(
            "order-1",
            "AAPL",
            market_type,
            101,
            41,
            False,
        )

    broker_modify.assert_not_awaited()
    assert result["error_code"] == "protected_quantity_exceeded"


@pytest.mark.asyncio
async def test_g5_blocks_one_legacy_fragment_before_direct_broker_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import kis_trading_service as legacy

    lease = _Lease([_blocked_decision()])
    send = AsyncMock()
    ops = SimpleNamespace(market="domestic", place_order=send)
    monkeypatch.setattr(
        legacy,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        legacy,
        "_legacy_fresh_kis_sell_position",
        AsyncMock(return_value=(100, 100, True)),
    )

    result = await legacy._place_legacy_guarded_sell_fragment(
        ops,
        SimpleNamespace(),
        "005930",
        41,
        70000,
        exchange_code=None,
    )

    send.assert_not_awaited()
    assert result["error_code"] == "protected_quantity_exceeded"
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_g5_q15_lookup_failure_never_reaches_direct_broker_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import kis_trading_service as legacy
    from app.services.protected_quantity_service import ProtectionStateUnavailable

    send = AsyncMock()
    monkeypatch.setattr(
        legacy,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=ProtectionStateUnavailable("test outage")),
    )

    result = await legacy._place_legacy_guarded_sell_fragment(
        SimpleNamespace(market="domestic", place_order=send),
        SimpleNamespace(),
        "005930",
        1,
        70_000,
        exchange_code=None,
    )

    assert result["error_code"] == "protection_state_unavailable"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_g5_rechecks_every_fragment_so_three_parts_cannot_double_spend_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import kis_trading_service as legacy

    allowed = ProtectionDecision(True, "covered", Decimal("40"))
    blocked = _blocked_decision()
    leases = [
        _Lease([allowed]),
        _Lease([allowed]),
        _Lease([blocked]),
    ]
    send = AsyncMock(return_value={"odno": "fragment"})
    ops = SimpleNamespace(market="domestic", place_order=send)
    fresh = AsyncMock(
        side_effect=[
            (Decimal("100"), Decimal("100"), True),
            (Decimal("80"), Decimal("100"), True),
            (Decimal("60"), Decimal("100"), True),
        ]
    )
    monkeypatch.setattr(
        legacy,
        "prepare_live_sell_lease",
        AsyncMock(side_effect=leases),
    )
    monkeypatch.setattr(legacy, "_legacy_fresh_kis_sell_position", fresh)

    results = []
    for price in (70_000, 71_000, 72_000):
        results.append(
            await legacy._place_legacy_guarded_sell_fragment(
                ops,
                SimpleNamespace(),
                "005930",
                20,
                price,
                exchange_code=None,
            )
        )

    assert [result.get("odno") for result in results[:2]] == ["fragment", "fragment"]
    assert results[2]["error_code"] == "protected_quantity_exceeded"
    assert fresh.await_count == 3
    assert send.await_count == 2
    assert [lease.release_calls for lease in leases] == [1, 1, 1]
