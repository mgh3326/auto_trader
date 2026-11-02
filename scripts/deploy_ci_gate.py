#!/usr/bin/env python3
"""Refuse forward deployment unless the image's exact SHA passed the full Test suite.

Stdlib only; GitHub authentication belongs to gh. Registry inspection is read-only
and the returned digest, not the mutable input tag, is what the caller must pull.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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


def _api_rows(endpoint: str, key: str) -> list[dict[str, Any]]:
    pages = _json_command(
        ["gh", "api", "--method", "GET", "--paginate", "--slurp", endpoint]
    )
    if not isinstance(pages, list) or not pages:
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
    args = parser.parse_args(argv)
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
    except GateError as exc:
        print(f"deploy gate: REFUSED: {exc}", file=sys.stderr)
        return 78
    try:
        problems = verify_sha(sha, args.repo)
    except GateError as exc:
        problems = [f"{name}: unverified ({exc})" for name in REQUIRED_CHECKS]
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
