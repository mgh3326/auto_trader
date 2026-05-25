"""ROB-298 — Static import guard.

No production code in ``app/`` may import from
``app.services.brokers.binance.testnet`` or ``app.services.scalping``
(both deleted in ROB-298 PR 1). Tests under ``tests/`` may not either
(no stale dead imports).

Scripts under ``scripts/`` are checked separately.
"""

from __future__ import annotations

import ast
import pathlib

_BANNED_PREFIXES = (
    "app.services.brokers.binance.testnet",
    "app.services.scalping",
)


def _module_is_banned(name: str) -> bool:
    """True when ``name`` is a banned module or a submodule of one.

    Matching is module-boundary aware: a banned prefix only matches when it is
    the whole module name or is followed by a ``.``. This keeps the deleted
    ``app.services.scalping`` package (and its submodules) banned without
    falsely catching unrelated siblings such as the ROB-315
    ``app.services.scalping_reviews`` package.
    """
    return any(name == p or name.startswith(p + ".") for p in _BANNED_PREFIXES)


def _scan(roots: list[pathlib.Path]) -> list[str]:
    offenders: list[str] = []
    for root in roots:
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and _module_is_banned(node.module):
                        offenders.append(f"{py}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _module_is_banned(alias.name):
                            offenders.append(f"{py}: import {alias.name}")
    return offenders


def test_banned_match_is_module_boundary_aware() -> None:
    """ROB-298 bans the *deleted* ``app.services.scalping`` package, but the
    match must respect module boundaries so the unrelated ROB-315
    ``app.services.scalping_reviews`` package is NOT falsely flagged."""
    # Banned: exact module and any submodule of the deleted packages.
    assert _module_is_banned("app.services.scalping")
    assert _module_is_banned("app.services.scalping.foo")
    assert _module_is_banned("app.services.brokers.binance.testnet")
    assert _module_is_banned("app.services.brokers.binance.testnet.client")
    # Allowed: sibling packages that merely share a name prefix.
    assert not _module_is_banned("app.services.scalping_reviews")
    assert not _module_is_banned("app.services.scalping_reviews.service")
    assert not _module_is_banned("app.services.brokers.binance.demo")


def test_no_testnet_imports_in_app() -> None:
    offenders = _scan([pathlib.Path("app")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_scripts() -> None:
    offenders = _scan([pathlib.Path("scripts")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_tests() -> None:
    offenders = _scan([pathlib.Path("tests")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_futures_demo_does_not_import_spot_demo() -> None:
    """ROB-298 PR 2 — Futures Demo and Spot Demo are independent adapters.

    They share only the unified ledger (binance.demo.ledger) and base
    errors (binance.errors). Any direct import between the two adapter
    packages is forbidden, except for a sanctioned cross-allowlist guard
    in futures_demo/transport.py that imports SPOT_DEMO_HOSTS to verify
    Futures Demo credentials never leak to Spot Demo endpoints.
    """
    futures_demo_root = pathlib.Path("app/services/brokers/binance/futures_demo")
    # Allowed cross-import: SPOT_DEMO_HOSTS for the futures→spot cross-allowlist guard.
    # See app/services/brokers/binance/futures_demo/transport.py (lines 46-49).
    # This is the only sanctioned runtime import from spot_demo into futures_demo.
    SANCTIONED_ALLOWLIST_CROSS_IMPORTS = {
        "app/services/brokers/binance/futures_demo/transport.py",
    }
    SANCTIONED_TARGETS = {"app.services.brokers.binance.spot_demo.host_allowlist"}

    offenders: list[str] = []
    for py in futures_demo_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "binance.spot_demo" in node.module:
                    # Skip if this is a sanctioned cross-import.
                    if (
                        str(py) in SANCTIONED_ALLOWLIST_CROSS_IMPORTS
                        and node.module in SANCTIONED_TARGETS
                    ):
                        continue
                    offenders.append(f"{py}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "binance.spot_demo" in alias.name:
                        offenders.append(f"{py}: import {alias.name}")
    assert not offenders, (
        "futures_demo must not import from spot_demo (except sanctioned host_allowlist cross-checks). Offenders:\n"
        + "\n".join(offenders)
    )


def test_spot_demo_does_not_import_futures_demo() -> None:
    """ROB-298 PR 2 — Symmetric isolation: spot_demo must not import futures_demo.

    Unlike the futures→spot direction (which has a sanctioned cross-allowlist
    guard), there is no legitimate reason for Spot Demo to import Futures Demo.
    """
    spot_demo_root = pathlib.Path("app/services/brokers/binance/spot_demo")
    offenders: list[str] = []
    for py in spot_demo_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "binance.futures_demo" in node.module:
                    offenders.append(f"{py}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "binance.futures_demo" in alias.name:
                        offenders.append(f"{py}: import {alias.name}")
    assert not offenders, (
        "spot_demo must not import from futures_demo. Offenders:\n"
        + "\n".join(offenders)
    )
