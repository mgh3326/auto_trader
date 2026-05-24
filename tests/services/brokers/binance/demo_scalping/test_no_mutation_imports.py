"""ROB-307 PR1 — static import guard for the read-only scalping package.

Acceptance (§ "Hard safety boundaries" + acceptance criteria): live
Binance endpoints and non-Demo order paths must be **unreachable** from
the scalping path. This guard fails the build if any module under
``app/services/brokers/binance/demo_scalping/`` imports:

* a signed execution client or signing helper (order mutation),
* the live public adapter / host allowlist / transport (which permit
  ``api.binance.com`` — the signal runner is Demo-host-only),
* the service-internal ledger repository (reads go via the service),
* the Demo credential resolver (a read-only signal needs no secrets).
"""

from __future__ import annotations

import ast
import pathlib

_SCALPING_ROOT = pathlib.Path("app/services/brokers/binance/demo_scalping")

_BANNED_PREFIXES = (
    # order mutation
    "app.services.brokers.binance.spot_demo.execution_client",
    "app.services.brokers.binance.futures_demo.execution_client",
    "app.services.brokers.binance.spot_demo.signing",
    "app.services.brokers.binance.futures_demo.signing",
    "app.services.brokers.binance.spot_demo.transport",
    "app.services.brokers.binance.futures_demo.transport",
    # live-host public adapter (PUBLIC_HOSTS includes api.binance.com)
    "app.services.brokers.binance.rest_client",
    "app.services.brokers.binance.host_allowlist",
    "app.services.brokers.binance.transport",
    # ledger internals + secrets
    "app.services.brokers.binance.demo.ledger.repository",
    "app.services.brokers.binance.demo.credentials",
)


def _imports_in_file(py: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module.startswith(p) for p in _BANNED_PREFIXES):
                offenders.append(f"{py}: from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in _BANNED_PREFIXES):
                    offenders.append(f"{py}: import {alias.name}")
    return offenders


def _imports_in(root: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        offenders.extend(_imports_in_file(py))
    return offenders


def test_scanner_detects_a_synthetic_offender(tmp_path: pathlib.Path) -> None:
    # Proves the guard actually catches banned imports (not vacuously green).
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from app.services.brokers.binance.spot_demo.execution_client "
        "import BinanceSpotDemoExecutionClient\n"
    )
    offenders = _imports_in(tmp_path)
    assert offenders, "scanner should flag a banned execution_client import"


def test_scalping_package_has_no_mutation_or_live_imports() -> None:
    offenders = _imports_in(_SCALPING_ROOT)
    assert not offenders, (
        "ROB-307 read-only scalping path must not import order-mutation, "
        "live-host, ledger-internal, or credential modules. Offenders:\n"
        + "\n".join(offenders)
    )


def test_observe_only_cli_has_no_mutation_or_live_imports() -> None:
    cli = pathlib.Path("scripts/binance_demo_scalping_signal.py")
    offenders = _imports_in_file(cli)
    assert not offenders, (
        "ROB-307 observe-only CLI must not import order-mutation, live-host, "
        "ledger-internal, or credential modules. Offenders:\n" + "\n".join(offenders)
    )
