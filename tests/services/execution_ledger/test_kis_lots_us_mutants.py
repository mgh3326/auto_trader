"""#1173 — assertion-RED mutants for the KIS live US guards, counted from disk.

Guard sites are counted ON DISK in ``kis_lots.py`` and
``portfolio_ledger_lots.py``: every ``if``/conditional expression whose test is
``market == "us"``, ``off_venue`` or the US-date rollover comparison. Each
mutant compiles a copy of the module with ONE site's test forced false and runs
the scenario only that site should decide; the real module passes it, the
mutant must not (wrong answer or crash). A new site without a declared
invariant fails ``test_every_us_guard_site_has_a_mutant``. Every comparison
of the ``market`` variable with ``"us"`` in those files must be such a site, so
a US branch cannot hide inside another expression. (SQL predicates such as
``LiveOrderLedger.market == "us"`` are column filters proven by the DB tests.)

Quarantined rows are covered by the "filter dropped" DB mutant in
test_quarantine_readers_db.py::test_us_lots_drop_the_quarantined_row.

Invariant sentences (one per mutant):
- DAY: "today" for a US block is the US trading date, so an order or fill of
  the current US date blocks even after KST midnight.
- ROLLOVER: the US trading date rolls over at 20:00 America/New_York; a KIS
  daytime-session instant after it belongs to the next US date.
- VENUE_SCOPE: only a US block checks venues; an authoritative US row on an
  unrecognized venue is never counted.
- VENUE_FILTER: an unrecognized-venue row is removed from the counted rows.
- VENUE_UNKNOWN: an unrecognized-venue row makes the block unknown even when
  the remaining rows reconcile with the broker quantity.
- US_KEYS: a US block names its market, currency, venues, trading-date basis
  and order-ledger sources.
- US_UNKNOWN_KEYS: a US fail-closed unknown block carries the same US keys.
- SUPERSEDE_DATE: an authoritative row covers a websocket row only on the
  same US trading date, so a reused KIS order number of an older date never
  hides a fill of today.
- ATTACH_LOADER: US positions go to the US loader, never the KR loader.
- ATTACH_UNKNOWN: a US position whose read failed gets the US unknown block.
"""

from __future__ import annotations

import ast
import re
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.mcp_server.tooling import portfolio_ledger_lots
from app.services.execution_ledger import kis_lots

pytestmark = pytest.mark.unit

FILES = {
    "kis_lots": Path(kis_lots.__file__),
    "portfolio_ledger_lots": Path(portfolio_ledger_lots.__file__),
}
GUARD_TESTS = frozenset(
    {
        "market == 'us'",
        "off_venue",
        "local.time() >= _US_TRADING_DATE_ROLLOVER",
    }
)

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # 10:00 EDT, 23:00 KST
AFTER_KST_MIDNIGHT = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)  # 12:30 EDT
SESSION = datetime(2026, 10, 1, 13, 45, tzinfo=UTC)  # 09:45 EDT


def _fill(m: types.ModuleType, fid: int, side: str, qty: str, at: datetime, **kw):
    return m.LedgerFill(
        id=fid,
        source=kw.get("source", "reconciler"),
        side=side,
        quantity=Decimal(qty),
        price=Decimal("400"),
        filled_at=at,
        broker_order_id=f"{fid:08d}",
        venue=kw.get("venue", "NASD"),
    )


def _us_block(m: types.ModuleType, fills, *, reference: str, now: datetime):
    return m.build_symbol_block(
        symbol="MSFT",
        reference_quantity=Decimal(reference),
        current_price=Decimal("420"),
        fills=fills,
        orders=[],
        freshness=m.Freshness("fresh", now - timedelta(minutes=5), 5.0),
        now=now,
        market="us",
    )


def _history(m: types.ModuleType) -> list[Any]:
    return [
        _fill(m, 1, "buy", "5", NOW - timedelta(days=9)),
        _fill(m, 2, "buy", "4", NOW - timedelta(days=6), venue="NYSE"),
        _fill(m, 3, "sell", "1", NOW - timedelta(days=3), venue="AMEX"),
    ]


def scenario_day(m: types.ModuleType) -> bool:
    fills = [*_history(m), _fill(m, 9, "buy", "1", SESSION)]
    block = _us_block(m, fills, reference="9", now=AFTER_KST_MIDNIGHT)
    return block["same_day_buy_evidence"]["blocking"] is True


