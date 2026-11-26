"""The assembler reads the database only: no broker, order, watch or mandate path."""

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BUILDER = "app.services.daily_plan.bundle_builder"
READER = "app.services.daily_plan.bundle_reader"
SCRIPT = "scripts.build_daily_plan_bundle"
# The builder's whole in-repo closure. A new entry needs a look at what it pulls in.
BUILDER_CLOSURE = {
    BUILDER,
    "app.core.price_rounding",
    "app.core.symbol",
    "app.core.upbit_tick",
    "app.schemas.trading_policy",
    "app.services.daily_plan.allocation",
    "app.services.daily_plan.bundle",
    "app.services.daily_plan.vocabulary",
    "app.services.halt_detection",
    "app.services.mechanical_loss_cut.rules",
    "app.services.technical_math",
    # Pure symbol constants behind is_parking_instrument.
    "app.services.order_proposals.parking_allowlist",
}
# Nothing under these may be loaded, except the exact modules listed after it.
GUARDED = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.mandate",
    "app.services.investment_watch",
    "app.services.execution_intent_gateway",
    "app.services.mechanical_loss_cut.job",
    "app.services.mechanical_loss_cut.store",
    "app.services.mechanical_loss_cut.live",
    "app.services.kis",
    "app.services.market_data",
    "app.mcp_server",
    "app.tasks",
)
PURE_EXCEPTIONS = {
    # Model definitions import these vocabulary modules; parking_allowlist and
    # tick_size are the daily-plan service's existing pure helpers.
    "app.mcp_server",
    "app.mcp_server.tick_size",
    "app.services.mandate",
    "app.services.mandate.account_ref",
    "app.services.mandate.vocabulary",
    "app.services.order_proposals",
    "app.services.order_proposals.callback_inbox",
    "app.services.order_proposals.callback_inbox.contracts",
    "app.services.order_proposals.errors",
    "app.services.order_proposals.parking_allowlist",
    "app.services.order_proposals.state_machine",
}


def _file(module):
    base = ROOT / module.replace(".", "/")
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _imports(module):
    for node in ast.walk(ast.parse(_file(module).read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (module, "relative import")
            yield node.module, [f"{node.module}.{name.name}" for name in node.names]
        elif isinstance(node, ast.Import):
            for name in node.names:
                yield name.name, []


def test_builder_import_closure_is_pure():
    pending, local = [BUILDER], set()
    external = set()
    while pending:
        module = pending.pop()
        if module in local:
            continue
        local.add(module)
        for name, members in _imports(module):
            if name.split(".")[0] != "app":
                external.add(name.split(".")[0])
                continue
            pending.extend(item for item in [name, *members] if _file(item))
    assert local == BUILDER_CLOSURE
    assert not external & {"sqlalchemy", "httpx", "asyncio", "socket", "redis"}


def test_reader_and_script_import_no_forbidden_module_directly():
    for module in (READER, SCRIPT):
        for name, members in _imports(module):
            for item in (name, *members):
                if item.startswith(GUARDED) and _file(item):
                    assert item == "app.services.mandate.account_ref", (module, item)
    # Only the pure reference digest comes from the mandate package.
    assert [
        members
        for name, members in _imports(SCRIPT)
        if name.startswith("app.services.mandate")
    ] == [["app.services.mandate.account_ref.derive_account_ref"]]


def test_loading_the_assembler_loads_no_order_or_broker_module():
    program = f"""
import json, sys
import {BUILDER}, {READER}, {SCRIPT}
from app.services.mechanical_loss_cut.rules import is_parking_instrument
is_parking_instrument("459580", "equity_kr")
print(json.dumps(sorted(sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = set(json.loads(result.stdout.splitlines()[-1]))
    assert {BUILDER, READER, SCRIPT} <= loaded
    assert {m for m in loaded if m.startswith(GUARDED)} <= PURE_EXCEPTIONS


def test_assembler_writes_only_through_the_daily_plan_writer():
    # The builder cannot write: its closure above holds no database library.
    for module in (READER, SCRIPT):
        tree = ast.parse(_file(module).read_text())
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert not calls & {
            "add",
            "add_all",
            "commit",
            "delete",
            "flush",
            "insert",
            "merge",
            "write_version",
            "confirm_links",
        }, module
        sql = " ".join(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        for word in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE"):
            assert word not in sql, (module, word)
    assert "store_input_bundle" not in _file(READER).read_text()
