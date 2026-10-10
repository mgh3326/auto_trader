"""Assertion-RED mutants for the #1272 account guard (hk 1271 = B).

Branches are counted ON DISK: every ``if`` inside ``H5DemoClient.read_account``
(``client.py``) and inside ``_account`` (``truth_gate.py``). Each mutant compiles
a copy of that module with ONE branch condition forced false and runs the
scenario only that branch should satisfy; the real module passes it, the mutant
must fail it with a readable assertion. A new branch without a declared
invariant fails ``test_every_branch_has_a_mutant``; an invariant sentence below
without a branch (or the other way round) fails
``test_invariant_sentences_match_the_declared_mutants``.

Invariant sentences (one per mutant):
- CAN_TRADE: an account that cannot trade is refused.
- SINGLE_ASSET_PROOF: non-USDT balances are refused unless multiAssetsMargin is exactly false.
- ASSETS_COMPLETE: an empty or missing asset list is refused as incomplete.
- MALFORMED_ASSET: a non-object asset row is refused, never skipped.
- USDT_ROW: the USDT row is read as USDT, never as a foreign asset.
- DUPLICATE_USDT: a second USDT row is refused.
- NEGATIVE_FOREIGN: a negative or non-finite foreign balance is refused.
- FOREIGN_NAMED: every positive foreign balance is reported by name, zero rows are absent.
- ASSET_NAME: a positive foreign balance without a readable asset name is refused.
- USDT_REQUIRED: an account without a USDT row is refused.
- NAV_PROOF_REQUIRED: with foreign balances an unreadable USDT margin balance is refused.
- NAV_EQUALS_USDT: with foreign balances NAV must equal the USDT margin balance.
- POSITIONS_COMPLETE: a missing position list is refused.
- MALFORMED_POSITION: a non-object position row is refused, never skipped.
- UNIVERSE_ROWS: every H5 universe row reports its isolated 1x BOTH state.
- DUPLICATE_POSITION: a duplicate universe position row is refused.
- GATE_ISOLATION: the gate fails while any universe symbol is not isolated 1x BOTH.
- GATE_FOREIGN: a passing gate names the foreign assets and the single-asset mode.
- GATE_MODE: the gate fails named foreign assets whose mode is not exactly false.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import sys
import types
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.brokers.binance.h5 import client as h5_client
from app.services.brokers.binance.h5 import truth_gate
from app.services.brokers.binance.h5.client import H5Account
from app.services.brokers.binance.h5.strategy import UNIVERSE

pytestmark = pytest.mark.unit

CLIENT_SOURCE = Path(h5_client.__file__)
GATE_SOURCE = Path(truth_gate.__file__)
PACKAGE = "app.services.brokers.binance.h5"

USDT = {"asset": "USDT", "marginBalance": "1000.00000000"}
GRANTS = [
    {"asset": "USDC", "marginBalance": "5000.00000000"},
    {"asset": "BTC", "marginBalance": "0.01000000"},
]


def body(*, assets=None, **over):
    out = {
        "canTrade": True,
        "multiAssetsMargin": False,
        "totalMarginBalance": "1000.00000000",
        "assets": copy.deepcopy([USDT, *GRANTS] if assets is None else assets),
        "positions": [
            {"symbol": s, "isolated": True, "leverage": "1", "positionSide": "BOTH"}
            for s in UNIVERSE
        ],
    }
    out.update(over)
    return out


def read(m, account_body):
    """Run ``read_account`` and turn every outcome into a comparable value."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/fapi/v2/account"
        return httpx.Response(200, json=account_body)

    client = m.H5DemoClient(api_key="FAKE", api_secret="FAKE")
    client._client = httpx.AsyncClient(
        base_url="https://demo-fapi.binance.com",
        transport=httpx.MockTransport(dispatch),
    )
    try:
        return ("ok", asyncio.run(client.read_account()))
    except m.H5BrokerTruthUnavailable as exc:
        return ("refused", str(exc))
    except Exception as exc:  # noqa: BLE001 - a crash is a distinct outcome
        return ("other", type(exc).__name__)


def refused(m, account_body, message):
    assert read(m, account_body) == ("refused", message)


def accepted(m, account_body) -> H5Account:
    kind, value = read(m, account_body)
    assert kind == "ok", value
    return value


# --- client scenarios -----------------------------------------------------------


def sc_can_trade(m):
    refused(m, body(canTrade=False), "account trading status unavailable")


def sc_single_asset_proof(m):
    for mode in (True, "false", None, 0):
        refused(
            m, body(multiAssetsMargin=mode), "single-asset margin evidence unavailable"
        )


def sc_assets_complete(m):
    refused(m, body(assets=[]), "complete account assets unavailable")


def sc_malformed_asset(m):
    refused(m, body(assets=[USDT, "BTC"]), "malformed account asset")


def sc_usdt_row(m):
    account = accepted(m, body(assets=[USDT]))
    assert account.non_usdt_assets == ()
    assert account.nav_usdt == Decimal("1000")


