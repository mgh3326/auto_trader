"""ROB obs/sentry-profiling-path: pin that the four runtime processes (API,
TaskIQ worker, TaskIQ scheduler, MCP server) converge on one shared Sentry
configuration seam — ``app.monitoring.sentry.init_sentry`` reading
``settings.SENTRY_TRACES_SAMPLE_RATE`` / ``SENTRY_PROFILES_SAMPLE_RATE`` —
rather than each process hand-rolling its own sample rate or PII default.

The call shapes below are extracted from the actual entrypoint source via
``ast`` (not hand-copied literals) specifically so that deleting or changing
a process's ``init_sentry(...)`` call makes this test fail loudly (no match
found / kwargs changed) instead of silently staying green against a stale
hardcoded expectation.

Three layers:
1. Static AST extraction + count: exactly the four expected
   ``init_sentry(...)`` calls (1 in app/main.py, 2 in
   app/core/taskiq_broker.py [worker + scheduler branches], 1 in
   app/mcp_server/main.py) must still exist with the expected service names.
2. A behavioral check: replay each *extracted* call's kwargs through the
   real ``init_sentry`` and assert every one forwards the identical
   settings-derived sample rates to ``sentry_sdk.init``.
3. A static source check: none of the four call sites literally pass
   ``traces_sample_rate=`` / ``profiles_sample_rate=`` / ``send_default_pii=``
   inline — that would fork the seam even though it currently reads the same
   settings object.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.monitoring.sentry as sentry_module

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PROCESS_ENTRYPOINT_FILES = [
    "app/main.py",
    "app/core/taskiq_broker.py",
    "app/mcp_server/main.py",
]

_EXPECTED_SERVICE_NAMES_BY_FILE = {
    "app/main.py": {"auto-trader-api"},
    "app/core/taskiq_broker.py": {"auto-trader-worker", "auto-trader-scheduler"},
    "app/mcp_server/main.py": {"auto-trader-mcp"},
}


def _literal_or_none(node: ast.expr):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _extract_init_sentry_calls(relative_path: str) -> list[dict]:
    """Return kwargs dicts for every top-level ``init_sentry(...)`` call
    (direct-name call, as every entrypoint imports it unaliased) found in
    ``relative_path`` via AST — never a hand-maintained duplicate."""
    source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)

    calls: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_init_sentry_call = (
            isinstance(func, ast.Name) and func.id == "init_sentry"
        ) or (isinstance(func, ast.Attribute) and func.attr == "init_sentry")
        if not is_init_sentry_call:
            continue
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg is None:  # **kwargs spread — not used by any call site
                continue
            kwargs[keyword.arg] = _literal_or_none(keyword.value)
        if len(node.args) >= 1:
            kwargs.setdefault("service_name", _literal_or_none(node.args[0]))
        calls.append(kwargs)
    return calls


@pytest.fixture(autouse=True)
def _reset_sentry_state(monkeypatch):
    monkeypatch.setattr(sentry_module, "_initialized", False)
    monkeypatch.setattr(
        sentry_module,
        "_enabled_integration_flags",
        {"fastapi": False, "sqlalchemy": False, "httpx": False, "mcp": False},
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_DSN", "fake-dsn-not-a-secret")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENVIRONMENT", "test")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", 0.37)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_PROFILES_SAMPLE_RATE", 0.42)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_SEND_DEFAULT_PII", False)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", False)


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _PROCESS_ENTRYPOINT_FILES)
def test_entrypoint_still_calls_init_sentry_with_expected_service_names(
    relative_path,
):
    """Fails loudly if a process's init_sentry(...) call is deleted, renamed,
    or its service_name changed without updating this contract."""
    calls = _extract_init_sentry_calls(relative_path)
    found_service_names = {call.get("service_name") for call in calls}
    expected = _EXPECTED_SERVICE_NAMES_BY_FILE[relative_path]
    assert found_service_names == expected, (
        f"{relative_path}: expected init_sentry() service_name(s) {expected}, "
        f"found {found_service_names} — the process entrypoint contract changed"
    )


@pytest.mark.unit
def test_all_extracted_process_calls_forward_identical_shared_sample_rates(
    monkeypatch,
):
    """Replay every init_sentry(...) call actually present in the four
    process entrypoints and assert they all forward the identical
    settings-derived sample rates to sentry_sdk.init — the 'four runtime
    process contracts agree' acceptance criterion, driven by real source."""
    all_calls: list[dict] = []
    for relative_path in _PROCESS_ENTRYPOINT_FILES:
        all_calls.extend(_extract_init_sentry_calls(relative_path))

    assert len(all_calls) == 4, (
        f"expected exactly 4 init_sentry() calls across the process "
        f"entrypoints, found {len(all_calls)}: {all_calls}"
    )

    observed_rate_pairs = []
    for call_kwargs in all_calls:
        monkeypatch.setattr(sentry_module, "_initialized", False)
        monkeypatch.setattr(
            sentry_module,
            "_enabled_integration_flags",
            {"fastapi": False, "sqlalchemy": False, "httpx": False, "mcp": False},
        )
        mock_init = Mock()
        monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

        result = sentry_module.init_sentry(**call_kwargs)

        assert result is True, call_kwargs
        mock_init.assert_called_once()
        _, kwargs = mock_init.call_args
        assert kwargs["traces_sample_rate"] == 0.37
        assert kwargs["profiles_sample_rate"] == 0.42
        assert kwargs["send_default_pii"] is False
        observed_rate_pairs.append(
            (kwargs["traces_sample_rate"], kwargs["profiles_sample_rate"])
        )

    assert len(set(observed_rate_pairs)) == 1, observed_rate_pairs


# --- static seam-drift guard -------------------------------------------------

_FORBIDDEN_INLINE_KWARGS_RE = re.compile(
    r"init_sentry\([^)]*\b(traces_sample_rate|profiles_sample_rate|send_default_pii)\s*="
)


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _PROCESS_ENTRYPOINT_FILES)
def test_entrypoint_does_not_hardcode_sample_rate_inline(relative_path):
    """A process that passed traces/profiles/PII kwargs directly to
    init_sentry() would silently fork the shared settings-derived seam."""
    source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    match = _FORBIDDEN_INLINE_KWARGS_RE.search(source)
    assert match is None, (
        f"{relative_path} passes {match.group(1) if match else ''} "
        "directly to init_sentry(), forking the shared config seam"
    )
