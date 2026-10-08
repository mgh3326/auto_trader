"""H5 read-only truth gate: verdicts, failure isolation and the read-only call set."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.brokers.binance.h5 import truth_gate
from app.services.brokers.binance.h5.strategy import UNIVERSE
from app.services.brokers.binance.h5.truth_gate import run_truth_gate
from scripts import binance_h5_truth_gate as gate_cli

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE = REPO_ROOT / "app/services/brokers/binance/h5/truth_gate.py"
SCRIPT = REPO_ROOT / "scripts/binance_h5_truth_gate.py"

CHECK_NAMES = [
    "account_isolated_1x",
    "one_way_position_mode",
    "positions_flat",
    "no_open_orders",
    "h5_state_empty",
    "demo_ledger_no_open_roots",
]


class Client:
    def __init__(self, **overrides) -> None:
        self.calls: list[str] = []
        self.overrides = overrides

    def _answer(self, name: str, default):
        self.calls.append(name)
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    async def read_account(self):
        return self._answer(
            "read_account",
            SimpleNamespace(
                nav_usdt=Decimal("1000"),
                per_symbol_isolated_1x=dict.fromkeys(UNIVERSE, True),
            ),
        )

    async def get_position_mode(self):
        return self._answer("get_position_mode", SimpleNamespace(is_hedge_mode=False))

    async def get_all_positions(self):
        return self._answer(
            "get_all_positions",
            [SimpleNamespace(symbol="BTCUSDT", position_amt=Decimal(0))],
        )

    async def get_all_open_orders(self):
        return self._answer("get_all_open_orders", SimpleNamespace(orders=[]))


class State:
    def __init__(self, signals=(), intents=(), error: Exception | None = None):
        self.signals, self.intents, self.error = signals, intents, error
        self.calls: list[str] = []

    async def list_active_signals(self):
        self.calls.append("list_active_signals")
        if self.error:
            raise self.error
        return self.signals

    async def list_unresolved_intents(self):
        self.calls.append("list_unresolved_intents")
        return self.intents


class Ledger:
    def __init__(self, open_roots=0, error: Exception | None = None):
        self.open_roots, self.error = open_roots, error
        self.calls: list[str] = []

    async def count_open_lifecycles(self):
        self.calls.append("count_open_lifecycles")
        if self.error:
            raise self.error
        return self.open_roots

    async def status_distribution(self):
        self.calls.append("status_distribution")
        return {"reconciled": 3, "anomaly": 1}


def gate(client=None, state=None, ledger=None):
    return asyncio.run(
        run_truth_gate(
            client=client or Client(), state=state or State(), ledger=ledger or Ledger()
        )
    )


def by_name(report):
    return {c.name: c for c in report.checks}


def test_flat_attributed_account_passes_with_six_named_checks():
    report = gate()
    assert [c.name for c in report.checks] == CHECK_NAMES
    assert report.verdict == "PASS" and all(c.ok for c in report.checks)


def test_open_position_fails_only_the_position_check():
    client = Client(
        get_all_positions=[
            SimpleNamespace(symbol="ETHUSDT", position_amt=Decimal("0.5")),
            SimpleNamespace(symbol="BTCUSDT", position_amt=Decimal(0)),
        ]
    )
    report = gate(client=client)
    failed = [c for c in report.checks if not c.ok]
    assert [c.name for c in failed] == ["positions_flat"]
    assert failed[0].detail == "non-flat: ETHUSDT"
    assert report.verdict == "FAIL"


def test_open_orders_fail():
    client = Client(get_all_open_orders=SimpleNamespace(orders=[object(), object()]))
    check = by_name(gate(client=client))["no_open_orders"]
    assert (check.ok, check.detail) == (False, "open orders: 2")


def test_hedge_mode_fails():
    client = Client(get_position_mode=SimpleNamespace(is_hedge_mode=True))
    assert by_name(gate(client=client))["one_way_position_mode"].ok is False


@pytest.mark.parametrize("missing", list(UNIVERSE))
def test_every_universe_symbol_must_be_isolated_1x(missing):
    flags = {s: s != missing for s in UNIVERSE}
    client = Client(
        read_account=SimpleNamespace(nav_usdt=Decimal(5), per_symbol_isolated_1x=flags)
    )
    check = by_name(gate(client=client))["account_isolated_1x"]
    assert check.ok is False and missing in check.detail


def test_a_symbol_absent_from_the_account_report_fails_too():
    client = Client(
        read_account=SimpleNamespace(
            nav_usdt=Decimal(5), per_symbol_isolated_1x={"BTCUSDT": True}
        )
    )
    assert by_name(gate(client=client))["account_isolated_1x"].ok is False


def test_h5_state_must_be_empty():
    state = State(signals=(object(),), intents=(object(), object()))
    check = by_name(gate(state=state))["h5_state_empty"]
    assert (check.ok, check.detail) == (
        False,
        "active_signals=1 unresolved_intents=2",
    )


def test_open_ledger_roots_fail_and_the_distribution_is_reported():
    check = by_name(gate(ledger=Ledger(open_roots=2)))["demo_ledger_no_open_roots"]
    assert check.ok is False
    assert "open_roots=2" in check.detail and "anomaly=1" in check.detail


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"client": Client(read_account=OSError("x"))}, "account_isolated_1x"),
        ({"client": Client(get_position_mode=OSError("x"))}, "one_way_position_mode"),
        ({"client": Client(get_all_positions=OSError("x"))}, "positions_flat"),
        ({"client": Client(get_all_open_orders=OSError("x"))}, "no_open_orders"),
        ({"state": State(error=OSError("x"))}, "h5_state_empty"),
        ({"ledger": Ledger(error=OSError("x"))}, "demo_ledger_no_open_roots"),
    ],
)
def test_an_unreadable_source_is_a_fail_not_a_pass_and_others_still_run(kwargs, name):
    report = gate(**kwargs)
    check = by_name(report)[name]
    assert check.ok is False and check.detail == "read failed: OSError"
    assert report.verdict == "FAIL"
    assert [c.name for c in report.checks] == CHECK_NAMES
    assert sum(1 for c in report.checks if c.ok) == 5


def test_h5_refusal_messages_reach_the_operator_but_other_errors_stay_class_only():
    from app.services.brokers.binance.h5.client import H5BrokerTruthUnavailable
    from app.services.brokers.binance.h5.state import H5StateBlocked

    client = Client(
        read_account=H5BrokerTruthUnavailable("foreign account asset exposure")
    )
    check = by_name(gate(client=client))["account_isolated_1x"]
    assert check.detail == (
        "read failed: H5BrokerTruthUnavailable: foreign account asset exposure"
    )
    state = State(error=H5StateBlocked("uncertain H5 exposure unresolved"))
    assert by_name(gate(state=state))["h5_state_empty"].detail.endswith(
        ": uncertain H5 exposure unresolved"
    )
    leaky = Client(get_all_positions=RuntimeError("https://x/?signature=SECRET"))
    detail = by_name(gate(client=leaky))["positions_flat"].detail
    assert detail == "read failed: RuntimeError" and "SECRET" not in detail


def test_an_empty_report_is_never_a_pass():
    assert truth_gate.TruthGateReport(()).verdict == "FAIL"


# --- read-only proof -------------------------------------------------------------

READS = {
    "read_account",
    "get_position_mode",
    "get_all_positions",
    "get_all_open_orders",
    "list_active_signals",
    "list_unresolved_intents",
    "count_open_lifecycles",
    "status_distribution",
}


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
    assert called - READS <= {"join", "get", "items"}, called - READS


def test_the_gate_runs_only_reads_against_its_collaborators():
    client, state, ledger = Client(), State(), Ledger()
    gate(client=client, state=state, ledger=ledger)
    assert set(client.calls) | set(state.calls) | set(ledger.calls) == READS


def test_neither_gate_file_can_reach_a_mutation():
    for path in (MODULE, SCRIPT):
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
        for forbidden in (
            "submit_order",
            "cancel_order",
            "set_leverage",
            "order_test",
            "H5Executor",
            "reserve_entry",
            "reserve_intent",
            "fence_send",
            "observe_signal",
            "apply_order_evidence",
            "record_nav",
            "mark_uncertain",
            "commit",
            "confirm",
        ):
            assert forbidden not in names | imported, (path.name, forbidden)
        assert not any("executor" in m or "demo_strategy_loop" in m for m in imported)
        assert not any(m.startswith("record_") for m in names)


# --- CLI ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirm", "h5", "futures", "message"),
    [
        (False, "true", "true", "--confirm-demo"),
        (True, None, "true", "BINANCE_H5_DEMO_ENABLED"),
        (True, "true", None, "BINANCE_FUTURES_DEMO_ENABLED"),
        (True, "TRUE", "true", "BINANCE_H5_DEMO_ENABLED"),
    ],
)
def test_cli_requires_confirm_and_both_exact_true_flags(
    monkeypatch, confirm, h5, futures, message
):
    for name, value in (
        ("BINANCE_H5_DEMO_ENABLED", h5),
        ("BINANCE_FUTURES_DEMO_ENABLED", futures),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=message):
        gate_cli._guard_cli(argparse.Namespace(confirm_demo=confirm))


def test_cli_prints_one_verdict_line_and_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    closed: list[bool] = []

    class FakeClient(Client):
        async def aclose(self):
            closed.append(True)

    fake = FakeClient()
    monkeypatch.setattr(
        gate_cli.H5DemoClient, "from_env", classmethod(lambda cls: fake)
    )
    monkeypatch.setattr(gate_cli, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(gate_cli, "H5StateService", lambda factory: State())
    monkeypatch.setattr(gate_cli, "BinanceDemoLedgerService", lambda db: Ledger())

    args = argparse.Namespace(confirm_demo=True)
    assert asyncio.run(gate_cli._run(args)) == 0
    (line,) = capsys.readouterr().out.splitlines()
    record = json.loads(line)
    assert record["event"] == "h5_truth_gate" and record["verdict"] == "PASS"
    assert [c["name"] for c in record["checks"]] == CHECK_NAMES
    assert closed == [True]

    fake.overrides["get_all_open_orders"] = SimpleNamespace(orders=[object()])
    assert asyncio.run(gate_cli._run(args)) == 2
    assert '"verdict": "FAIL"' in capsys.readouterr().out
