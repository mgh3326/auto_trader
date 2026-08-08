"""Binance Spot Demo sidecar — allowlist, ratio transfer, and the submit gates."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo.execution_client import SpotDemoDryRunResult
from scripts.b0x.crypto import sidecar
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import CRYPTO_SIDECAR_ENVELOPE, EnvelopeNotLocked

pytestmark = pytest.mark.unit

ENVELOPE = CRYPTO_SIDECAR_ENVELOPE


def _filters(
    *, step: str = "0.00001", tick: str = "0.01", min_notional: str = "5"
) -> sidecar.SymbolFilters:
    return sidecar.SymbolFilters(
        step_size=Decimal(step),
        tick_size=Decimal(tick),
        min_notional=Decimal(min_notional),
    )


def _buy(
    symbol: str = "KRW-BTC", *, ratio: str = "0.97", notional: str = "10"
) -> DerivedOrder:
    return DerivedOrder(
        sequence=0,
        symbol=symbol,
        side="buy",
        leg="buy_l1",
        price_ratio=Decimal(ratio),
        table_price=Decimal("97"),
        table_previous_close=Decimal("100"),
        notional=Decimal(notional),
        quantity_fraction=None,
        basis="A_buy_side.buy_l1.price",
        labels=(),
        detail={},
        order_key="abc123",
    )


def _fresh(
    *,
    quote_free: str = "1000",
    base: dict[str, tuple[Decimal, Decimal]] | None = None,
    open_orders: dict[str, list[Any]] | None = None,
    foreign_orders: tuple[str, ...] = (),
    foreign_assets: tuple[str, ...] = (),
) -> sidecar.FreshTruth:
    return sidecar.FreshTruth(
        quote_free=Decimal(quote_free),
        quote_locked=Decimal("0"),
        base_balances=base
        or {asset: (Decimal("0"), Decimal("0")) for asset in ("BTC", "ETH", "SOL")},
        open_orders=open_orders or {"BTCUSDT": [], "ETHUSDT": [], "SOLUSDT": []},
        foreign_open_orders=foreign_orders,
        foreign_base_assets=foreign_assets,
    )


# ---------------------------------------------------------------------------
# Allowlist + account map
# ---------------------------------------------------------------------------


def test_allowlist_is_exactly_the_three_authorized_symbols() -> None:
    assert set(sidecar.B0X_SIDECAR_SYMBOLS) == {"KRW-BTC", "KRW-ETH", "KRW-SOL"}
    assert set(sidecar.B0X_SIDECAR_SYMBOLS.values()) == {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    }


@pytest.mark.parametrize("symbol", ["DOGEUSDT", "XRPUSDT", "BTCUSDC", "btcusdt", ""])
def test_symbols_outside_the_allowlist_are_refused(symbol: str) -> None:
    with pytest.raises(sidecar.SidecarSymbolNotAllowed):
        sidecar.assert_symbol_allowed(symbol)


def test_rob845_profile_is_not_modified_or_widened() -> None:
    """The ROB-845 adapter is a different experiment; B0-X must not touch it."""

    from app.services.brokers.binance import paper_adapter

    assert paper_adapter._ALLOWED_SYMBOLS == frozenset({"BTCUSDT", "ETHUSDT"})
    assert paper_adapter._POLICY_VERSION == "rob845-binance-spot-demo-v1"
    assert "SOLUSDT" not in paper_adapter._POLICY_CANONICAL
    # Distinct policy identities — the two profiles must never collide.
    b0x = sidecar.build_policy(ENVELOPE)
    assert b0x.version != paper_adapter._POLICY_VERSION
    assert b0x.policy_hash != paper_adapter._POLICY_HASH


def test_sidecar_module_does_not_import_the_rob845_adapter() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(sidecar.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any("paper_adapter" in module for module in modules)
    assert not any("paper_cohort" in module for module in modules)


def test_policy_hash_changes_with_the_envelope() -> None:
    policy = sidecar.build_policy(ENVELOPE)
    assert policy.policy_hash == sidecar.build_policy(ENVELOPE).policy_hash
    widened = replace(
        ENVELOPE,
        per_order_notional=Decimal("100"),
        per_symbol_total_notional=Decimal("500"),
    )
    with pytest.raises(EnvelopeNotLocked):
        sidecar.build_policy(widened)


# ---------------------------------------------------------------------------
# Enable gate + host
# ---------------------------------------------------------------------------


def test_sidecar_is_default_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("B0X_SIDECAR_ENABLED", raising=False)
    with pytest.raises(sidecar.SidecarDisabled):
        sidecar.assert_sidecar_enabled()


@pytest.mark.parametrize("value", ["false", "0", "no", "", "  ", "maybe"])
def test_non_truthy_values_keep_it_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", value)
    with pytest.raises(sidecar.SidecarDisabled):
        sidecar.assert_sidecar_enabled()


def test_enabled_gate_opens_only_on_explicit_truthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    sidecar.assert_sidecar_enabled()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com",
        "https://testnet.binance.vision",
        "https://demo-fapi.binance.com",
        "https://demo-api.binance.com.evil.example",
    ],
)
def test_non_demo_hosts_are_blocked(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", url)
    with pytest.raises(BinanceLiveHostBlocked):
        sidecar.base_url()


def test_default_base_url_is_the_demo_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_SPOT_DEMO_BASE_URL", raising=False)
    assert sidecar.base_url() == "https://demo-api.binance.com"


# ---------------------------------------------------------------------------
# Ratio transfer + tick alignment
# ---------------------------------------------------------------------------


def test_buy_prices_round_down_and_sell_prices_round_up() -> None:
    tick = Decimal("0.5")
    assert sidecar.align_price(Decimal("100.7"), tick_size=tick, side="buy") == Decimal(
        "100.5"
    )
    assert sidecar.align_price(
        Decimal("100.7"), tick_size=tick, side="sell"
    ) == Decimal("101.0")


def test_ratio_transfer_applies_the_table_ratio_to_the_venue_price() -> None:
    planned, blocked = sidecar.plan_orders(
        (_buy(ratio="0.97"),),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters()},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    assert blocked == []
    assert planned[0].price == Decimal("970.00")
    assert planned[0].symbol == "BTCUSDT"
    assert planned[0].table_symbol == "KRW-BTC"


def test_client_order_id_is_deterministic_and_prefixed() -> None:
    order = _buy()
    first = sidecar.client_order_id_for(order.order_key)
    assert first == sidecar.client_order_id_for(order.order_key)
    assert first.startswith(sidecar.CLIENT_ORDER_ID_PREFIX)
    assert len(first) <= 36  # Binance clientOrderId bound


def test_notional_never_exceeds_the_per_order_cap() -> None:
    planned, _ = sidecar.plan_orders(
        (_buy(notional="10"),),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters()},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    assert planned[0].notional <= ENVELOPE.per_order_notional


def test_lot_size_floor_below_min_notional_blocks_rather_than_rounding_up() -> None:
    planned, blocked = sidecar.plan_orders(
        (_buy(notional="10"),),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters(step="1", min_notional="5")},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    assert planned == []
    assert blocked[0].reason == "sizing_blocked"


def test_missing_market_data_blocks_the_order() -> None:
    planned, blocked = sidecar.plan_orders(
        (_buy(),), envelope=ENVELOPE, filters={}, reference_prices={}
    )
    assert planned == []
    assert blocked[0].reason == "market_data_unavailable"


def test_symbols_outside_the_lane_are_dropped_not_planned() -> None:
    planned, blocked = sidecar.plan_orders(
        (_buy(symbol="KRW-DOGE"),),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters()},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    assert planned == [] and blocked == []


def test_sell_sizes_from_the_venue_balance_not_the_table() -> None:
    sell = DerivedOrder(
        sequence=0,
        symbol="KRW-BTC",
        side="sell",
        leg="sell_r1",
        price_ratio=Decimal("1.05"),
        table_price=Decimal("105"),
        table_previous_close=Decimal("100"),
        notional=None,
        quantity_fraction=Decimal("0.5"),
        basis="B_sell_side.sell_r1",
        labels=("SELL_SIDE_MODEL_MISMATCH",),
        detail={},
        order_key="sellkey",
    )
    planned, blocked = sidecar.plan_orders(
        (sell,),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters(step="0.001", min_notional="1")},
        reference_prices={"BTCUSDT": Decimal("1000")},
        base_balances={"BTC": (Decimal("0.02"), Decimal("0"))},
    )
    assert blocked == []
    assert planned[0].qty == Decimal("0.010")  # half of 0.02, step-floored
    assert planned[0].side == "sell"


def test_plan_orders_refuses_a_widened_envelope() -> None:
    widened = replace(
        ENVELOPE,
        per_order_notional=Decimal("100"),
        per_symbol_total_notional=Decimal("500"),
    )
    with pytest.raises(EnvelopeNotLocked):
        sidecar.plan_orders(
            (_buy(),),
            envelope=widened,
            filters={"BTCUSDT": _filters()},
            reference_prices={"BTCUSDT": Decimal("1000")},
        )


# ---------------------------------------------------------------------------
# Fresh truth + submission gates
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_order(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        if not kwargs.get("confirm"):
            return SpotDemoDryRunResult(
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                order_type=kwargs["order_type"],
                qty=kwargs["qty"],
                client_order_id=kwargs["client_order_id"],
            )
        raise AssertionError("test must not dispatch a confirmed order")


@pytest.mark.asyncio
async def test_dry_run_dispatches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    client = _FakeClient()
    planned, _ = sidecar.plan_orders(
        (_buy(),),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters()},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    results = await sidecar.submit_planned(
        client, planned, envelope=ENVELOPE, fresh_truth=_fresh(), confirm=False
    )
    assert results[0]["dispatched"] is False
    assert client.submitted[0]["confirm"] is False
    assert client.submitted[0]["order_type"] == "LIMIT"
    assert client.submitted[0]["time_in_force"] == "GTC"


@pytest.mark.asyncio
async def test_disabled_lane_cannot_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("B0X_SIDECAR_ENABLED", raising=False)
    with pytest.raises(sidecar.SidecarDisabled):
        await sidecar.submit_planned(
            _FakeClient(), [], envelope=ENVELOPE, fresh_truth=_fresh(), confirm=False
        )


@pytest.mark.asyncio
async def test_foreign_open_orders_block_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    fresh = _fresh(foreign_orders=("BTCUSDT:someone-elses-order",))
    assert fresh.contaminated
    with pytest.raises(sidecar.SidecarContaminated):
        await sidecar.submit_planned(
            _FakeClient(), [], envelope=ENVELOPE, fresh_truth=fresh, confirm=False
        )


@pytest.mark.asyncio
async def test_foreign_balances_block_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    fresh = _fresh(foreign_assets=("BTC",))
    with pytest.raises(sidecar.SidecarContaminated):
        await sidecar.submit_planned(
            _FakeClient(), [], envelope=ENVELOPE, fresh_truth=fresh, confirm=False
        )


@pytest.mark.asyncio
async def test_submission_rechecks_the_cap_even_for_a_hand_built_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: plan_orders is not the only thing standing in the way."""

    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    oversized = sidecar.SidecarPlannedOrder(
        order_key="k",
        client_order_id="b0xc-k",
        table_symbol="KRW-BTC",
        symbol="BTCUSDT",
        side="buy",
        leg="buy_l1",
        price=Decimal("1000"),
        qty=Decimal("1"),
        notional=Decimal("1000"),
        reference_price=Decimal("1000"),
        price_ratio=Decimal("1"),
    )
    with pytest.raises(AssertionError):
        await sidecar.submit_planned(
            _FakeClient(),
            [oversized],
            envelope=ENVELOPE,
            fresh_truth=_fresh(),
            confirm=False,
        )


