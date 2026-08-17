#!/usr/bin/env python3
"""Exact-head source-ownership fence checker.

Evaluates only the exact change set under review: an explicit ``base``
commit and an explicit ``head`` commit, both supplied by the caller (CI
event payload or an explicit override) — never a fixed historical SHA
compared against an indefinitely advancing branch. That comparison is what
made ``test_scope_actual_job_diff_stays_inside_the_write_fence`` red on
every unrelated merge to ``main`` and, worse, let CI's shallow checkout
silently ``pytest.skip`` the check instead of failing it.

Every failure mode here is a hard failure (nonzero exit): an unresolvable
base or head commit, a failing ``git diff``, or a changed path outside the
declared allow-list. None of these are ever downgraded to a skip, a
warning, or a success. Run with ``-h`` for usage.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


class FenceCheckError(RuntimeError):
    """Any condition that must fail the check, never be silently skipped."""


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_commit(repo_root: Path, sha: str) -> None:
    """Ensure ``sha`` is a locally reachable commit, fetching if necessary.

    Raises FenceCheckError (never returns a sentinel) if the commit cannot
    be made reachable — a shallow checkout missing the object is a hard
    failure here, not a reason to skip the check.
    """

    probe = _run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
    if probe.returncode == 0:
        return

    fetch = _run_git(repo_root, "fetch", "--no-tags", "--depth", "100", "origin", sha)
    if fetch.returncode == 0:
        probe = _run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
        if probe.returncode == 0:
            return

    unshallow = _run_git(repo_root, "fetch", "--unshallow", "origin")
    if unshallow.returncode == 0:
        probe = _run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
        if probe.returncode == 0:
            return

    raise FenceCheckError(
        f"commit {sha!r} is not reachable locally and could not be fetched "
        f"(git fetch stderr: {fetch.stderr.strip() or unshallow.stderr.strip()!r}). "
        "This is a hard failure, not a skip."
    )


def _changed_paths(repo_root: Path, base: str, head: str) -> set[str]:
    diff = _run_git(repo_root, "diff", "--name-only", "-z", f"{base}..{head}")
    if diff.returncode != 0:
        raise FenceCheckError(
            f"`git diff --name-only {base}..{head}` failed (exit "
            f"{diff.returncode}): {diff.stderr.strip()!r}. This is a hard "
            "failure, not a skip."
        )
    return {field for field in diff.stdout.split("\0") if field}


def _load_allow_list(args: argparse.Namespace) -> set[str]:
    allow: set[str] = set(args.allow or ())
    if args.allow_file:
        text = Path(args.allow_file).read_text(encoding="utf-8")
        allow |= {
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    if not allow:
        raise FenceCheckError(
            "no allow-list entries given (--allow / --allow-file); refusing "
            "to run an unbounded fence check."
        )
    return allow


def _resolve_sha(cli_value: str | None, env_name: str) -> str:
    value = cli_value or os.environ.get(env_name)
    if not value:
        raise FenceCheckError(
            f"no commit given: pass the CLI flag or set ${env_name}. This "
            "script never falls back to a default ref (e.g. `origin/main` "
            "or `HEAD`) — that is exactly the moving-base bug being fixed."
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", help="exact base commit (else $FENCE_BASE_SHA)")
    parser.add_argument("--head-sha", help="exact head commit (else $FENCE_HEAD_SHA)")
    parser.add_argument(
        "--allow", action="append", help="an allowed repo-relative path (repeatable)"
    )
    parser.add_argument(
        "--allow-file", help="file with one allowed repo-relative path per line"
    )
    parser.add_argument(
        "--exempt-prefix",
        action="append",
        default=[],
        help="changed paths starting with this prefix are ignored (repeatable)",
    )
    parser.add_argument(
        "--repo-root", default=".", help="repository root (default: cwd)"
    )
    parser.add_argument("--json-out", help="write a machine-readable report here")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    report: dict[str, object] = {
        "base_sha": None,
        "head_sha": None,
        "allow_list": None,
        "exempt_prefixes": args.exempt_prefix,
        "changed_paths": None,
        "offending_paths": None,
        "result": "error",
        "error": None,
    }

    try:
        base_sha = _resolve_sha(args.base_sha, "FENCE_BASE_SHA")
        head_sha = _resolve_sha(args.head_sha, "FENCE_HEAD_SHA")
        report["base_sha"] = base_sha
        report["head_sha"] = head_sha

        allow_list = _load_allow_list(args)
        report["allow_list"] = sorted(allow_list)

        _resolve_commit(repo_root, base_sha)
        _resolve_commit(repo_root, head_sha)

        changed = _changed_paths(repo_root, base_sha, head_sha)
        report["changed_paths"] = sorted(changed)

        considered = {
            path
            for path in changed
            if not any(path.startswith(prefix) for prefix in args.exempt_prefix)
        }
        offending = sorted(considered - allow_list)
        report["offending_paths"] = offending

        if offending:
            report["result"] = "fail"
            raise FenceCheckError(
                f"exact-head change set ({base_sha}..{head_sha}) touches "
                f"paths outside the allow-list: {offending}"
            )

        report["result"] = "pass"
    except FenceCheckError as exc:
        if report["result"] == "error":
            report["result"] = "fail"
        report["error"] = str(exc)
        print(f"WRITE-FENCE CHECK: FAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    print(
        f"WRITE-FENCE CHECK: PASS — {base_sha}..{head_sha} changed "
        f"{len(report['changed_paths'])} path(s), all within the allow-list"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
