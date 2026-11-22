#!/usr/bin/env python3
"""Refuse forward deployment unless the image's exact SHA passed the full Test suite.

Stdlib only; GitHub authentication belongs to gh. Registry inspection is read-only
and the returned digest, not the mutable input tag, is what the caller must pull.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

REPOSITORY = "mgh3326/auto_trader"
IMAGE_REPOSITORY = f"ghcr.io/{REPOSITORY}"
# .github/workflows/test.yml: lint + taskiq-smoke + Python 3.13's four groups.
# ROB-1454 narrows PRs, not push/dispatch runs. Neither PR gate nor a skipped
# matrix job proves the deploy suite ran.
REQUIRED_CHECKS = (
    "lint",
    "taskiq-smoke",
    "test (3.13, 1)",
    "test (3.13, 2)",
    "test (3.13, 3)",
    "test (3.13, 4)",
)
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION = "org.opencontainers.image.revision"
# hk 6037: the pi has no gh auth. `anonymous` reads the public repo's Actions evidence
# straight from api.github.com (60 requests/hour per address), so callers only use it
# when a digest changed. `gh` (default) keeps every existing caller unchanged.
GITHUB_MODES = ("gh", "anonymous")
_GITHUB_MODE_ENV = "AT_DEPLOY_GATE_GITHUB"
_API_ROOT = "https://api.github.com/"
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_MAX_PAGES = 20


class GateError(Exception):
    """Evidence unavailable or not in the expected shape (never command stderr)."""


def _json_command(argv: list[str]) -> Any:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GateError(f"{argv[0]} evidence unavailable") from None
    if result.returncode:
        raise GateError(f"{argv[0]} evidence unavailable (exit {result.returncode})")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise GateError(f"{argv[0]} evidence is not valid JSON") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _github_mode() -> str:
    mode = os.environ.get(_GITHUB_MODE_ENV, "gh") or "gh"
    if mode not in GITHUB_MODES:
        raise GateError(f"{_GITHUB_MODE_ENV} must be one of {', '.join(GITHUB_MODES)}")
    return mode


def _anonymous_pages(endpoint: str, *, single: bool = False) -> list[Any]:
    """GET endpoint (with Link pagination) from api.github.com without credentials.

    `single` reads exactly one response and ignores Link: one request, however long the list.
    """
    url: str | None = _API_ROOT + endpoint.lstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)
    pages: list[Any] = []
    while url:
        if not url.startswith(_API_ROOT) or len(pages) >= _MAX_PAGES:
            raise GateError("GitHub evidence pagination is not trustworthy")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "auto-trader-deploy-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            raise GateError(f"GitHub evidence unavailable (HTTP {exc.code})") from None
        except (OSError, ValueError):
            raise GateError("GitHub evidence unavailable") from None
        if len(body) > 8 * 1024 * 1024:
            raise GateError("GitHub evidence is too large")
        try:
            pages.append(json.loads(body))
        except (ValueError, TypeError):
            raise GateError("GitHub evidence is not valid JSON") from None
        match = _NEXT_LINK.search(link or "")
        url = None if single else (match.group(1) if match else None)
    return pages


def _api_pages(endpoint: str) -> list[Any]:
    if _github_mode() == "anonymous":
        return _anonymous_pages(endpoint)
    pages = _json_command(
        ["gh", "api", "--method", "GET", "--paginate", "--slurp", endpoint]
    )
    if not isinstance(pages, list):
        raise GateError("GitHub evidence has no pages")
    return pages


def _api_rows(endpoint: str, key: str) -> list[dict[str, Any]]:
    pages = _api_pages(endpoint)
    if not pages:
        raise GateError("GitHub evidence has no pages")
    rows = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get(key), list):
            raise GateError(f"GitHub evidence has no {key} list")
        for row in page[key]:
            if not isinstance(row, dict):
                raise GateError(f"GitHub evidence has an invalid {key} row")
            rows.append(row)
    return rows


def main_ancestor_problems(sha: str, repo: str) -> list[str]:
    """Return a problem unless `sha` is main itself or an ancestor of main.

    compare/<sha>...main: base=sha, head=main. `ahead`/`identical` means main contains
    the commit; `behind`/`diverged` means it is not on main's history.
    """
    quoted = urllib.parse.quote(sha, safe="")
    if _github_mode() == "anonymous":
        # The verdict is the first response's `status`; its commit list may span many pages
        # (one per commit main is ahead), so fetch exactly one request.
        pages = _anonymous_pages(
            f"repos/{repo}/compare/{quoted}...main?per_page=1", single=True
        )
        first = pages[0] if pages else None
    else:
        first = _json_command(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{repo}/compare/{quoted}...main?per_page=1",
            ]
        )
    if not isinstance(first, dict) or not isinstance(first.get("status"), str):
        raise GateError("GitHub compare evidence is invalid")
    if first["status"] in ("ahead", "identical"):
        return []
    return [
        f"main-ancestor: {sha} is not an ancestor of main (compare {first['status']})"
    ]


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def verify_sha(sha: str, repo: str) -> list[str]:
    """Return named missing/red checks from the newest eligible run and attempt."""
    runs = _api_rows(
        f"repos/{repo}/actions/workflows/test.yml/runs?head_sha={sha}&per_page=100",
        "workflow_runs",
    )
    eligible = [
        run
        for run in runs
        if run.get("head_sha") == sha
        and run.get("event") in ("push", "workflow_dispatch")
    ]
    if not eligible:
        return [
            f"{name}: missing (no exact-SHA push/dispatch Test run)"
            for name in REQUIRED_CHECKS
        ]
    if any(
        not _positive_int(run.get("id")) or not _positive_int(run.get("run_attempt"))
        for run in eligible
    ):
        raise GateError("GitHub Test run identity is invalid")
    run = max(eligible, key=lambda row: (row["id"], row["run_attempt"]))
    run_id, attempt = run["id"], run["run_attempt"]
    print(
        f"deploy gate: SHA {sha}; Test run {run_id}, attempt {attempt}", file=sys.stderr
    )
    jobs = _api_rows(
        f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        "jobs",
    )
    latest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("name")
        if name not in REQUIRED_CHECKS:
            continue
        if not _positive_int(job.get("id")):
            raise GateError("GitHub required job identity is invalid")
        if name not in latest or job["id"] > latest[name]["id"]:
            latest[name] = job
    problems = []
    for name in REQUIRED_CHECKS:
        job = latest.get(name)
        if job is None:
            problems.append(f"{name}: missing")
        elif job.get("head_sha", sha) != sha:
            problems.append(f"{name}: wrong SHA")
        elif job.get("status") != "completed" or job.get("conclusion") != "success":
            state = json.dumps(
                [job.get("status"), job.get("conclusion")], ensure_ascii=True
            )
            problems.append(f"{name}: not successful {state}")
    return problems


def _manifest_digest(manifest: Any) -> str:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("digest"), str):
        raise GateError("registry manifest digest is missing")
    digest = manifest["digest"]
    if not _DIGEST.fullmatch(digest):
        raise GateError("registry manifest digest is invalid")
    return digest


def _inspect(image: str, field: str) -> Any:
    return _json_command(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            "{{json ." + field + "}}",
        ]
    )


def resolve_image(image: str) -> tuple[str, list[str]]:
    """Pin the remote manifest before inspecting its immutable platform configs."""
    if not image.startswith(f"{IMAGE_REPOSITORY}:"):
        raise GateError("image must be an auto_trader GHCR tag")
    manifest = _inspect(image, "Manifest")
    pinned = f"{IMAGE_REPOSITORY}@{_manifest_digest(manifest)}"
    children = manifest.get("manifests")
    if children is None:
        references = [pinned]
    else:
        if not isinstance(children, list) or not children:
            raise GateError("registry manifest list is invalid")
        references = []
        for child in children:
            if not isinstance(child, dict):
                raise GateError("registry platform manifest is invalid")
            annotations = child.get("annotations", {})
            if not isinstance(annotations, dict):
                raise GateError("registry manifest annotations are invalid")
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                continue
            references.append(f"{IMAGE_REPOSITORY}@{_manifest_digest(child)}")
        if not references:
            raise GateError("registry manifest has no runnable images")
    return pinned, references


def image_sha(references: list[str]) -> str:
    revisions = set()
    for reference in references:
        config = _inspect(reference, "Image")
        try:
            revision = config["config"]["Labels"][_REVISION]
        except (KeyError, TypeError):
            raise GateError("image OCI revision is missing") from None
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise GateError("image OCI revision is not a full commit SHA")
        revisions.add(revision)
    if len(revisions) != 1:
        raise GateError("image platforms have different commit SHAs")
    return revisions.pop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sha")
    target.add_argument("--image")
    parser.add_argument("--repo", default=REPOSITORY)
    parser.add_argument("--allow-unverified", metavar="REASON")
    parser.add_argument(
        "--github",
        choices=GITHUB_MODES,
        help="evidence source: gh (authenticated gh CLI, default) or anonymous "
        f"(public api.github.com, rate limited); env {_GITHUB_MODE_ENV}",
    )
    parser.add_argument(
        "--main-ancestor",
        action="store_true",
        help="also require the SHA to be main or an ancestor of main (hk 6037)",
    )
    parser.add_argument(
        "--expect-sha",
        metavar="SHA",
        help="with --image: refuse unless the image's OCI revision is exactly this SHA",
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="with --image: print '<pinned digest>\\t<sha>' from the registry only, "
        "no GitHub request and no CI verdict",
    )
    args = parser.parse_args(argv)
    if args.github:
        os.environ[_GITHUB_MODE_ENV] = args.github
    if args.resolve_only and not args.image:
        parser.error("--resolve-only needs --image")
    if args.expect_sha and not args.image:
        parser.error("--expect-sha needs --image")
    reason = args.allow_unverified
    if reason is not None and not reason.strip():
        print(
            "deploy gate: --allow-unverified needs a non-blank reason", file=sys.stderr
        )
        return 78
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        print("deploy gate: invalid GitHub repository", file=sys.stderr)
        return 78
    pinned = None
    # An override waives CI evidence, not the identity of the image/commit.
    sha = args.sha
    try:
        if args.image:
            pinned, references = resolve_image(args.image)
            sha = image_sha(references)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GateError("deploy SHA must be a full 40-character commit SHA")
        if args.expect_sha and args.expect_sha != sha:
            raise GateError(
                f"image revision {sha} is not the expected {args.expect_sha}"
            )
        _github_mode()
    except GateError as exc:
        print(f"deploy gate: REFUSED: {exc}", file=sys.stderr)
        return 78
    if args.resolve_only:
        print(f"{pinned}\t{sha}")
        return 0
    try:
        problems = verify_sha(sha, args.repo)
    except GateError as exc:
        problems = [f"{name}: unverified ({exc})" for name in REQUIRED_CHECKS]
    ancestry: list[str] = []
    if args.main_ancestor:
        try:
            ancestry = main_ancestor_problems(sha, args.repo)
        except GateError as exc:
            ancestry = [f"main-ancestor: unverified ({exc})"]
        for problem in ancestry:
            print(f"deploy gate: {problem}", file=sys.stderr)
        if ancestry:
            # --allow-unverified waives CI evidence only, never which commit this is.
            print("deploy gate: REFUSED (exit 78); not on main", file=sys.stderr)
            return 78
    for problem in problems:
        print(f"deploy gate: {problem}", file=sys.stderr)
    if reason is not None:
        print(
            f"deploy gate: OVERRIDE --allow-unverified SHA={sha or 'unknown'} reason={json.dumps(reason, ensure_ascii=True)}",
            file=sys.stderr,
        )
    elif problems:
        print(
            "deploy gate: REFUSED (exit 78); wait for the full suite or use --allow-unverified REASON",
            file=sys.stderr,
        )
        return 78
    else:
        print(
            f"deploy gate: PASS SHA {sha}; all six full-suite checks succeeded",
            file=sys.stderr,
        )
    if pinned:
        print(pinned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