def sc_duplicate_usdt(m):
    refused(m, body(assets=[USDT, USDT, *GRANTS]), "duplicate USDT account asset")


def sc_negative_foreign(m):
    for balance in ("-0.01", "NaN", "-Infinity"):
        refused(
            m,
            body(assets=[USDT, {"asset": "BTC", "marginBalance": balance}]),
            "foreign account asset exposure",
        )


def sc_foreign_named(m):
    zero = [{"asset": "BNB", "marginBalance": "0.00000000"}]
    account = accepted(m, body(assets=[USDT, *GRANTS, *zero]))
    assert account.non_usdt_assets == ("BTC", "USDC")
    assert account.multi_assets_margin is False


def sc_asset_name(m):
    for name in (None, "", "usdc", "BTC,ETH", 7):
        refused(
            m,
            body(assets=[USDT, {"asset": name, "marginBalance": "1"}]),
            "non-USDT asset unreadable",
        )


def sc_usdt_required(m):
    refused(
        m,
        body(assets=[{"asset": "USDC", "marginBalance": "0"}]),
        "USDT account asset unavailable",
    )


def sc_nav_proof_required(m):
    for unreadable in ("x", None):
        refused(
            m,
            body(assets=[{"asset": "USDT", "marginBalance": unreadable}, *GRANTS]),
            "USDT margin balance unreadable",
        )


def sc_nav_equals_usdt(m):
    for balance in ("6000.00000000", "999.99999999", "NaN"):
        refused(
            m,
            body(assets=[{"asset": "USDT", "marginBalance": balance}, *GRANTS]),
            "USDT NAV not separable from non-USDT assets",
        )


def sc_positions_complete(m):
    refused(m, body(positions=None), "complete margin configuration unavailable")


def sc_malformed_position(m):
    refused(m, body(positions=["BTCUSDT"]), "malformed account position")


def sc_universe_rows(m):
    assert accepted(m, body()).per_symbol_isolated_1x == dict.fromkeys(UNIVERSE, True)


def sc_duplicate_position(m):
    rows = body()["positions"]
    refused(m, body(positions=[*rows, rows[0]]), "duplicate account position row")


# --- gate scenarios -------------------------------------------------------------


class StaticClient:
    def __init__(self, account) -> None:
        self.account = account

    async def read_account(self):
        return self.account

    async def get_position_mode(self):
        return types.SimpleNamespace(is_hedge_mode=False)

    async def get_all_positions(self):
        return []

    async def get_all_open_orders(self):
        return types.SimpleNamespace(orders=[])


class Empty:
    async def list_active_signals(self):
        return ()

    async def list_unresolved_intents(self):
        return ()

    async def count_open_lifecycles(self):
        return 0

    async def status_distribution(self):
        return {}


def account_check(m, account):
    try:
        report = asyncio.run(
            m.run_truth_gate(
                client=StaticClient(account), state=Empty(), ledger=Empty()
            )
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"gate raised {type(exc).__name__}")
    check = report.checks[0]
    assert check.name == "account_isolated_1x"
    return check.ok, check.detail


def h5_account(*, flags=None, foreign=(), mode=False):
    return H5Account(
        nav_usdt=Decimal("1000"),
        per_symbol_isolated_1x=flags or dict.fromkeys(UNIVERSE, True),
        non_usdt_assets=foreign,
        multi_assets_margin=mode,
    )


def sc_gate_isolation(m):
    flags = {s: s != "SOLUSDT" for s in UNIVERSE}
    assert account_check(m, h5_account(flags=flags)) == (
        False,
        "not isolated 1x BOTH: SOLUSDT",
    )


def sc_gate_foreign(m):
    assert account_check(m, h5_account(foreign=("USDC", "BTC"))) == (
        True,
        "nav_usdt=1000 symbols=all margin_mode=single_asset non_usdt_assets=BTC,USDC",
    )
    assert account_check(m, h5_account()) == (True, "nav_usdt=1000 symbols=all")


def sc_gate_mode(m):
    for mode in (True, None, "false", 0):
        assert account_check(m, h5_account(foreign=("USDC",), mode=mode)) == (
            False,
            "non-USDT assets without single-asset margin: USDC",
        )


READ = ("client", "H5DemoClient.read_account")
GATE = ("gate", "_account")

