#!/usr/bin/env python3
"""Post the watched checks of one commit as commit statuses (test-any-ref.yml).

mergewake reads a commit whose push run GitHub cancelled through its commit
statuses, named exactly like the watched checks. This helper reads the jobs of
the any-ref run, judges each watched name, and posts one status per name on the
commit. It posts nothing else: no image, no deploy-ok, no ci-ok.

Stdlib only, and it runs from the default-branch checkout, never from the tested
commit. Pure functions (`verdicts`, `ancestor_of_main`, `job_matches`) carry the
decisions; `main` only moves them to and from the GitHub CLI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

TARGET_BRANCH = "main"
# `compare/main...<sha>` reports "behind" or "identical" exactly when <sha> is an
# ancestor of main (or main itself). "ahead", "diverged" and anything else refuse.
ANCESTOR_STATUSES = frozenset({"behind", "identical"})
DESCRIPTION_LIMIT = 140
JOBS_PAGE_SIZE = 100


def ancestor_of_main(compare_status: str) -> bool:
    return compare_status in ANCESTOR_STATUSES


def job_matches(job_name: str, watched: str) -> bool:
    """A reusable-workflow job is named `<caller job> / <job>`; a plain job is `<job>`."""
    return job_name == watched or job_name.endswith(" / " + watched)


def verdicts(jobs: list[dict], watched: list[str]) -> dict[str, tuple[str, str]]:
    """Map each watched check name to (commit status state, short reason).

    Only a single completed job with conclusion `success` is green. A cancelled,
    skipped, failed, unfinished, missing or duplicated job is `failure`: the run
    holds no green evidence for that name.
    """
    result: dict[str, tuple[str, str]] = {}
    for name in watched:
        found = [job for job in jobs if job_matches(str(job.get("name", "")), name)]
        if len(found) != 1:
            result[name] = ("failure", f"{len(found)} jobs match, expected one")
            continue
        job = found[0]
        green = job.get("status") == "completed" and job.get("conclusion") == "success"
        reason = str(job.get("conclusion") or job.get("status") or "unknown")
        result[name] = ("success" if green else "failure", reason)
    return result


def description(name: str, reason: str) -> str:
    return f"any-ref CI {name}: {reason}"[:DESCRIPTION_LIMIT]


def _gh_json(args: list[str]) -> dict:
    out = subprocess.run(
        ["gh", "api", *args], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)


def _fetch_jobs(repo: str, run_id: str, attempt: str) -> list[dict]:
    jobs: list[dict] = []
    page = 1
    while True:
        batch = _gh_json(
            [
                f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
                f"/jobs?per_page={JOBS_PAGE_SIZE}&page={page}"
            ]
        ).get("jobs", [])
        jobs.extend(batch)
        if len(batch) < JOBS_PAGE_SIZE:
            return jobs
        page += 1


def _is_ancestor(repo: str, sha: str) -> bool:
    status = _gh_json([f"repos/{repo}/compare/{TARGET_BRANCH}...{sha}"]).get("status")
    return ancestor_of_main(str(status))


def _post(repo: str, sha: str, context: str, state: str, text: str, url: str) -> bool:
    proc = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={context}",
            "-f",
            f"target_url={url}",
            "-f",
            f"description={description(context, text)}",
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--check-ancestor", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--target-url")
    parser.add_argument("--watch", action="append", default=[])
    args = parser.parse_args(argv)

    if not _is_ancestor(args.repo, args.sha):
        print(f"{args.sha} is not on {TARGET_BRANCH}'s history; nothing posted")
        return 2
    if args.check_ancestor:
        print(f"{args.sha} is on {TARGET_BRANCH}'s history")
        return 0
    if not (args.run_id and args.run_attempt and args.target_url and args.watch):
        parser.error("posting needs --run-id, --run-attempt, --target-url and --watch")

    plan = verdicts(_fetch_jobs(args.repo, args.run_id, args.run_attempt), args.watch)
    failed = []
    for name, (state, reason) in plan.items():
        print(f"{name}: {state} ({reason})")
        if not _post(args.repo, args.sha, name, state, reason, args.target_url):
            failed.append(name)
    if failed:
        print(f"could not post: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
