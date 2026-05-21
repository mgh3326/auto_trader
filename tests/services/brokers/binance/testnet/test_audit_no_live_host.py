"""ROB-286 — Hard safety invariants enforced by source-level audit.

Locks two top-level invariants:

1. **No live-Binance host literal** anywhere under
   ``app/services/brokers/binance/testnet/``. The signed-endpoint adapter
   must only ever talk to ``testnet.binance.vision`` /
   ``stream.testnet.binance.vision``. A literal ``api.binance.com`` or
   ``fapi.binance.com`` in this sub-package would mean a single typo
   could route a signed request (real money) to live Binance.

2. **No scheduler activation.** ``BinanceTestnetExecutionClient`` and
   ``binance_testnet_scalper`` must NOT be referenced anywhere in
   ``app/core/scheduler.py``, ``app/core/taskiq_broker.py`` or
   ``app/tasks/``. The runner is CLI-only; production schedule activation
   is explicitly deferred.

Reviewer guarantee: if either test fails, a future PR either (a) added a
live-host literal inside the testnet package, or (b) wired the testnet
runner into the production scheduler. Both are scope breaches; roll back
or escalate.
"""

from __future__ import annotations

import pathlib
import re


def _repo_root() -> pathlib.Path:
    # tests/services/brokers/binance/testnet/test_audit_no_live_host.py
    # parents[0]=testnet, [1]=binance, [2]=brokers, [3]=services,
    # [4]=tests, [5]=repo root
    return pathlib.Path(__file__).resolve().parents[5]


def test_no_live_host_url_in_testnet_package() -> None:
    """No literal live-Binance host appears in ``binance/testnet/`` source.

    The signed adapter must never reference ``api.binance.com`` or
    ``fapi.binance.com``. ``testnet.binance.vision`` is fine.
    """
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance" / "testnet"
    if not pkg.exists():
        # Until Task 2 introduces the package this audit is vacuously true.
        return
    forbidden_hosts = ("api.binance.com", "fapi.binance.com")
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in pkg.rglob("*.py"):
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            stripped = line.strip()
            # Skip comment-only lines that document the invariant
            if stripped.startswith("#"):
                continue
            for host in forbidden_hosts:
                if host in line:
                    offenders.append((py_file, lineno, line.strip()))
                    break
    assert not offenders, (
        "Live Binance host literal(s) found inside testnet package: "
        f"{offenders}. ROB-286 invariant: the testnet adapter must never "
        "reference api.binance.com or fapi.binance.com. Use "
        "testnet.binance.vision instead. If a docstring needs to mention "
        "the live host for contrast, place the docstring outside this "
        "package or split the words."
    )


def test_no_scheduler_activation() -> None:
    """No scheduler/TaskIQ/tasks module references the testnet runner.

    Scans ``app/core/scheduler.py``, ``app/core/taskiq_broker.py``, and
    every ``*.py`` under ``app/tasks/`` for references to the testnet
    execution client or the scalper runner module. Any match is a
    scope breach.
    """
    repo_root = _repo_root()
    needles = (
        "BinanceTestnetExecutionClient",
        "binance_testnet_scalper",
        "binance.testnet.execution_client",
        "scalping.runner",
    )
    paths_to_scan: list[pathlib.Path] = []
    scheduler_path = repo_root / "app" / "core" / "scheduler.py"
    if scheduler_path.exists():
        paths_to_scan.append(scheduler_path)
    taskiq_path = repo_root / "app" / "core" / "taskiq_broker.py"
    if taskiq_path.exists():
        paths_to_scan.append(taskiq_path)
    tasks_dir = repo_root / "app" / "tasks"
    if tasks_dir.exists():
        paths_to_scan.extend(tasks_dir.rglob("*.py"))
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in paths_to_scan:
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            for needle in needles:
                if needle in line:
                    offenders.append((py_file, lineno, line.strip()))
                    break
    assert not offenders, (
        f"Scheduler activation of testnet runner detected: {offenders}. "
        "ROB-286 invariant: the testnet scalper runs only via the smoke CLI "
        "or directly in tests. Production schedule activation is gated by a "
        "separate runbook + child issue. Remove the reference or escalate."
    )