def test_fresh_truth_status_report_hides_values() -> None:
    fresh = _fresh(
        quote_free="12345.67",
        base={
            "BTC": (Decimal("0.5"), Decimal("0")),
            "ETH": (Decimal("0"), Decimal("0")),
            "SOL": (Decimal("0"), Decimal("0")),
        },
    )
    status = fresh.status_only()
    rendered = str(status)
    assert "12345" not in rendered
    assert "0.5" not in rendered
    assert status["quote_balance_present"] is True
    assert status["base_assets_with_nonzero_balance"] == ["BTC"]


def test_flat_account_is_recognized() -> None:
    assert _fresh().flat is True
    assert _fresh(base={"BTC": (Decimal("1"), Decimal("0"))}).flat is False


# ---------------------------------------------------------------------------
# Contamination judgment — contract v1.2 §8 dust rule.
#
# The whole rule in one line: "too small to sell" is excused, "small" is not.
# ---------------------------------------------------------------------------


class _FakeBalance:
    def __init__(self, free: str, locked: str = "0") -> None:
        self.free = Decimal(free)
        self.locked = Decimal(locked)


class _FakeOpenOrders:
    orders: list[Any] = []


class _ReadOnlyClient:
    """Read-only stand-in. Any mutation attempt fails the test loudly."""

    def __init__(self, balances: dict[str, str]) -> None:
        self._balances = balances

    async def get_asset_balance(self, *, asset: str) -> _FakeBalance:
        return _FakeBalance(self._balances.get(asset, "0"))

    async def get_open_orders(self, *, symbol: str) -> _FakeOpenOrders:
        return _FakeOpenOrders()

    async def submit_order(self, **kwargs: Any) -> None:
        raise AssertionError("read_fresh_truth must never submit an order")