def scenario_rollover(m: types.ModuleType) -> bool:
    daytime = datetime(2026, 10, 2, 1, 30, tzinfo=UTC)  # 10:30 KST, 21:30 EDT
    return m.us_trading_day_window(daytime)[0] == datetime(2026, 10, 2, tzinfo=UTC)


def scenario_venue(m: types.ModuleType) -> bool:
    fills = [*_history(m), _fill(m, 9, "buy", "2", NOW, venue="NASDAQ")]
    block = _us_block(m, fills, reference="8", now=NOW)
    return (
        block["ledger_state"] == "unknown"
        and block["unknown_reasons"] == [m.UNKNOWN_UNRECOGNIZED_VENUE]
        and block["diagnostics"]["ledger_net_quantity"] == "8"
    )


def scenario_us_keys(m: types.ModuleType) -> bool:
    block = _us_block(m, _history(m), reference="8", now=NOW)
    return block["market"] == "us" and block["currency"] == "USD"


def scenario_us_unknown_keys(m: types.ModuleType) -> bool:
    block = m.unknown_block("MSFT", m.UNKNOWN_LOAD_FAILED, market="us")
    return block["market"] == "us" and block["trading_day_basis"].startswith("us_")


def scenario_supersede_date(m: types.ModuleType) -> bool:
    old = m.LedgerFill(
        id=1,
        source="reconciler",
        side="buy",
        quantity=Decimal("8"),
        price=Decimal("400"),
        filled_at=NOW - timedelta(days=9),
        broker_order_id="000123",
        venue="NASD",
    )
    today = m.LedgerFill(
        id=2,
        source="websocket",
        side="buy",
        quantity=Decimal("1"),
        price=Decimal("400"),
        filled_at=SESSION,
        broker_order_id="123",
        venue="NASD",
    )
    block = _us_block(m, [old, today], reference="8", now=NOW)
    return block["same_day_buy_evidence"]["blocking"] is True


def scenario_attach_loader(m: types.ModuleType) -> bool:
    return m._loader("us") is m.load_kis_live_us_lot_blocks


def scenario_attach_unknown(m: types.ModuleType) -> bool:
    return m._unknown("MSFT", "us").get("market") == "us"


# (file, site id) -> (invariant sentence key, scenario)
DECLARED: dict[tuple[str, str], tuple[str, Callable[[types.ModuleType], bool]]] = {
    ("kis_lots", "_day_start:market == 'us'#0"): ("DAY", scenario_day),
    (
        "kis_lots",
        "us_trading_day_window:local.time() >= _US_TRADING_DATE_ROLLOVER#0",
    ): ("ROLLOVER", scenario_rollover),
    ("kis_lots", "build_symbol_block:market == 'us'#0"): (
        "VENUE_SCOPE",
        scenario_venue,
    ),
    ("kis_lots", "build_symbol_block:off_venue#0"): ("VENUE_FILTER", scenario_venue),
    ("kis_lots", "build_symbol_block:off_venue#1"): ("VENUE_UNKNOWN", scenario_venue),
    ("kis_lots", "build_symbol_block:market == 'us'#1"): (
        "US_KEYS",
        scenario_us_keys,
    ),
    ("kis_lots", "_supersede_key:market == 'us'#0"): (
        "SUPERSEDE_DATE",
        scenario_supersede_date,
    ),
    ("kis_lots", "unknown_block:market == 'us'#0"): (
        "US_UNKNOWN_KEYS",
        scenario_us_unknown_keys,
    ),
    ("portfolio_ledger_lots", "_loader:market == 'us'#0"): (
        "ATTACH_LOADER",
        scenario_attach_loader,
    ),
    ("portfolio_ledger_lots", "_unknown:market == 'us'#0"): (
        "ATTACH_UNKNOWN",
        scenario_attach_unknown,
    ),
}


