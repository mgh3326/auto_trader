#!/usr/bin/env python3
"""Publish the ``deploy-ok`` commit status and its evidence file.

hk 3445 / decision/2026-11-07/fleet-ci-three-tiers-r2 §1: the deploy tier ends
with one job that posts ``deploy-ok`` on the candidate SHA. The state is
``success`` only when every required deploy-tier job reported ``success``;
anything else (failure, cancelled, skipped, absent, unknown) is ``failure``.
The status is posted only for a commit that is an ancestor of the main ref
(any-ref dispatches on a branch write evidence but never a status).

This PR only *publishes* the status. Nothing reads it yet (rollout's
``deploy_checks`` switch is a later change), and ``scripts/deploy_ci_gate.py`` is
untouched.

stdlib only. Network access is ``gh api`` (write: commit status).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

STATUS_CONTEXT = "deploy-ok"
SUITE = "deploy"
POLICY_PATH = ".mergewake/policy.yaml"
#: GitHub rejects a status description longer than 140 characters.
DESCRIPTION_LIMIT = 140


def verdict(needs: object, required: Sequence[str]) -> tuple[str, list[str]]:
    """``(state, problems)``; state is ``success`` or ``failure``. Pure."""

    if not required:
        return "failure", ["no required deploy-tier jobs declared"]
    if not isinstance(needs, Mapping):
        return "failure", ["needs payload is not an object"]
    problems: list[str] = []
    for name in required:
        raw = needs.get(name)
        result = raw.get("result") if isinstance(raw, Mapping) else raw
        if result != "success":
            problems.append(f"{name}={result!r}")
    return ("failure" if problems else "success"), problems


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True)


def policy_revision(sha: str) -> str:
    """Git blob sha of ``.mergewake/policy.yaml`` at ``sha``, or ``absent``."""

    done = _git("rev-parse", "--verify", f"{sha}:{POLICY_PATH}")
    return done.stdout.strip() if done.returncode == 0 else "absent"


def is_main_ancestor(sha: str, main_ref: str) -> bool:
    done = _git("merge-base", "--is-ancestor", sha, main_ref)
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    raise RuntimeError(
        f"cannot tell whether {sha} is an ancestor of {main_ref}: "
        f"{done.stderr.strip() or done.returncode}"
    )


def post_status(repo: str, sha: str, state: str, description: str, url: str) -> None:
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={STATUS_CONTEXT}",
            "-f",
            f"description={description[:DESCRIPTION_LIMIT]}",
            "-f",
            f"target_url={url}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--needs-env", required=True, help="env var with toJSON(needs)")
    parser.add_argument("--required", action="append", default=[])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--evidence-out", required=True)
    args = parser.parse_args(argv)

    try:
        needs = json.loads(os.environ.get(args.needs_env, ""))
    except json.JSONDecodeError:
        needs = None
    state, problems = verdict(needs, args.required)

    evidence = {
        "repo": args.repo,
        "sha": args.sha,
        "policy_revision": policy_revision(args.sha),
        "suite": SUITE,
        "environment": args.environment,
        "run_id": args.run_id,
        "result": state,
        "image_digest": args.image_digest,
    }
    Path(args.evidence_out).write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))

    try:
        publish = is_main_ancestor(args.sha, args.main_ref)
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if not publish:
        print(
            f"{args.sha} is not an ancestor of {args.main_ref}: no {STATUS_CONTEXT} "
            "status is posted (evidence only)"
        )
        return 0
    description = (
        f"deploy tier {state}"
        if state == "success"
        else f"deploy tier failed: {', '.join(problems)}"
    )
    post_status(args.repo, args.sha, state, description, args.run_url)
    print(f"posted {STATUS_CONTEXT}={state} on {args.sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
