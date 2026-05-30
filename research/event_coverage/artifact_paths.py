"""ROB-371 — artifact location for the US earnings coverage probe.

Shares the ``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` env contract with the rest of
the research tooling but keeps its own ``results/`` fallback so earnings
coverage output is never mixed into the ``nautilus_scalping`` namespace. Read via
plain ``os.environ`` only — never imports app Settings (research-only boundary).

The fallback ``research/event_coverage/results/`` is gitignored: coverage
artifacts (even counts-only) must not be committed.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"


def event_coverage_artifact_root() -> Path:
    """Env root if set (non-blank), else repo-internal gitignored ``results/``."""
    raw = os.environ.get(ENV_VAR)
    if raw is not None and raw.strip():
        return Path(raw.strip())
    return Path(__file__).resolve().parent / "results"


def coverage_artifact_path(*parts: str) -> Path:
    """``<root>/event_coverage/<*parts>``."""
    return event_coverage_artifact_root().joinpath("event_coverage", *parts)