def _guard_nodes(tree: ast.Module) -> list[tuple[str, ast.If | ast.IfExp]]:
    """(site id, node) for every guard site, in source order."""
    sites: list[tuple[str, ast.If | ast.IfExp]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seen: dict[str, int] = {}
        nodes = [
            n
            for n in ast.walk(func)
            if isinstance(n, (ast.If, ast.IfExp)) and ast.unparse(n.test) in GUARD_TESTS
        ]
        for node in sorted(nodes, key=lambda n: (n.lineno, n.col_offset)):
            test = ast.unparse(node.test)
            ordinal = seen.get(test, 0)
            seen[test] = ordinal + 1
            sites.append((f"{func.name}:{test}#{ordinal}", node))
    return sites


def sites_on_disk() -> list[tuple[str, str]]:
    found = []
    for name, path in FILES.items():
        tree = ast.parse(path.read_text("utf-8"))
        found.extend((name, site) for site, _ in _guard_nodes(tree))
    assert len(found) == len(set(found)), found
    return sorted(found)


def test_every_us_guard_site_has_a_mutant() -> None:
    assert sites_on_disk() == sorted(DECLARED)
    sentences = set(re.findall(r"^- ([A-Z_]+):", __doc__ or "", re.MULTILINE))
    assert {key for key, _ in DECLARED.values()} == sentences


def test_every_market_variable_comparison_is_a_counted_site() -> None:
    for path in FILES.values():
        tree = ast.parse(path.read_text("utf-8"))
        guarded = {id(node.test) for _, node in _guard_nodes(tree)}
        for node in ast.walk(tree):
            operands = (
                [node.left, *node.comparators] if isinstance(node, ast.Compare) else []
            )
            if any(
                isinstance(c, ast.Name) and c.id == "market" for c in operands
            ) and any(
                isinstance(c, ast.Constant) and c.value == "us" for c in operands
            ):
                assert id(node) in guarded, (path.name, ast.unparse(node))


class _SiteOff(ast.NodeTransformer):
    def __init__(self, site: str) -> None:
        self.site = site
        self.replaced = 0

    def visit_Module(self, node: ast.Module) -> ast.AST:
        for site, guard in _guard_nodes(node):
            if site == self.site:
                guard.test = ast.Constant(value=False)
                self.replaced += 1
        return node


def _compile_mutant(file: str, site: str) -> types.ModuleType:
    source = FILES[file]
    transformer = _SiteOff(site)
    tree = ast.fix_missing_locations(
        transformer.visit(ast.parse(source.read_text("utf-8")))
    )
    assert transformer.replaced == 1, (file, site)
    name = f"_mutant_us_{file}_{abs(hash(site))}"
    module = types.ModuleType(name)
    module.__file__ = str(source)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(source), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


REAL = {"kis_lots": kis_lots, "portfolio_ledger_lots": portfolio_ledger_lots}


@pytest.mark.parametrize(("file", "site"), sorted(DECLARED))
def test_site_mutant_breaks_its_invariant(file: str, site: str) -> None:
    _key, scenario = DECLARED[(file, site)]
    assert scenario(REAL[file]) is True
    mutant = _compile_mutant(file, site)
    try:
        outcome = scenario(mutant)
    except (AttributeError, KeyError, IndexError, TypeError):
        outcome = False
    assert outcome is False


# ------------------------------------------------- one symbol identity (r5)

_MODELS = {"ExecutionLedger", "LiveOrderLedger", "KISLiveOrderLedger"}


def _loader_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(FILES["kis_lots"].read_text("utf-8"))
    [node] = [
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "load_kis_live_us_lot_blocks"
    ]
    return node


def test_us_loader_has_no_sql_symbol_expression() -> None:
    """r4 B1/B2: SQL must never compare, normalize or filter the symbol.

    A SQL re-implementation of the identity diverged three times (r3 F3,
    r4 B1, r4 B2); the loader references no model ``.symbol`` column at all.
    """
    offenders = [
        ast.unparse(n)
        for n in ast.walk(_loader_node())
        if isinstance(n, ast.Attribute)
        and n.attr == "symbol"
        and isinstance(n.value, ast.Name)
        and n.value.id in _MODELS
    ]
    assert offenders == []


def test_every_row_symbol_goes_through_the_one_identity() -> None:
    """Every ``<x>.symbol`` the loader reads is the argument of _us_symbol_key."""
    node = _loader_node()
    wrapped = {
        id(arg)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_us_symbol_key"
        for arg in call.args
    }
    reads = [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and n.attr == "symbol"
        and isinstance(n.ctx, ast.Load)
    ]
    assert reads
    for read in reads:
        source = ast.unparse(read)
        if source.startswith("ref."):
            continue  # the caller's key: passed through _us_symbol_key or used as the block key
        assert id(read) in wrapped, source
