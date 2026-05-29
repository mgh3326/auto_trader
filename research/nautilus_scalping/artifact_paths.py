"""ROB-339 (D2) — single source of truth for research artifact locations.

``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` (opt-in) points artifacts at a path outside
the git tree; unset (or whitespace) falls back to the repo-internal ``results/``
so zero-config dev and CI keep working. Read via plain ``os.environ`` only — this
module must never import ``app`` Settings/pydantic (research-only boundary).

Namespace separation keeps non-canonical discovery output (``discovery/``) from
being mistaken for the gate's citable run-cards (``gate/``).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"
_NAMESPACES = frozenset({"discovery", "gate"})


def research_artifact_root() -> Path:
    """Resolve the artifact root: env if set (non-blank), else repo ``results/``."""
    raw = os.environ.get(ENV_VAR)
    if raw is not None and raw.strip():
        return Path(raw.strip())
    return Path(__file__).resolve().parent / "results"


def resolve_artifact_path(namespace: str, *parts: str) -> Path:
    """``root / namespace / *parts``; ``namespace`` must be a known namespace."""
    if namespace not in _NAMESPACES:
        raise ValueError(
            f"unknown artifact namespace {namespace!r}; expected one of {sorted(_NAMESPACES)}"
        )
    return research_artifact_root().joinpath(namespace, *parts)


def pit_data_root() -> Path:
    """Raw-data root for downloaded klines (gitignored). Distinct from
    ``resolve_artifact_path`` (citable discovery/gate outputs). Env if set
    (non-blank), else repo-internal ``data/`` (matched by ``.gitignore``)."""
    raw = os.environ.get(ENV_VAR)
    base = Path(raw.strip()) if raw is not None and raw.strip() else Path(__file__).resolve().parent
    return base / "data"
