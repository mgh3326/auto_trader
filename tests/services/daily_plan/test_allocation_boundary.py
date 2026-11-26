"""The comparison module stays pure: no database, broker, network or order code."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = "app.services.daily_plan.allocation"
# The whole in-repo closure. A new entry needs a look at what it pulls in.
CLOSURE = {
    MODULE,
    "app.core.symbol",
    "app.schemas.trading_policy",
    "app.services.daily_plan.bundle",
    "app.services.daily_plan.vocabulary",
    "app.services.halt_detection",
    "app.services.mechanical_loss_cut.rules",
    # Pure symbol constants behind is_parking_instrument.
    "app.services.order_proposals.parking_allowlist",
}
EXTERNAL = {
    "__future__",
    "bisect",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "fractions",
    "hashlib",
    "json",
    "math",
    "pydantic",
    "re",
    "typing",
}


def _file(module):
    path = ROOT / (module.replace(".", "/") + ".py")
    if path.is_file():
        return path
    package = ROOT / module.replace(".", "/") / "__init__.py"
    return package if package.is_file() else None


def _imports(module):
    for node in ast.walk(ast.parse(_file(module).read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (module, "relative import")
            yield node.module, [f"{node.module}.{name.name}" for name in node.names]
        elif isinstance(node, ast.Import):
            for name in node.names:
                yield name.name, []


def test_comparison_import_closure_is_pure():
    pending = [MODULE]
    local, external = set(), set()
    while pending:
        module = pending.pop()
        if module in local:
            continue
        local.add(module)
        for name, members in _imports(module):
            if name.split(".")[0] != "app":
                external.add(name.split(".")[0])
                continue
            # `from package import submodule` reaches the submodule too.
            pending.extend(item for item in [name, *members] if _file(item))
    assert local == CLOSURE
    assert external == EXTERNAL
