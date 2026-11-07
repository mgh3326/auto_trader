#!/usr/bin/env python3
"""Pick the deployed schema revision for the deploy tier's migration check.

hk 3445 / decision/2026-11-07/fleet-ci-three-tiers-r2 §1: the deploy tier checks
the migration path from the schema that is deployed to the candidate. It runs
``alembic upgrade head`` at a base checkout, then the candidate's
``alembic upgrade head`` on the same database.

The base is the pinned production commit (``--deployed``). It is not derived
from ``deploy-ok``: that status only says CI validated an image. Production
deploys are run by an operator and publish nothing GitHub can read, so a commit
with ``deploy-ok`` may never have been deployed. The pin moves forward only in a
PR that names the deploy record of the new production commit. A pin older than
production still runs every migration production has not applied yet.

Fail-closed: the pin must be a commit and an ancestor of the candidate (or the
candidate itself). Otherwise this exits non-zero. Nothing here falls back to
the candidate.

stdlib only. No network access.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

SOURCE = "pinned-deployed"


class BaseSelectionError(RuntimeError):
    """No trustworthy base. The caller must go red, never guess."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True)


def _commit(ref: str) -> str:
    done = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if done.returncode != 0:
        raise BaseSelectionError(
            f"{ref} is not a commit: {done.stderr.strip() or done.returncode}"
        )
    return done.stdout.strip()


def select_base(*, sha: str, deployed: str) -> tuple[str, str]:
    """Return ``(base_sha, source)``."""

    candidate = _commit(sha)
    base = _commit(deployed)
    done = _git("merge-base", "--is-ancestor", base, candidate)
    if done.returncode == 1:
        raise BaseSelectionError(
            f"the deployed commit {base} is not an ancestor of {candidate}"
        )
    if done.returncode != 0:
        raise BaseSelectionError(
            f"cannot tell whether {base} is an ancestor of {candidate}: "
            f"{done.stderr.strip() or done.returncode}"
        )
    return base, SOURCE


def _write_output(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="candidate commit")
    parser.add_argument(
        "--deployed", required=True, help="pinned commit that production runs"
    )
    args = parser.parse_args(argv)
    try:
        base, source = select_base(sha=args.sha, deployed=args.deployed)
    except BaseSelectionError as exc:
        print(f"::error::deploy base: {exc}", file=sys.stderr)
        return 1
    print(f"deploy base {base} ({source})")
    _write_output("base_sha", base)
    _write_output("base_source", source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
