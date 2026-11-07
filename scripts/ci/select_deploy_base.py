#!/usr/bin/env python3
"""Pick the "deployed" schema revision for the deploy tier's migration check.

hk 3445 / decision/2026-11-07/fleet-ci-three-tiers-r2 §1: the deploy tier runs
``alembic upgrade head`` at a base checkout, then the candidate's
``alembic upgrade head`` on the same database. The base is the newest
first-parent main ancestor of the candidate that carries a ``deploy-ok`` commit
status with state ``success``. Until the first ``deploy-ok`` exists the base is
the pinned bootstrap commit (``--bootstrap``): production ran sha-0c416a3 (deploy
record ``deploy/auto_trader/20261106T230040Z``) when the deploy tier was added.

Fail-closed: a status read that errors, or a candidate that is older than the
bootstrap and has no ``deploy-ok`` ancestor, exits non-zero. Nothing here
falls back to the candidate itself.

stdlib only. Network access is ``gh api`` (read-only).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable

STATUS_CONTEXT = "deploy-ok"
#: First-parent commits examined before giving up on finding the bootstrap.
WALK_LIMIT = 100


class BaseSelectionError(RuntimeError):
    """No trustworthy base. The caller must go red, never guess."""


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], check=False, capture_output=True, text=True)
    if done.returncode != 0:
        raise BaseSelectionError(
            f"git {' '.join(args)} failed: {done.stderr.strip() or done.returncode}"
        )
    return done.stdout.strip()


def _git_ok(*args: str) -> bool:
    return (
        subprocess.run(
            ["git", *args], check=False, capture_output=True, text=True
        ).returncode
        == 0
    )


def has_deploy_ok(repo: str, sha: str) -> bool:
    """True when the combined status of ``sha`` carries a successful deploy-ok."""

    done = subprocess.run(
        ["gh", "api", "--method", "GET", f"repos/{repo}/commits/{sha}/status"],
        check=False,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise BaseSelectionError(
            f"cannot read the commit status of {sha}: "
            f"{done.stderr.strip() or done.returncode}"
        )
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise BaseSelectionError(f"status of {sha} is not JSON: {exc}") from exc
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, list):
        raise BaseSelectionError(f"status of {sha} has no `statuses` list")
    return any(
        isinstance(item, dict)
        and item.get("context") == STATUS_CONTEXT
        and item.get("state") == "success"
        for item in statuses
    )


def select_base(
    *,
    repo: str,
    sha: str,
    bootstrap: str,
    main_ref: str,
    status_probe: Callable[[str, str], bool] = has_deploy_ok,
) -> tuple[str, str]:
    """Return ``(base_sha, source)`` with source ``deploy-ok`` or ``bootstrap``."""

    candidate = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
    boot = _git("rev-parse", "--verify", f"{bootstrap}^{{commit}}")
    # Walk main, not the candidate's own branch: only main commits can carry a
    # deploy-ok. A main-push candidate is its own merge-base, so skip it.
    merge_base = _git("merge-base", candidate, main_ref)
    chain = _git(
        "rev-list", "--first-parent", f"--max-count={WALK_LIMIT}", merge_base
    ).split()
    if chain and chain[0] == candidate:
        chain = chain[1:]
    for commit in chain:
        if status_probe(repo, commit):
            return commit, STATUS_CONTEXT
        if commit == boot:
            return boot, "bootstrap"
    if _git_ok("merge-base", "--is-ancestor", boot, candidate):
        # Walk limit reached before the bootstrap: it is still a valid ancestor.
        return boot, "bootstrap"
    raise BaseSelectionError(
        f"{candidate} has no deploy-ok first-parent ancestor on {main_ref} and "
        f"the bootstrap {boot} is not its ancestor"
    )


def _write_output(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", required=True, help="candidate commit")
    parser.add_argument(
        "--bootstrap",
        required=True,
        help="pinned base used until a deploy-ok exists",
    )
    parser.add_argument("--main-ref", default="origin/main")
    args = parser.parse_args(argv)
    try:
        base, source = select_base(
            repo=args.repo,
            sha=args.sha,
            bootstrap=args.bootstrap,
            main_ref=args.main_ref,
        )
    except BaseSelectionError as exc:
        print(f"::error::deploy base: {exc}", file=sys.stderr)
        return 1
    print(f"deploy base {base} ({source})")
    _write_output("base_sha", base)
    _write_output("base_source", source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
