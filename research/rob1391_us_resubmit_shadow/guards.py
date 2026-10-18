"""Run-time guards checked at the start of every command.

* write-path reachability: if any order / proposal / watch / ledger-writer /
  broker-client module is loaded in this process, the experiment cannot
  claim isolation -> operational latch (prereg §4 운영 중단).
* policy: the deployed-policy ``{version, content_hash}`` must equal the
  sealed one, else the epoch ends ``STOPPED_BY_POLICY_CHANGE``.
* code: the git SHA must equal the sealed SHA (refusal, not a latch -- a
  different checkout is an operator mistake, not an experiment event).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Prefixes that must never be importable-in-process while the experiment runs.
FORBIDDEN_RUNTIME_PREFIXES: tuple[str, ...] = (
    "app.services.order_proposals",
    "app.services.brokers",
    "app.services.execution_ledger",
    "app.services.kis",
    "app.services.toss",
    "app.services.upbit",
    "app.services.alpaca",
    "app.services.kiwoom",
    "app.services.nhplug",
    "app.services.binance",
    "app.services.live_",
    "app.services.investment_watch",
    "app.services.order",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.jobs",
    "alpaca.trading",
    "alpaca.broker",
)


def loaded_forbidden_modules(modules: dict | None = None) -> list[str]:
    """Plain prefix match on purpose: over-matching fails closed."""

    names = (modules if modules is not None else sys.modules).keys()
    return sorted(
        name
        for name in names
        if any(name.startswith(p) for p in FORBIDDEN_RUNTIME_PREFIXES)
    )


def current_policy_stamp() -> dict[str, str]:
    from app.services.trading_policy_service import policy_version_stamp

    stamp = policy_version_stamp()
    return {
        "version": str(stamp["version"]),
        "content_hash": str(stamp["content_hash"]),
    }


class CodeShaError(RuntimeError):
    pass


def git_code_sha(root: Path = REPO_ROOT) -> str:
    """HEAD SHA of a clean checkout; refuses a dirty tracked tree."""

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CodeShaError(f"cannot read git state: {type(exc).__name__}") from None
    if dirty:
        raise CodeShaError("tracked files are modified; run from a clean checkout")
    return sha