# (module, function, branch condition source) -> (invariant key, scenario)
DECLARED: dict[tuple[str, str, str], tuple[str, Callable[[types.ModuleType], None]]] = {
    (*READ, "not isinstance(body, dict) or body.get('canTrade') is not True"): (
        "CAN_TRADE",
        sc_can_trade,
    ),
    (*READ, "body.get('multiAssetsMargin') is not False"): (
        "SINGLE_ASSET_PROOF",
        sc_single_asset_proof,
    ),
    (*READ, "not isinstance(assets, list) or not assets"): (
        "ASSETS_COMPLETE",
        sc_assets_complete,
    ),
    (*READ, "not isinstance(asset, dict)"): ("MALFORMED_ASSET", sc_malformed_asset),
    (*READ, "asset.get('asset') == 'USDT'"): ("USDT_ROW", sc_usdt_row),
    (*READ, "usdt_seen"): ("DUPLICATE_USDT", sc_duplicate_usdt),
    (*READ, "not balance.is_finite() or balance < 0"): (
        "NEGATIVE_FOREIGN",
        sc_negative_foreign,
    ),
    (*READ, "balance != 0"): ("FOREIGN_NAMED", sc_foreign_named),
    (*READ, "not isinstance(name, str) or not _ASSET_NAME.fullmatch(name)"): (
        "ASSET_NAME",
        sc_asset_name,
    ),
    (*READ, "not usdt_seen"): ("USDT_REQUIRED", sc_usdt_required),
    (*READ, "non_usdt"): ("NAV_PROOF_REQUIRED", sc_nav_proof_required),
    (*READ, "not usdt_balance.is_finite() or usdt_balance != nav"): (
        "NAV_EQUALS_USDT",
        sc_nav_equals_usdt,
    ),
    (*READ, "not isinstance(positions, list)"): (
        "POSITIONS_COMPLETE",
        sc_positions_complete,
    ),
    (*READ, "not isinstance(row, dict)"): ("MALFORMED_POSITION", sc_malformed_position),
    (*READ, "symbol in UNIVERSE"): ("UNIVERSE_ROWS", sc_universe_rows),
    (*READ, "symbol in configured"): ("DUPLICATE_POSITION", sc_duplicate_position),
    (*GATE, "bad"): ("GATE_ISOLATION", sc_gate_isolation),
    (*GATE, "foreign"): ("GATE_FOREIGN", sc_gate_foreign),
    (*GATE, "getattr(account, 'multi_assets_margin', None) is not False"): (
        "GATE_MODE",
        sc_gate_mode,
    ),
}

SOURCES = {"client": CLIENT_SOURCE, "gate": GATE_SOURCE}
REAL = {"client": h5_client, "gate": truth_gate}


def _branches(module: str, tree: ast.Module) -> list[tuple[str, str, ast.If]]:
    found: list[tuple[str, str, ast.If]] = []

    def visit(qualified: str, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.If):
                found.append((qualified, ast.unparse(child.test), child))

    for node in tree.body:
        if module == "client" and isinstance(node, ast.ClassDef):
            if node.name == "H5DemoClient":
                for item in node.body:
                    if (
                        isinstance(item, ast.AsyncFunctionDef)
                        and item.name == "read_account"
                    ):
                        visit("H5DemoClient.read_account", item)
        elif (
            module == "gate"
            and isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_account"
        ):
            visit("_account", node)
    return found


def _on_disk() -> set[tuple[str, str, str]]:
    return {
        (module, q, t)
        for module, path in SOURCES.items()
        for q, t, _ in _branches(module, ast.parse(path.read_text("utf-8")))
    }


def _mutant_module(target: tuple[str, str, str]) -> types.ModuleType:
    module_key, qualified, test = target
    source = SOURCES[module_key]
    tree = ast.parse(source.read_text("utf-8"))
    hits = [n for q, t, n in _branches(module_key, tree) if (q, t) == (qualified, test)]
    assert len(hits) == 1, target
    hits[0].test = ast.Constant(False)
    ast.fix_missing_locations(tree)
    name = (
        "h5_single_asset_mutant_"
        + "".join(c if c.isalnum() else "_" for c in "".join(target))[:60]
    )
    module = types.ModuleType(name)
    module.__file__ = str(source)
    module.__package__ = PACKAGE
    sys.modules[name] = module
    try:
        exec(compile(tree, str(source), "exec"), module.__dict__)  # noqa: S102
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def test_every_branch_has_a_mutant():
    on_disk = _on_disk()
    assert on_disk == set(DECLARED), {
        "undeclared": sorted(on_disk - set(DECLARED)),
        "stale": sorted(set(DECLARED) - on_disk),
    }


def test_invariant_sentences_match_the_declared_mutants():
    doc = __doc__ or ""
    keys = [
        line[2:].split(":", 1)[0]
        for line in doc.splitlines()
        if line.startswith("- ") and ": " in line
    ]
    declared = [key for key, _ in DECLARED.values()]
    assert sorted(keys) == sorted(declared)
    assert len(set(declared)) == len(declared) == len(_on_disk()) == 19


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_scenario_passes_on_the_real_module(target):
    DECLARED[target][1](REAL[target[0]])


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_mutant_is_killed_by_its_invariant(target):
    _, scenario = DECLARED[target]
    mutant = _mutant_module(target)
    try:
        # Only a readable assertion (or pytest.fail) counts as the invariant
        # going RED; an AttributeError or TypeError from a broken mutant does not.
        with pytest.raises((AssertionError, pytest.fail.Exception)):
            scenario(mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)
