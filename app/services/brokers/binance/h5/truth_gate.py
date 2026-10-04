"""Read-only account truth gate for the first H5 runner start.

The H5 contract requires a read-only check of positions, open orders and
ledger attribution before the first run. The gate passes only when the shared
Futures Demo account is flat and unattributed work is absent, so everything the
runner later opens is attributable to H5 alone.

Only reads happen here: broker GETs through the H5 client, SELECT-only state and
ledger reads. No order, leverage, margin, cancel or ledger write is reachable;
``tests/services/brokers/binance/h5/test_truth_gate.py`` pins the call set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .client import H5BrokerTruthUnavailable
from .state import H5StateBlocked
from .strategy import UNIVERSE


@dataclass(frozen=True)
class GateCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class TruthGateReport:
    checks: tuple[GateCheck, ...]

    @property
    def verdict(self) -> str:
        return "PASS" if self.checks and all(c.ok for c in self.checks) else "FAIL"


class GateClient(Protocol):
    async def read_account(self) -> Any: ...
    async def get_position_mode(self) -> Any: ...
    async def get_all_positions(self) -> Any: ...
    async def get_all_open_orders(self) -> Any: ...


class GateState(Protocol):
    async def list_active_signals(self) -> Any: ...
    async def list_unresolved_intents(self) -> Any: ...


class GateLedger(Protocol):
    async def count_open_lifecycles(self) -> Any: ...
    async def status_distribution(self) -> Any: ...


def _failed(name: str, exc: Exception) -> GateCheck:
    """H5's own refusals carry fixed, secret-free messages; anything else is class only."""
    detail = f"read failed: {type(exc).__name__}"
    if isinstance(exc, H5BrokerTruthUnavailable | H5StateBlocked):
        detail += f": {exc}"
    return GateCheck(name, False, detail)


async def _account(client: GateClient) -> GateCheck:
    name = "account_isolated_1x"
    try:
        account = await client.read_account()
    except Exception as exc:  # noqa: BLE001 - an unreadable account is a FAIL
        return _failed(name, exc)
    configured = account.per_symbol_isolated_1x
    bad = sorted(s for s in UNIVERSE if configured.get(s) is not True)
    if bad:
        return GateCheck(name, False, "not isolated 1x BOTH: " + ",".join(bad))
    return GateCheck(name, True, f"nav_usdt={account.nav_usdt} symbols=all")


async def _mode(client: GateClient) -> GateCheck:
    name = "one_way_position_mode"
    try:
        mode = await client.get_position_mode()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    if mode.is_hedge_mode is not False:
        return GateCheck(name, False, "hedge mode or unreadable mode")
    return GateCheck(name, True, "one-way")


async def _positions(client: GateClient) -> GateCheck:
    name = "positions_flat"
    try:
        positions = await client.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    open_symbols = sorted(p.symbol for p in positions if p.position_amt != 0)
    if open_symbols:
        return GateCheck(name, False, "non-flat: " + ",".join(open_symbols))
    return GateCheck(name, True, f"rows={len(positions)} all flat")


async def _orders(client: GateClient) -> GateCheck:
    name = "no_open_orders"
    try:
        orders = await client.get_all_open_orders()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    if orders.orders:
        return GateCheck(name, False, f"open orders: {len(orders.orders)}")
    return GateCheck(name, True, "none")


async def _h5_state(state: GateState) -> GateCheck:
    name = "h5_state_empty"
    try:
        signals = await state.list_active_signals()
        intents = await state.list_unresolved_intents()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    if signals or intents:
        return GateCheck(
            name,
            False,
            f"active_signals={len(signals)} unresolved_intents={len(intents)}",
        )
    return GateCheck(name, True, "active_signals=0 unresolved_intents=0")


async def _ledger(ledger: GateLedger) -> GateCheck:
    name = "demo_ledger_no_open_roots"
    try:
        open_roots = await ledger.count_open_lifecycles()
        distribution = await ledger.status_distribution()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    states = ",".join(f"{k}={v}" for k, v in sorted(dict(distribution).items()))
    if open_roots != 0:
        return GateCheck(name, False, f"open_roots={open_roots} states[{states}]")
    return GateCheck(name, True, f"open_roots=0 states[{states}]")


async def run_truth_gate(
    *, client: GateClient, state: GateState, ledger: GateLedger
) -> TruthGateReport:
    """Every check runs even when an earlier one failed, so one run shows all."""
    return TruthGateReport(
        (
            await _account(client),
            await _mode(client),
            await _positions(client),
            await _orders(client),
            await _h5_state(state),
            await _ledger(ledger),
        )
    )
