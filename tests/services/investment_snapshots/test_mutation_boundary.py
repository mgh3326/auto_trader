"""ROB-269 Phase 2 — Static mutation-boundary guard.

Reads every Phase 2-touched source file and asserts:

* No HTTP client imports (``httpx``, ``aiohttp``, ``requests``,
  ``urllib.request``). Phase 2 has no live external surface; collectors
  are a future Phase 3 entry point.
* No imports of broker/order/watch-intent service classes that could be
  used to perform mutations against live or paper accounts.
* No identifier references to ``submit_*`` / ``cancel_*`` / ``modify_*``
  method calls (broker mutation verbs).

This is a deliberately conservative static scan. False positives can be
bypassed with a ``# noqa: rob-269-boundary`` marker on the offending line,
but the marker must be the only way to opt out — there is no implicit
allow-list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Files in scope. Order doesn't matter; we read each and run the checks.
_SCOPED_FILES: list[str] = [
    "app/services/analysis_snapshot_bundle/capture.py",
    "app/services/investment_snapshots/repository.py",
    "app/services/investment_snapshots/collectors.py",
    "app/services/investment_snapshots/freshness.py",
    "app/services/investment_snapshots/policy.py",
    "app/services/investment_snapshots/read_service.py",
    "app/services/investment_snapshots/refresh_request_service.py",
    "app/services/action_report/common/snapshot_bundle.py",
    "app/services/action_report/common/canonicalize.py",
    # ROB-269 Phase 3 — stale gate + lifted us_action_report. The lifted
    # files contain ``_FORBIDDEN_LIVE_ORDER_METHODS = ("submit_order", ...)``
    # as a string-literal allowlist (used elsewhere to *reject* dangerous
    # client method calls). The boundary regex requires a literal ``(``
    # after the verb to fire, so those tuple entries don't trip it.
    "app/services/action_report/common/stale_gate.py",
    "app/services/action_report/common/generator_constraints.py",
    "app/services/action_report/common/critical_kinds.py",
    "app/services/action_report/common/bundle_aware_publishing.py",
    "app/services/action_report/us/__init__.py",
    "app/services/action_report/us/account_snapshot.py",
    "app/services/action_report/us/action_classifier.py",
    "app/services/action_report/us/new_buy_candidates.py",
    "app/services/action_report/us/discord_formatter.py",
    "app/services/action_report/us/order_preview.py",
    "app/schemas/investment_snapshots.py",
    "app/schemas/investment_snapshots_mcp.py",
    "app/mcp_server/tooling/investment_snapshots_tools.py",
    "app/mcp_server/tooling/investment_snapshots_registration.py",
    "app/routers/investment_snapshots.py",
    # ROB-269 Phase 4 — Prefect snapshot-refresh flow scaffold (importable
    # only, no deployment registered). The flow wraps Phase 2's ensure
    # service; safety guarantees flow through automatically.
    "app/flows/investment_snapshots_refresh_flow.py",
]

_ANALYSIS_CAPTURE_FORBIDDEN_IMPORT_NAMES = {
    "place_order",
    "cancel_order",
    "modify_order",
    "order_proposal",
    "investment_report_create",
    "watch_create",
    "execution_service",
}

# Lines containing this marker are exempted from boundary checks.
_BYPASS_MARKER = "rob-269-boundary"

# HTTP clients — Phase 2 has no live external surface.
_HTTP_IMPORT_PATTERNS = [
    re.compile(r"^\s*import\s+httpx\b"),
    re.compile(r"^\s*from\s+httpx\b"),
    re.compile(r"^\s*import\s+aiohttp\b"),
    re.compile(r"^\s*from\s+aiohttp\b"),
    re.compile(r"^\s*import\s+requests\b"),
    re.compile(r"^\s*from\s+requests\b"),
    re.compile(r"^\s*from\s+urllib\.request\b"),
    re.compile(r"^\s*import\s+urllib\.request\b"),
]

# Service classes whose import indicates we may mutate broker/order/watch state.
_FORBIDDEN_SERVICE_NAMES = {
    "KISTradingService",
    "OrderExecutionService",
    "AlpacaPaperOrdersService",
    "WatchActivationService",
    "TradeJournalWriteService",
}

# Broker mutation verb identifiers used as method *calls* (followed by ``(``).
# These match against bare identifiers; bound .calls are also covered by the
# regex (``.cancel_order(`` etc.).
_FORBIDDEN_VERB_PATTERNS = [
    re.compile(r"\bsubmit_order\b\s*\("),
    re.compile(r"\bcancel_order\b\s*\("),
    re.compile(r"\bmodify_order\b\s*\("),
    re.compile(r"\bplace_order\b\s*\("),
    re.compile(r"\bcreate_watch_intent\b\s*\("),
]


def _read(file: str) -> list[str]:
    path = REPO_ROOT / file
    if not path.exists():  # pragma: no cover — surface-level guard
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _lines_without_bypass(lines: list[str]) -> list[tuple[int, str]]:
    return [(i + 1, line) for i, line in enumerate(lines) if _BYPASS_MARKER not in line]


@pytest.mark.parametrize("rel_path", _SCOPED_FILES)
def test_no_http_client_imports(rel_path: str) -> None:
    """Phase 2 source must not import live HTTP clients."""
    violations: list[str] = []
    for lineno, line in _lines_without_bypass(_read(rel_path)):
        for pattern in _HTTP_IMPORT_PATTERNS:
            if pattern.match(line):
                violations.append(f"{rel_path}:{lineno}: {line.strip()}")
                break
    assert violations == [], (
        "Phase 2 must have no HTTP client imports. Violations:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize("rel_path", _SCOPED_FILES)
def test_no_forbidden_service_imports(rel_path: str) -> None:
    """Phase 2 source must not import broker/order/watch mutation services."""
    violations: list[str] = []
    for lineno, line in _lines_without_bypass(_read(rel_path)):
        for forbidden in _FORBIDDEN_SERVICE_NAMES:
            # Match in any context (from X import Y, including aliases, attribute use).
            if re.search(rf"\b{re.escape(forbidden)}\b", line):
                violations.append(
                    f"{rel_path}:{lineno}: forbidden name {forbidden!r}: {line.strip()}"
                )
                break
    assert violations == [], (
        "Phase 2 must not reference broker/order/watch mutation service classes. "
        "Violations:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("rel_path", _SCOPED_FILES)
def test_no_broker_mutation_verb_calls(rel_path: str) -> None:
    """Phase 2 source must not call submit_/cancel_/modify_/place_ verbs."""
    violations: list[str] = []
    for lineno, line in _lines_without_bypass(_read(rel_path)):
        for pattern in _FORBIDDEN_VERB_PATTERNS:
            if pattern.search(line):
                violations.append(f"{rel_path}:{lineno}: {line.strip()}")
                break
    assert violations == [], (
        "Phase 2 must not call broker mutation verbs (submit_/cancel_/modify_/place_)."
        " Violations:\n" + "\n".join(violations)
    )


def test_scoped_file_list_is_non_empty_and_all_exist() -> None:
    """Catch regressions where files were renamed/removed without updating this test."""
    missing = [f for f in _SCOPED_FILES if not (REPO_ROOT / f).exists()]
    assert missing == [], f"Scoped files missing: {missing}"
    assert len(_SCOPED_FILES) >= 10, "Boundary scope shrunk — investigate."


def test_analysis_capture_has_no_mutation_boundary_imports() -> None:
    lines = _read("app/services/analysis_snapshot_bundle/capture.py")
    imports = [line for line in lines if line.lstrip().startswith(("import ", "from "))]
    violations = [
        line
        for line in imports
        if any(name in line for name in _ANALYSIS_CAPTURE_FORBIDDEN_IMPORT_NAMES)
    ]
    assert violations == []