# BTCUSDT/SOLUSDT venue reality as measured by X-S on 2026-08-08.
_VENUE_FILTERS = {
    "BTCUSDT": sidecar.SymbolFilters(
        step_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("5"),
        min_qty=Decimal("0.00001"),
    ),
    "ETHUSDT": sidecar.SymbolFilters(
        step_size=Decimal("0.0001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("5"),
        min_qty=Decimal("0.0001"),
    ),
    "SOLUSDT": sidecar.SymbolFilters(
        step_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("5"),
        min_qty=Decimal("0.001"),
    ),
}


async def _truth(balances: dict[str, str]) -> sidecar.FreshTruth:
    return await sidecar.read_fresh_truth(
        _ReadOnlyClient(balances), filters=_VENUE_FILTERS
    )


@pytest.mark.asyncio
async def test_mutant_1_sellable_balance_under_min_notional_is_still_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The load-bearing case. A balance that clears minQty but is worth far
    less than MIN_NOTIONAL (5 USDT) is foreign inventory and must block.

    If this ever passes, the gate has been widened to a notional threshold and
    foreign holdings the size of the lane's own 10 USDT cap would walk through.
    """

    # 0.00009 BTC ~= 5.6 USDT at the price X-S measured... but sizing does not
    # matter: it is 9 steps of minQty, so it is sellable, so it is foreign.
    fresh = await _truth({"BTC": "0.00009"})
    assert fresh.foreign_base_assets == ("BTC",)
    assert fresh.contaminated is True

    # And the pathological version: exactly one minQty unit, worth ~0.6 USDT —
    # an order of magnitude below MIN_NOTIONAL, still contamination.
    one_unit = await _truth({"BTC": "0.00001"})
    assert one_unit.foreign_base_assets == ("BTC",)
    assert one_unit.contaminated is True

    # ...and it still blocks submission at the last line before the venue.
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    with pytest.raises(sidecar.SidecarContaminated):
        await sidecar.submit_planned(
            _ReadOnlyClient({}), [], envelope=ENVELOPE, fresh_truth=fresh, confirm=False
        )


@pytest.mark.asyncio
async def test_mutant_2_unsellable_dust_is_not_contamination() -> None:
    """The X-S deadlock: residue that floors to zero cannot be liquidated, so
    treating it as contamination made ``SIDECAR_ARMED=YES`` unreachable."""

    fresh = await _truth({"BTC": "0.00000972", "SOL": "0.00094600"})
    assert fresh.foreign_base_assets == ()
    assert fresh.contaminated is False

    # Not hidden, though — the dust stays visible in the observation record.
    assert fresh.status_only()["base_assets_with_nonzero_balance"] == ["BTC", "SOL"]


@pytest.mark.asyncio
async def test_mutant_3_judgment_does_not_consult_min_notional() -> None:
    """Kill the notional-based variant: with MIN_NOTIONAL mutated to absurd
    values in both directions, the verdict must not move. Only LOT_SIZE counts.
    """

    sellable = {"BTC": "0.00009"}
    dust = {"BTC": "0.00000972"}

    for min_notional in ("0", "5", "1000000"):
        mutated = {
            symbol: replace(filters, min_notional=Decimal(min_notional))
            for symbol, filters in _VENUE_FILTERS.items()
        }
        assert (
            await sidecar.read_fresh_truth(_ReadOnlyClient(sellable), filters=mutated)
        ).contaminated is True, (
            f"min_notional={min_notional} changed a sellable verdict"
        )
        assert (
            await sidecar.read_fresh_truth(_ReadOnlyClient(dust), filters=mutated)
        ).contaminated is False, f"min_notional={min_notional} changed a dust verdict"


@pytest.mark.asyncio
async def test_locked_balance_counts_toward_the_judgment() -> None:
    """Inventory parked in someone else's resting order is still inventory."""

    assert (await _truth({})).contaminated is False

    class _Locked(_ReadOnlyClient):
        async def get_asset_balance(self, *, asset: str) -> _FakeBalance:
            if asset == "SOL":
                return _FakeBalance("0", "0.5")
            return _FakeBalance("0")

    locked = await sidecar.read_fresh_truth(_Locked({}), filters=_VENUE_FILTERS)
    assert locked.foreign_base_assets == ("SOL",)


@pytest.mark.asyncio
async def test_missing_filters_fail_closed_to_the_old_presence_test() -> None:
    """If sellability is unknowable, fall back to the stricter rule."""

    fresh = await sidecar.read_fresh_truth(
        _ReadOnlyClient({"BTC": "0.00000972"}), filters={}
    )
    assert fresh.foreign_base_assets == ("BTC",)
    assert fresh.contaminated is True


def test_sellable_qty_requires_min_qty_not_just_a_whole_step() -> None:
    """On all three authorized symbols ``stepSize == minQty``, so the two
    thresholds coincide and a step-only implementation looks identical. Pin the
    intended rule on a venue where they differ: a balance that is a whole
    number of steps but below minQty is still refused by the matching engine,
    so it is unsellable — dust.
    """

    wide = sidecar.SymbolFilters(
        step_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("5"),
        min_qty=Decimal("0.01"),
    )
    assert sidecar.sellable_qty(Decimal("0.005"), filters=wide) == Decimal("0")
    assert sidecar.sellable_qty(Decimal("0.01"), filters=wide) == Decimal("0.01")


def test_sellable_qty_floors_and_never_rounds_up() -> None:
    filters = _VENUE_FILTERS["SOLUSDT"]
    assert sidecar.sellable_qty(Decimal("0.0009"), filters=filters) == Decimal("0")
    assert sidecar.sellable_qty(Decimal("0.001"), filters=filters) == Decimal("0.001")
    assert sidecar.sellable_qty(Decimal("0.0019"), filters=filters) == Decimal("0.001")
    assert sidecar.sellable_qty(Decimal("0"), filters=filters) == Decimal("0")


def test_per_order_cap_bounds_buys_only_not_exits() -> None:
    """The §4 per-order cap bounds capital deployment. Capping a sell would
    strand inventory the lane is trying to reduce."""

    big_sell = DerivedOrder(
        sequence=0,
        symbol="KRW-BTC",
        side="sell",
        leg="sell_r1",
        price_ratio=Decimal("1.05"),
        table_price=Decimal("105"),
        table_previous_close=Decimal("100"),
        notional=None,
        quantity_fraction=Decimal("0.5"),
        basis="B_sell_side.sell_r1",
        labels=("SELL_SIDE_MODEL_MISMATCH",),
        detail={},
        order_key="bigsell",
    )
    planned, blocked = sidecar.plan_orders(
        (big_sell,),
        envelope=ENVELOPE,
        filters={"BTCUSDT": _filters(step="0.001", min_notional="1")},
        reference_prices={"BTCUSDT": Decimal("1000")},
        base_balances={"BTC": (Decimal("1"), Decimal("0"))},
    )
    assert blocked == []
    # 0.5 BTC at ~1050 USDT = far above the 10 USDT buy cap, and rightly allowed.
    assert planned[0].notional > ENVELOPE.per_order_notional

    oversized_buy = _buy(notional="10")
    _, buy_blocked = sidecar.plan_orders(
        (oversized_buy,),
        envelope=replace(
            ENVELOPE,
            per_order_notional=ENVELOPE.per_order_notional,
        ),
        filters={"BTCUSDT": _filters()},
        reference_prices={"BTCUSDT": Decimal("1000")},
    )
    assert buy_blocked == []