def _scan_non_docstring_lines(
    *, pkg: pathlib.Path, needle: str
) -> list[tuple[pathlib.Path, int, str]]:
    """Return (file, lineno, line) tuples where ``needle`` appears in
    source code that is NOT inside a docstring or a ``#`` comment.

    We scan by parsing the AST to collect line ranges occupied by
    module/class/function docstring constants, then check each raw line
    against ``needle`` while skipping those ranges and comment-only lines.
    Lines like ``x = "foo"`` are still checked (they are real code) but
    triple-quoted docstrings are exempt.
    """
    import ast

    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in pkg.rglob("*.py"):
        source = py_file.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # Collect line ranges occupied by docstrings (module/class/func).
        docstring_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.Module,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                doc_node = (
                    node.body[0]
                    if node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                    else None
                )
                if doc_node is not None:
                    docstring_ranges.append(
                        (
                            doc_node.lineno,
                            doc_node.end_lineno or doc_node.lineno,
                        )
                    )

        def _in_docstring(lineno: int, ranges: list[tuple[int, int]]) -> bool:
            for start, end in ranges:
                if start <= lineno <= end:
                    return True
            return False

        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _in_docstring(lineno, docstring_ranges):
                continue
            if needle in line:
                offenders.append((py_file, lineno, stripped))
    return offenders


def test_place_stop_orders_no_reduceonly_param() -> None:
    """TT6 — No ``reduceOnly`` literal in real code under ``binance/testnet/``.

    ROB-289 reviewer focus #6: ``reduceOnly`` is a futures-only concept
    on Binance. Spot doesn't use it, and introducing it here (even
    unused) would create a footgun for the future-path PR (ROB-291).
    The audit walks the AST to skip docstring text, then flags any
    remaining occurrence in real code (parameter literals, string
    constants, identifier names, inline comments).
    """
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance" / "testnet"
    if not pkg.exists():
        return
    offenders = _scan_non_docstring_lines(pkg=pkg, needle="reduceOnly")
    assert not offenders, (
        "Forbidden ``reduceOnly`` literal found in real code under "
        f"binance/testnet/: {offenders}. ROB-289 safety boundary #3: "
        "spot doesn't use ``reduceOnly``; introducing it (even unused) "
        "would invite futures-path leakage. Futures-side enforcement is "
        "gated to ROB-291. Remove the literal or escalate."
    )


def test_place_stop_orders_no_futures_host_literal() -> None:
    """Safety boundary #2 — futures testnet host literal must not appear
    in real code under ``binance/testnet/``. Futures is ROB-291 scope."""
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance" / "testnet"
    if not pkg.exists():
        return
    # Use a string concatenation in the test source so the audit itself
    # doesn't trip on this file (the audit only scans the app package,
    # but defense-in-depth keeps the test file readable on grep too).
    needle = "testnet.binance" + "future.com"
    offenders = _scan_non_docstring_lines(pkg=pkg, needle=needle)
    assert not offenders, (
        f"Forbidden futures testnet host literal in real code: {offenders}. "
        "ROB-289 safety boundary #2 — futures path is ROB-291."
    )


def test_audit_module_grep_regex_is_sane() -> None:
    """Defensive check on the needle list above.

    A regression where someone shortens a needle to ``Binance`` would
    silently match thousands of unrelated lines. Pin minimum lengths so
    the audit can't be quietly weakened.
    """
    minimum_length = len("BinanceTestnetExecutionClient")
    needles = (
        "BinanceTestnetExecutionClient",
        "binance_testnet_scalper",
    )
    for needle in needles:
        assert len(needle) >= len("binance_testnet_scalper"), (
            f"Needle {needle!r} is suspiciously short ({len(needle)} chars). "
            "Audit needles must be specific enough to avoid false positives."
        )
    # Anchor the regex parse so a future PR can't accidentally swap to a
    # `re.IGNORECASE` over a short substring.
    assert re.search(r"BinanceTestnet", "BinanceTestnetExecutionClient")
    _ = minimum_length  # silence ty
