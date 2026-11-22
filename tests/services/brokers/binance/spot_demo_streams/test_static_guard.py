"""The collector has no order/cancel/ledger capability (hk 6067)."""

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[5]
PKG = REPO / "app/services/brokers/binance/spot_demo_streams"
CLI = REPO / "scripts/binance_spot_demo_streams.py"

FORBIDDEN_MODULE_FRAGMENTS = (
    "execution_client",
    "spot_demo",  # the signed REST package (exact-prefix check below allows ours)
    "futures_demo",
    "ledger",
    "order",
    "execution",
    "mock_strategy",
    "transport",  # signed REST transports
    "httpx",
    "requests",
    "aiohttp",
    "sqlalchemy",
    "app.models",
    "app.services.brokers.kis",
    "app.services.brokers.upbit",
)
OURS = "app.services.brokers.binance.spot_demo_streams"
FORBIDDEN_NAMES = {
    "submit_order",
    "cancel_order",
    "place_order",
    "new_order",
    "record_fill",
}


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
            mods += [f"{node.module}.{a.name}" for a in node.names]
    return mods


def _files() -> list[pathlib.Path]:
    return [*PKG.glob("*.py"), CLI]


def test_no_order_execution_or_ledger_imports() -> None:
    offenders = []
    for path in _files():
        for mod in _imports(path):
            probe = mod.replace(OURS, "")  # our own package name contains 'spot_demo'
            if mod.startswith(OURS):
                continue
            if any(frag in probe for frag in FORBIDDEN_MODULE_FRAGMENTS):
                offenders.append((path.name, mod))
    # redis/config are allowed; the demo credential resolver is the only demo import.
    assert not offenders, offenders


def test_demo_credential_resolver_is_the_only_demo_import() -> None:
    demo = {m for p in _files() for m in _imports(p) if "binance.demo" in m}
    assert {
        m.rsplit(".", 1)[0]
        if m.endswith(
            (
                "ResolvedDemoCredential",
                "resolve_demo_credentials",
                "BinanceDemoCredentialError",
            )
        )
        else m
        for m in demo
    } <= {
        "app.services.brokers.binance.demo.credentials",
        "app.services.brokers.binance.demo.errors",
    }


def test_no_order_verbs_anywhere() -> None:
    for path in _files():
        tree = ast.parse(path.read_text())
        names = (
            {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            | {
                n.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        )
        assert not (names & FORBIDDEN_NAMES), (path.name, names & FORBIDDEN_NAMES)


def test_only_one_ws_send_site_exists() -> None:
    sends = [
        (p.name, n.lineno)
        for p in PKG.glob("*.py")
        for n in ast.walk(ast.parse(p.read_text()))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "send"
    ]
    assert len(sends) == 1 and sends[0][0] == "collector.py"


def _closure(entry: pathlib.Path) -> set[str]:
    """Static transitive closure of first-party ``app.*`` modules (no import)."""
    seen: set[str] = set()
    todo = [entry]
    while todo:
        path = todo.pop()
        for mod in _imports(path):
            if not mod.startswith("app.") or mod in seen:
                continue
            rel = REPO / (mod.replace(".", "/") + ".py")
            pkg = REPO / mod.replace(".", "/") / "__init__.py"
            target = rel if rel.exists() else pkg if pkg.exists() else None
            if target is None:
                continue  # ``from pkg import name`` form: the name is not a module
            seen.add(mod)
            todo.append(target)
            # importing a module runs every parent package ``__init__``
            parts = mod.split(".")
            for i in range(2, len(parts)):
                init = REPO / "/".join(parts[:i]) / "__init__.py"
                parent = ".".join(parts[:i])
                if init.exists() and parent not in seen:
                    seen.add(parent)
                    todo.append(init)
    return seen


def test_import_closure_reaches_no_signed_rest_or_ledger_module() -> None:
    closure = _closure(PKG / "collector.py") | _closure(CLI)
    banned = (
        "app.services.brokers.binance.spot_demo.",
        "app.services.brokers.binance.futures_demo",
        "app.services.brokers.binance.demo.ledger",
        "app.models",
        "app.services.mock_strategy",
        "app.services.brokers.binance.transport",
        "app.services.brokers.binance.rest_client",
    )
    offenders = sorted(
        m
        for m in closure
        if m.startswith(banned) or m == "app.services.brokers.binance.spot_demo"
    )
    assert not offenders, offenders


def test_default_off_and_no_scheduler_registration() -> None:
    text = (PKG / "collector.py").read_text()
    assert 'environ.get(ENABLE_ENV) != "true"' in text
    for path in _files():
        src = path.read_text()
        assert "taskiq" not in src.lower() and "cron" not in src.lower()
