"""Non-secret Sentry collection-path diagnostics.

Separate from ``app.monitoring.sentry`` on purpose: this module's only job is
to answer "is this process's Sentry/profiling config wired the way we
expect", using values that are safe to print in a CLI, a log line, or a
support ticket. It must never read or echo ``SENTRY_DSN``, any other secret,
or an actual event/envelope payload — only the bounded allowlist below.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk

from app.core.config import settings

# The complete, allowlisted diagnostics surface. Adding a field here is a
# deliberate decision, not a side effect of forwarding more of `settings`.
DIAGNOSTICS_FIELDS: frozenset[str] = frozenset(
    {
        "process_kind",
        "sdk_version",
        "enabled",
        "traces_sample_rate",
        "profiles_sample_rate",
        "profiler_ready",
    }
)

# process_kind identifies which of this repo's known runtime processes is
# asking, nothing more — it must never become a free-text field an unknown
# caller could stuff arbitrary (or secret-shaped) text into and have it
# echoed straight back out in diagnostics output.
KNOWN_PROCESS_KINDS: frozenset[str] = frozenset(
    {
        "api",
        "taskiq-worker",
        "taskiq-scheduler",
        "mcp",
        "cli",
        "sentry-profiling-canary",
    }
)


def get_sentry_diagnostics(process_kind: str) -> dict[str, Any]:
    """Return bounded, non-secret Sentry diagnostics for one process kind.

    ``process_kind`` must be one of ``KNOWN_PROCESS_KINDS`` — an unknown
    value raises ``ValueError`` (fail closed) rather than being echoed back
    verbatim, so this function can never become a channel for leaking
    arbitrary caller-supplied text.
    """
    if process_kind not in KNOWN_PROCESS_KINDS:
        raise ValueError(
            f"unknown process_kind={process_kind!r}; must be one of "
            f"{sorted(KNOWN_PROCESS_KINDS)}"
        )

    dsn_configured = bool((settings.SENTRY_DSN or "").strip())
    traces_sample_rate = settings.SENTRY_TRACES_SAMPLE_RATE
    profiles_sample_rate = settings.SENTRY_PROFILES_SAMPLE_RATE
    # ROB obs/sentry-profiling-path: verified against installed sentry-sdk
    # 2.57.0 — the transaction profiler piggybacks on trace sampling, so
    # profiles_sample_rate > 0 alone does not produce a profile item when
    # traces_sample_rate == 0 (see tests/test_sentry_profile_envelope_contract.py
    # ::test_zero_traces_sample_rate_suppresses_profile_even_with_profiles_enabled).
    profiler_ready = (
        dsn_configured and traces_sample_rate > 0 and profiles_sample_rate > 0
    )

    diagnostics = {
        "process_kind": process_kind,
        "sdk_version": sentry_sdk.VERSION,
        "enabled": dsn_configured,
        "traces_sample_rate": traces_sample_rate,
        "profiles_sample_rate": profiles_sample_rate,
        "profiler_ready": profiler_ready,
    }
    assert set(diagnostics) == DIAGNOSTICS_FIELDS
    return diagnostics
