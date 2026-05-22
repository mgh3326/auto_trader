"""ROB-296 — Source-level audit guards for the Spot Demo sub-package.

Two top-level invariants enforced by grep over the sub-package source:

  1. **No live-Binance host literal** anywhere under
     ``app/services/brokers/binance/spot_demo/``. The Spot Demo signed
     adapter must only ever talk to ``demo-api.binance.com``. A literal
     ``api.binance.com`` or ``fapi.binance.com`` in this sub-package
     would mean a single typo could route signed Spot Demo credentials
     to live Binance.

  2. **No scheduler activation.** The Spot Demo preflight client and
     smoke CLI must NOT be referenced anywhere in
     ``app/core/scheduler.py``, ``app/core/taskiq_broker.py``, or
     ``app/tasks/``. The smoke runner is CLI-only; ROB-292 scheduler
     activation remains blocked.
"""

from __future__ import annotations

import pathlib
import re


def _repo_root() -> pathlib.Path:
    # tests/services/brokers/binance/spot_demo/test_audit_no_live_host.py
    # parents[0]=spot_demo, [1]=binance, [2]=brokers, [3]=services,
    # [4]=tests, [5]=repo root
    return pathlib.Path(__file__).resolve().parents[5]


def test_no_live_host_url_in_spot_demo_package() -> None:
    """No literal live-Binance host appears in ``binance/spot_demo/`` source.

    The Spot Demo signed adapter must never reference ``api.binance.com``
    or ``fapi.binance.com``. The Spot Testnet host
    ``testnet.binance.vision`` and the live Futures host
    ``demo-fapi.binance.com`` (ROB-291 scope) must also not appear as
    runtime literals — only inside comment-only lines that document the
    invariant.
    """
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance" / "spot_demo"
    assert pkg.exists(), f"Expected spot_demo package at {pkg}"
    # Precise-host regexes: the negative lookbehind prevents matching when the
    # candidate host is a suffix of an allowed host. For example, the literal
    # ``demo-api.binance.com`` contains the substring ``api.binance.com`` but
    # the lookbehind ``(?<![-A-Za-z0-9])`` ensures we only flag the live host
    # when it stands alone (preceded by a non-host character such as quote,
    # whitespace, slash, etc.).
    forbidden_patterns = (
        re.compile(r"(?<![-A-Za-z0-9])api\.binance\.com"),
        re.compile(r"(?<![-A-Za-z0-9])fapi\.binance\.com"),
        re.compile(r"(?<![-A-Za-z0-9])testnet\.binance\.vision"),
    )
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in pkg.rglob("*.py"):
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in forbidden_patterns:
                if pattern.search(line):
                    offenders.append((py_file, lineno, line.strip()))
                    break
    assert not offenders, (
        "Forbidden host literal(s) found inside spot_demo package: "
        f"{offenders}. ROB-296 invariant: the Spot Demo adapter must "
        "only reference demo-api.binance.com. If a docstring needs to "
        "mention another host for contrast, place it in a comment-only "
        "line or split the words."
    )


def test_no_scheduler_activation() -> None:
    """No scheduler/TaskIQ/tasks module references the Spot Demo runner.

    Scans ``app/core/scheduler.py``, ``app/core/taskiq_broker.py``, and
    every ``*.py`` under ``app/tasks/`` for references to the Spot Demo
    preflight client or the smoke script module. Any match is a scope
    breach — ROB-292 activation must remain blocked.
    """
    repo_root = _repo_root()
    needles = (
        "SpotDemoPreflightClient",
        "binance_spot_demo_smoke",
        "app.services.brokers.binance.spot_demo",
    )
    targets: list[pathlib.Path] = []
    for path in (
        repo_root / "app" / "core" / "scheduler.py",
        repo_root / "app" / "core" / "taskiq_broker.py",
    ):
        if path.exists():
            targets.append(path)
    tasks_dir = repo_root / "app" / "tasks"
    if tasks_dir.exists():
        targets.extend(tasks_dir.rglob("*.py"))
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for path in targets:
        try:
            content = path.read_text()
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for needle in needles:
                if needle in line:
                    offenders.append((path, lineno, line.strip()))
                    break
    assert not offenders, (
        "Spot Demo runner referenced from scheduler/TaskIQ/tasks modules: "
        f"{offenders}. ROB-296 invariant: the Spot Demo smoke is CLI-only. "
        "ROB-292 scheduler activation must remain blocked."
    )
