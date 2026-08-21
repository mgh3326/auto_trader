"""ROB obs/sentry-profiling-path: pin that the four runtime processes (API,
TaskIQ worker, TaskIQ scheduler, MCP server) converge on one shared Sentry
configuration seam — ``app.monitoring.sentry.init_sentry`` reading
``settings.SENTRY_TRACES_SAMPLE_RATE`` / ``SENTRY_PROFILES_SAMPLE_RATE`` —
rather than each process hand-rolling its own sample rate or PII default.

Two layers:
1. A behavioral check: call ``init_sentry`` with the exact ``service_name`` +
   integration flags each of the four call sites uses, and assert every one
   forwards the identical settings-derived sample rates to ``sentry_sdk.init``.
2. A static source check: none of the four call sites literally pass
   ``traces_sample_rate=`` / ``profiles_sample_rate=`` / ``send_default_pii=``
   inline — that would fork the seam even though it currently reads the same
   settings object.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.monitoring.sentry as sentry_module

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact init_sentry(...) call each runtime process makes, per source.
_PROCESS_CALLS = [
    pytest.param(
        {
            "service_name": "auto-trader-api",
            "enable_fastapi": True,
            "enable_sqlalchemy": True,
            "enable_httpx": True,
        },
        id="api",
    ),
    pytest.param(
        {
            "service_name": "auto-trader-worker",
            "enable_sqlalchemy": True,
            "enable_httpx": True,
        },
        id="taskiq-worker",
    ),
    pytest.param(
        {"service_name": "auto-trader-scheduler"},
        id="taskiq-scheduler",
    ),
    pytest.param(
        {
            "service_name": "auto-trader-mcp",
            "enable_sqlalchemy": True,
            "enable_httpx": True,
            "enable_mcp": True,
        },
        id="mcp",
    ),
]


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
@pytest.mark.parametrize("call_kwargs", _PROCESS_CALLS)
def test_process_entrypoint_forwards_shared_sample_rates(monkeypatch, call_kwargs):
    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    result = sentry_module.init_sentry(**call_kwargs)

    assert result is True
    mock_init.assert_called_once()
    _, kwargs = mock_init.call_args
    assert kwargs["traces_sample_rate"] == 0.37
    assert kwargs["profiles_sample_rate"] == 0.42
    assert kwargs["send_default_pii"] is False


@pytest.mark.unit
def test_all_four_processes_agree_on_identical_sample_rates(monkeypatch):
    """Directly compare across all four call shapes in one run — the actual
    'four runtime process contracts agree' acceptance criterion."""
    observed_rate_pairs = []
    for call_kwargs in (params.values[0] for params in _PROCESS_CALLS):
        monkeypatch.setattr(sentry_module, "_initialized", False)
        monkeypatch.setattr(
            sentry_module,
            "_enabled_integration_flags",
            {"fastapi": False, "sqlalchemy": False, "httpx": False, "mcp": False},
        )
        mock_init = Mock()
        monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

        sentry_module.init_sentry(**call_kwargs)

        _, kwargs = mock_init.call_args
        observed_rate_pairs.append(
            (kwargs["traces_sample_rate"], kwargs["profiles_sample_rate"])
        )

    assert len(set(observed_rate_pairs)) == 1, observed_rate_pairs


# --- static seam-drift guard -------------------------------------------------

_PROCESS_ENTRYPOINT_FILES = [
    "app/main.py",
    "app/core/taskiq_broker.py",
    "app/mcp_server/main.py",
]

_FORBIDDEN_INLINE_KWARGS_RE = re.compile(
    r"init_sentry\([^)]*\b(traces_sample_rate|profiles_sample_rate|send_default_pii)\s*="
)


@pytest.mark.unit
def test_no_process_entrypoint_hardcodes_sample_rate_inline():
    """A process that passed traces/profiles/PII kwargs directly to
    init_sentry() would silently fork the shared settings-derived seam."""
    for relative_path in _PROCESS_ENTRYPOINT_FILES:
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        match = _FORBIDDEN_INLINE_KWARGS_RE.search(source)
        assert match is None, (
            f"{relative_path} passes {match.group(1) if match else ''} "
            "directly to init_sentry(), forking the shared config seam"
        )
