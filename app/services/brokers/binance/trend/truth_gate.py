"""Read-only account truth gate for the first TREND-TSMOM-v1 runner start.

The trend lane needs a flat, attributable account before its first run: every
position it later opens must be its own. The gate passes only when the shared
Futures Demo account is one-way, every universe symbol is preconfigured
isolated 1x, and the account positions agree with this lane's durable state
(no foreign exposure, no unresolved intent, no ``holding`` row without a
matching position).

Only reads happen here: broker GETs through the trend client, SELECT-only state
reads. No order, leverage, margin, cancel or ledger write is reachable;
``tests/services/brokers/binance/trend/test_truth_gate.py`` pins the call set
with an AST test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .constants import LOCKED_LIMITS


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
    def assert_demo_identity(self) -> None: ...
    async def get_position_mode(self) -> Any: ...
    async def assert_symbol_isolated_1x(self, symbol: str) -> None: ...
    async def get_all_positions(self) -> Any: ...


class GateState(Protocol):
    async def list_active_signals(self) -> Any: ...
    async def list_unresolved_intents(self) -> Any: ...


def _failed(name: str, exc: Exception) -> GateCheck:
    """Class name only: a collaborator's message may carry a signed URL."""
    return GateCheck(name, False, f"read failed: {type(exc).__name__}")


async def _host(client: GateClient) -> GateCheck:
    name = "demo_host_identity"
    try:
        client.assert_demo_identity()
    except Exception as exc:  # noqa: BLE001 - an off-origin client is a FAIL
        return _failed(name, exc)
    return GateCheck(name, True, "exact https://demo-fapi.binance.com")


async def _mode(client: GateClient) -> GateCheck:
    name = "one_way_position_mode"
    try:
        mode = await client.get_position_mode()
    except Exception as exc:  # noqa: BLE001
        return _failed(name, exc)
    if mode.is_hedge_mode is not False:
        return GateCheck(name, False, "hedge mode or unreadable mode")
    return GateCheck(name, True, "one-way")


async def _isolated(client: GateClient) -> GateCheck:
    name = "account_isolated_1x"
    bad: list[str] = []
    for symbol in LOCKED_LIMITS.universe:
        try:
            await client.assert_symbol_isolated_1x(symbol)
        except Exception:  # noqa: BLE001 - any unreadable symbol is not proven
            bad.append(symbol)
    if bad:
        return GateCheck(name, False, "not isolated 1x BOTH: " + ",".join(bad))
    return GateCheck(name, True, "symbols=all")


async def _flatness(client: GateClient, state: GateState) -> GateCheck:
    name = "account_flat_vs_trend_state"
    try:
        signals = await state.list_active_signals()
        intents = await state.list_unresolved_intents()
        positions = await client.get_all_positions()
    except Exception as exc:  # noqa: BLE001 - an unreadable side proves nothing
        return _failed(name, exc)
    if intents:
        return GateCheck(name, False, f"unresolved_intents={len(intents)}")
    held = {s.symbol: s for s in signals if s.state == "holding"}
    pending = [s for s in signals if s.state != "holding"]
    if pending:
        states = ",".join(sorted({s.state for s in pending}))
        return GateCheck(name, False, f"active_signals={len(pending)} states[{states}]")
    nonzero = {p.symbol: p.position_amt for p in positions if p.position_amt != 0}
    foreign = sorted(set(nonzero) - set(held))
    if foreign:
        return GateCheck(name, False, "foreign positions: " + ",".join(foreign))
    expected = {
        symbol: (
            signal.remaining_qty if signal.side == "BUY" else -signal.remaining_qty
        )
        for symbol, signal in held.items()
    }
    mismatched = sorted(
        symbol
        for symbol in set(expected) | set(nonzero)
        if nonzero.get(symbol, 0) != expected.get(symbol, 0)
    )
    if mismatched:
        detail = ",".join(
            f"{s}={nonzero.get(s, 0)}!={expected.get(s, 0)}" for s in mismatched
        )
        return GateCheck(name, False, "position/state mismatch: " + detail)
    return GateCheck(name, True, f"flat_or_owned={len(held)}")


async def run_truth_gate(*, client: GateClient, state: GateState) -> TruthGateReport:
    """Every check runs even when an earlier one failed, so one run shows all."""
    return TruthGateReport(
        (
            await _host(client),
            await _mode(client),
            await _isolated(client),
            await _flatness(client, state),
        )
    )
