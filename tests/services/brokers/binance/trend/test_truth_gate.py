"""TREND read-only truth gate: verdicts, failure isolation and the AST proof."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.brokers.binance.trend import truth_gate
from app.services.brokers.binance.trend.constants import LOCKED_LIMITS
from app.services.brokers.binance.trend.truth_gate import run_truth_gate
from scripts import binance_trend_truth_gate as gate_cli

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE = REPO_ROOT / "app/services/brokers/binance/trend/truth_gate.py"
SCRIPT = REPO_ROOT / "scripts/binance_trend_truth_gate.py"
UNIVERSE = LOCKED_LIMITS.universe

CHECK_NAMES = [
    "demo_host_identity",
    "one_way_position_mode",
    "account_isolated_1x",
    "account_flat_vs_trend_state",
]


def signal(symbol: str, *, side: str, qty: str, state: str = "holding"):
    return SimpleNamespace(symbol=symbol, side=side, state=state, remaining_qty=D(qty))


class Client:
    def __init__(
        self,
        *,
        host_error: Exception | None = None,
        hedge: bool = False,
        bad_symbols: frozenset[str] = frozenset(),
        positions: dict[str, str] | None = None,
        position_error: Exception | None = None,
    ) -> None:
        self.host_error = host_error
        self.hedge = hedge
        self.bad_symbols = bad_symbols
        self.positions = positions if positions is not None else {}
        self.position_error = position_error
        self.calls: list[str] = []

    def assert_demo_identity(self) -> None:
        self.calls.append("assert_demo_identity")
        if self.host_error:
            raise self.host_error

    async def get_position_mode(self):
        self.calls.append("get_position_mode")
        return SimpleNamespace(is_hedge_mode=self.hedge)

    async def assert_symbol_isolated_1x(self, symbol: str) -> None:
        self.calls.append("assert_symbol_isolated_1x")
        if symbol in self.bad_symbols:
            raise RuntimeError("not isolated 1x")

    async def get_all_positions(self):
        self.calls.append("get_all_positions")
        if self.position_error:
            raise self.position_error
        return [
            SimpleNamespace(symbol=s, position_amt=D(self.positions.get(s, "0")))
            for s in UNIVERSE
        ]


class State:
    def __init__(self, signals=(), intents=(), error: Exception | None = None) -> None:
        self.signals, self.intents, self.error = signals, intents, error
        self.calls: list[str] = []

    async def list_active_signals(self):
        self.calls.append("list_active_signals")
        if self.error:
            raise self.error
        return self.signals

    async def list_unresolved_intents(self):
        self.calls.append("list_unresolved_intents")
        if self.error:
            raise self.error
        return self.intents


def gate(client: Client | None = None, state: State | None = None):
    return asyncio.run(
        run_truth_gate(client=client or Client(), state=state or State())
    )


def by_name(report):
    return {check.name: check for check in report.checks}


def test_a_flat_account_with_no_state_passes_all_four_named_checks():
    report = gate()
    assert [c.name for c in report.checks] == CHECK_NAMES
    assert report.verdict == "PASS"
    assert all(c.ok for c in report.checks)


def test_hedge_mode_fails_only_its_own_check():
    report = gate(client=Client(hedge=True))
    failed = [c.name for c in report.checks if not c.ok]
    assert failed == ["one_way_position_mode"]
    assert report.verdict == "FAIL"


@pytest.mark.parametrize("missing", UNIVERSE)
def test_every_universe_symbol_must_prove_isolated_1x(missing):
    check = by_name(gate(client=Client(bad_symbols=frozenset({missing}))))[
        "account_isolated_1x"
    ]
    assert check.ok is False and missing in check.detail


def test_a_foreign_position_is_not_attributable():
    check = by_name(gate(client=Client(positions={"BTCUSDT": "0.5"})))[
        "account_flat_vs_trend_state"
    ]
    assert check.ok is False
    assert check.detail == "foreign positions: BTCUSDT"


def test_an_unresolved_intent_blocks_the_gate():
    check = by_name(gate(state=State(intents=(object(),))))[
        "account_flat_vs_trend_state"
    ]
    assert (check.ok, check.detail) == (False, "unresolved_intents=1")


def test_a_signal_that_is_not_holding_blocks_the_gate():
    held = signal("ETHUSDT", side="BUY", qty="1", state="entry_reserved")
    check = by_name(gate(state=State(signals=(held,))))["account_flat_vs_trend_state"]
    assert check.ok is False
    assert "active_signals=1" in check.detail


@pytest.mark.parametrize(
    ("side", "amount", "expected_ok"),
    [
        ("BUY", "0.6", True),
        ("BUY", "-0.6", False),
        ("BUY", "0.5", False),
        ("SELL", "-0.6", True),
        ("SELL", "0.6", False),
    ],
)
def test_a_held_signal_must_match_the_account_position_in_direction_and_size(
    side, amount, expected_ok
):
    held = signal("BTCUSDT", side=side, qty="0.6")
    check = by_name(
        gate(client=Client(positions={"BTCUSDT": amount}), state=State(signals=(held,)))
    )["account_flat_vs_trend_state"]
    assert check.ok is expected_ok


def test_an_owned_position_passes():
    held = signal("SOLUSDT", side="BUY", qty="2")
    check = by_name(
        gate(
            client=Client(positions={"SOLUSDT": "2"}),
            state=State(signals=(held,)),
        )
    )["account_flat_vs_trend_state"]
    assert (check.ok, check.detail) == (True, "flat_or_owned=1")


def test_an_off_origin_client_fails_only_the_host_check():
    report = gate(client=Client(host_error=ValueError("x")))
    failed = [c.name for c in report.checks if not c.ok]
    assert failed == ["demo_host_identity"]


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"client": Client(host_error=OSError("x"))}, "demo_host_identity"),
        (
            {"client": Client(position_error=OSError("x"))},
            "account_flat_vs_trend_state",
        ),
        ({"state": State(error=OSError("x"))}, "account_flat_vs_trend_state"),
    ],
)
def test_an_unreadable_source_is_a_fail_and_every_check_still_runs(kwargs, name):
    report = gate(**kwargs)
    check = by_name(report)[name]
    assert check.ok is False and check.detail == "read failed: OSError"
    assert [c.name for c in report.checks] == CHECK_NAMES
    assert report.verdict == "FAIL"


def test_a_collaborator_message_is_never_copied_into_the_report():
    leaky = Client(position_error=RuntimeError("https://x/?signature=SECRET"))
    detail = by_name(gate(client=leaky))["account_flat_vs_trend_state"].detail
    assert detail == "read failed: RuntimeError" and "SECRET" not in detail


def test_an_empty_report_is_never_a_pass():
    assert truth_gate.TruthGateReport(()).verdict == "FAIL"


# --- read-only proof -------------------------------------------------------

READS = {
    "assert_demo_identity",
    "get_position_mode",
    "assert_symbol_isolated_1x",
    "get_all_positions",
    "list_active_signals",
    "list_unresolved_intents",
}
LOCAL_CALLS = {"append", "get", "items", "join", "set", "sorted", "is_hedge_mode"}


def _attribute_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_gate_module_calls_only_the_declared_reads():
    called = _attribute_calls(MODULE)
    assert READS <= called
    assert called - READS <= LOCAL_CALLS, called - READS


def test_the_gate_runs_only_reads_against_its_collaborators():
    client, state = Client(), State()
    gate(client=client, state=state)
    client_calls = {call.split(":")[0] for call in client.calls}
    assert client_calls | set(state.calls) == READS


def _names_and_imports(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    imported = {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for alias in n.names
    } | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return names, imported


def _import_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    modules |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return modules


def test_neither_gate_file_can_reach_an_order_or_a_write():
    for path in (MODULE, SCRIPT):
        names, imported = _names_and_imports(path)
        for forbidden in (
            "submit_order",
            "cancel_order",
            "set_leverage",
            "set_margin_type",
            "order_test",
            "TrendExecutor",
            "BinanceDemoLedgerService",
            "reserve_entry",
            "reserve_intent",
            "mark_sending",
            "mark_uncertain",
            "observe_signal",
            "apply_order_evidence",
            "commit",
            "confirm",
        ):
            assert forbidden not in names | imported, (path.name, forbidden)
        assert not any(m.startswith("record_") for m in names)
    module_imports = _import_modules(MODULE)
    assert module_imports == {"__future__", "dataclasses", "typing", "constants"}
    # The CLI must construct the client and open a read-only session; nothing
    # else from the app may enter this file.
    assert _import_modules(SCRIPT) == {
        "__future__",
        "argparse",
        "asyncio",
        "dataclasses",
        "datetime",
        "json",
        "os",
        "app.core.db",
        "app.services.brokers.binance.trend.executor",
        "app.services.brokers.binance.trend.state",
        "app.services.brokers.binance.trend.truth_gate",
    }


# --- CLI guard -------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirm", "trend", "futures", "message"),
    [
        (False, "true", "true", "--confirm-demo"),
        (True, None, "true", "BINANCE_TREND_DEMO_ENABLED"),
        (True, "true", None, "BINANCE_FUTURES_DEMO_ENABLED"),
        (True, "TRUE", "true", "BINANCE_TREND_DEMO_ENABLED"),
    ],
)
def test_cli_requires_confirm_and_both_exact_true_flags(
    monkeypatch, confirm, trend, futures, message
):
    for name, value in (
        ("BINANCE_TREND_DEMO_ENABLED", trend),
        ("BINANCE_FUTURES_DEMO_ENABLED", futures),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=message):
        gate_cli._guard_cli(argparse.Namespace(confirm_demo=confirm))


def test_cli_prints_one_verdict_line_and_maps_the_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    report = truth_gate.TruthGateReport(
        (truth_gate.GateCheck("demo_host_identity", True, "x"),)
    )

    async def fake_gate(*, client, state):
        return report

    class FakeClient:
        @classmethod
        def from_env(cls):
            return cls()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(gate_cli, "run_truth_gate", fake_gate)
    monkeypatch.setattr(gate_cli, "TrendDemoClient", FakeClient)
    monkeypatch.setattr(gate_cli, "TrendStateService", lambda factory: object())
    monkeypatch.setattr(gate_cli, "AsyncSessionLocal", lambda: None)
    code = asyncio.run(gate_cli._run(argparse.Namespace(confirm_demo=True)))
    line = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert line["event"] == "trend_truth_gate" and line["verdict"] == "PASS"
    assert line["checks"] == [{"name": "demo_host_identity", "ok": True, "detail": "x"}]

    report = truth_gate.TruthGateReport(
        (truth_gate.GateCheck("demo_host_identity", False, "x"),)
    )
    code = asyncio.run(gate_cli._run(argparse.Namespace(confirm_demo=True)))
    assert code == 2
