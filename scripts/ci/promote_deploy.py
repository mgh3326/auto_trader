#!/usr/bin/env python3
"""Move the `deploy` image tag onto a verified main commit (hk 6037).

main push -> Test -> deploy.yml builds ghcr.io/mgh3326/auto_trader:sha-<7> ->
this script moves the `deploy` tag onto the same digest (no rebuild) -> the pi's
follow.sh installs it. The pi trusts nobody who moved the tag: it re-verifies on its own.

The script moves the tag only when ALL of these hold:
  * the SHA is main or an ancestor of main and its six full-suite Test jobs are green
    (scripts/deploy_ci_gate.py), and the image `sha-<7>` carries exactly that SHA;
  * the range deployed..candidate is routine under the repo's `deploy_approval` policy.
    The rule is mergewake's rollout rule (mergewake internal/pipeline/deploy_approval.go,
    docs/deploy-approval.md), not a looser one:
      - the policy is read at the PREVIOUSLY deployed commit, so a release cannot relax
        its own gate; a missing policy or a missing `deploy_approval` key holds;
      - any changed path (both names of a rename) matching a rule holds;
      - the policy file itself, env/secret-looking paths always hold;
      - an unknown previous deployment, divergent history, an unreadable comparison or one
        that reaches GitHub's 300-file limit holds;
    plus one rule of this repo that does not depend on the old policy text: any change
    under alembic/versions/ or alembic/env.py holds (a new migration is never automatic).
A hold does not fail the job. It writes the matched paths, reasons and the exact
re-dispatch command to the job summary (and Discord when configured). An operator records
the decision in hk and re-runs promote-deploy.yml with `approval_ref`; the approval waives
the range hold only, never the CI, ancestry or image-identity checks.

Stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPOSITORY = "mgh3326/auto_trader"
IMAGE_REPOSITORY = f"ghcr.io/{REPOSITORY}"
DEPLOY_TAG = "deploy"
POLICY_PATH = ".mergewake/policy.yaml"
# GitHub's compare API stops listing files here; mergewake holds when it is reached.
FILE_LIMIT = 300
# New migrations and the migration runner always hold, whatever the previous policy says.
ALWAYS_HOLD = (
    (
        ("alembic/versions/**", "alembic/env.py"),
        "Production migrations change persistent schema or data and need operator coordination (always held).",
    ),
)
_SHA = re.compile(r"[0-9a-f]{40}")
_APPROVAL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.:#-]{2,199}")
EXIT_HOLD = 10
EXIT_REFUSED = 78


class HoldError(Exception):
    """Evidence is missing or unreadable: the range is held, never waved through."""


class PolicyError(HoldError):
    pass


# --------------------------------------------------------------------- policy

_RULE_START = re.compile(r"^  - paths:\s*(\[.*\])?\s*(#.*)?$")
_PATH_ITEM = re.compile(
    r"^      - ('([^']*)'|\"([^\"]*)\"|([^\s#'\"][^#]*?))\s*(#.*)?$"
)
_REASON = re.compile(r"^    reason:\s*(.*?)\s*$")
_TOP_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:")


def _flow_list(text: str) -> list[str]:
    inner = text.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        raise PolicyError("deploy_approval paths flow list is malformed")
    items = []
    for raw in inner[1:-1].split(","):
        item = raw.strip()
        if not item:
            continue
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        if not item or any(ch in item for ch in "'\"\\"):
            raise PolicyError("deploy_approval paths flow list is malformed")
        items.append(item)
    return items


def _reason_text(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def parse_deploy_approval(policy_text: str) -> list[dict[str, Any]]:
    """Read the `deploy_approval` list from the repo's policy file.

    A deliberately small reader for the shape this repo uses (the workflow has no YAML
    library): anything it does not recognise raises PolicyError, and PolicyError holds the
    release. tests/ci/test_promote_deploy.py proves it agrees with a real YAML parser on
    the checked-in policy.
    """
    lines = policy_text.splitlines()
    start = None
    for number, line in enumerate(lines):
        if re.match(r"^deploy_approval:", line):
            if start is not None:
                raise PolicyError("deploy_approval is defined twice")
            start = number
    if start is None:
        raise PolicyError("deployment approval policy has no deploy_approval key")
    head = lines[start].split("#", 1)[0].strip()
    if head == "deploy_approval: []":
        return []
    if head != "deploy_approval:":
        raise PolicyError("deploy_approval has an unsupported shape")
    rules: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_paths = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _TOP_KEY.match(line):
            break
        start_match = _RULE_START.match(line)
        if start_match:
            current = {"paths": [], "reason": ""}
            rules.append(current)
            flow = start_match.group(1)
            in_paths = flow is None
            if flow is not None:
                current["paths"] = _flow_list(flow)
            continue
        if current is None:
            raise PolicyError("deploy_approval has an unsupported shape")
        if in_paths:
            item = _PATH_ITEM.match(line)
            if item:
                value = item.group(2) or item.group(3) or item.group(4)
                if not value:
                    raise PolicyError("deploy_approval path is empty")
                current["paths"].append(value.strip())
                continue
        reason = _REASON.match(line)
        if reason:
            in_paths = False
            current["reason"] = _reason_text(reason.group(1))
            continue
        raise PolicyError(f"deploy_approval line is not understood: {stripped[:60]}")
    if not rules:
        raise PolicyError("deploy_approval has no rules")
    for rule in rules:
        if not rule["paths"] or not rule["reason"]:
            raise PolicyError("deploy_approval rule needs paths and a reason")
    return rules


# ----------------------------------------------------------------------- globs


def match_glob(glob: str, name: str) -> bool:
    """mergewake policy.Match: path.Match per segment, `**` as a whole segment."""
    if not glob or glob.startswith("/") or "\\" in glob:
        raise PolicyError(f"invalid path glob {glob!r}")
    globs = glob.split("/")
    names = name.split("/")
    for segment in globs:
        if segment in ("", ".."):
            raise PolicyError(f"invalid path glob {glob!r}")

    def segment_match(pattern: str, text: str) -> bool:
        # path.Match negates a class with `^`; fnmatch uses `!`.
        pattern = re.sub(r"\[\^", "[!", pattern)
        return fnmatch.fnmatchcase(text, pattern)

    def walk(i: int, j: int) -> bool:
        if i == len(globs):
            return j == len(names)
        if globs[i] == "**":
            return walk(i + 1, j) or (j < len(names) and walk(i, j + 1))
        if j == len(names):
            return False
        return segment_match(globs[i], names[j]) and walk(i + 1, j + 1)

    return walk(0, 0)


def match_any(globs: Sequence[str], name: str) -> bool:
    matched = False
    for glob in globs:  # every glob is checked: an invalid one fails even after a match
        matched = match_glob(glob, name) or matched
    return matched


def is_protected(name: str) -> bool:
    """mergewake's environment/secret/approval-policy names (always held)."""
    base = os.path.basename(name).lower()
    if name == POLICY_PATH:
        return True
    if (
        base.startswith(".env")
        or base.startswith("env.")
        or base.endswith(".env")
        or base.startswith("secret")
        or base.startswith("credential")
        or base.endswith(".pem")
        or base.endswith(".key")
    ):
        return True
    return any(
        segment in ("secrets", "secret", "env", ".env")
        for segment in name.lower().split("/")
    )


# ------------------------------------------------------------------------ range


@dataclass
class Review:
    previous: str
    release: str
    status: str = "routine"  # routine | same | stale | hold
    paths: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        return self.status == "hold"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "previous": self.previous,
            "release": self.release,
            "paths": sorted(set(self.paths)),
            "reasons": sorted(set(self.reasons)),
        }


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def changed_files(root: Path, previous: str, release: str) -> list[tuple[str, str]]:
    """(path, previous_name) for every changed file; a rename lists both names."""
    done = _git(
        root,
        "diff",
        "--name-status",
        "-z",
        "-M",
        "--no-ext-diff",
        f"{previous}..{release}",
    )
    if done.returncode != 0:
        raise HoldError("change range unavailable")
    tokens = done.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    files: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status or status[0] not in "AMDTRCU":
            raise HoldError("change range has an unknown change kind")
        width = 2 if status[0] in "RC" else 1
        names = tokens[index + 1 : index + 1 + width]
        if len(names) != width or not all(names):
            raise HoldError("change range is malformed")
        files.append((names[-1], names[0] if width == 2 else ""))
        index += 1 + width
    return files


def review_range(root: Path, previous: str, release: str) -> Review:
    review = Review(previous=previous, release=release)
    if not _SHA.fullmatch(release):
        raise HoldError("release must be a full commit SHA")
    if previous == "unknown":
        review.status = "hold"
        review.reasons.append(
            "missing or invalid previous deployment (the deploy tag carries no verifiable commit)"
        )
        return review
    if not _SHA.fullmatch(previous):
        raise HoldError("previous must be a full commit SHA or 'unknown'")
    if previous == release:
        review.status = "same"
        return review
    for sha in (previous, release):
        if _git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            review.status = "hold"
            review.reasons.append(
                f"commit {sha[:7]} is not in the checkout (history unavailable)"
            )
            return review
    if _git(root, "merge-base", "--is-ancestor", release, previous).returncode == 0:
        review.status = "stale"  # an older build finishing late: nothing to promote
        return review
    if _git(root, "merge-base", "--is-ancestor", previous, release).returncode != 0:
        review.status = "hold"
        review.reasons.append(
            "divergent history: the deployed commit is not an ancestor of the release"
        )
        return review
    try:
        files = changed_files(root, previous, release)
    except HoldError as exc:
        review.status = "hold"
        review.reasons.append(str(exc))
        return review
    if len(files) >= FILE_LIMIT:
        review.status = "hold"
        review.reasons.append(
            f"change range reaches the {FILE_LIMIT}-file comparison limit"
        )
        return review
    # Load the policy from the DEPLOYED commit: a release cannot relax its own gate.
    policy = _git(root, "show", f"{previous}:{POLICY_PATH}")
    rules: list[dict[str, Any]] = []
    if policy.returncode != 0:
        review.status = "hold"
        review.reasons.append(
            "deployment approval policy unavailable at the deployed commit"
        )
    else:
        try:
            rules = parse_deploy_approval(policy.stdout)
        except HoldError as exc:
            review.status = "hold"
            review.reasons.append(f"deployment approval policy unavailable: {exc}")
    for path, old_path in files:
        for name in (path, old_path):
            if not name:
                continue
            hit = False
            if is_protected(name):
                hit = True
                review.reasons.append("environment, secret or approval policy change")
            for globs, reason in ALWAYS_HOLD:
                if match_any(globs, name):
                    hit = True
                    review.reasons.append(reason)
            try:
                for rule in rules:
                    if match_any(rule["paths"], name):
                        hit = True
                        review.reasons.append(rule["reason"])
            except HoldError as exc:
                review.status = "hold"
                review.reasons.append(f"deployment approval policy unavailable: {exc}")
            if hit:
                review.status = "hold"
                review.paths.append(name)
    return review


# ------------------------------------------------------------------- orchestration

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _gate_script() -> str:
    return str(Path(__file__).resolve().parents[1] / "deploy_ci_gate.py")


def verify_candidate(sha: str, repo: str, run: Runner) -> str:
    """CI gate + main ancestry + image identity. Returns the pinned digest reference."""
    short = sha[:7]
    done = run(
        [
            sys.executable,
            _gate_script(),
            "--image",
            f"{IMAGE_REPOSITORY}:sha-{short}",
            "--expect-sha",
            sha,
            "--main-ancestor",
            "--repo",
            repo,
        ]
    )
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        raise SystemExit(EXIT_REFUSED)
    pinned = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    if not re.fullmatch(
        rf"{re.escape(IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}", pinned
    ):
        print("promote: the gate returned no immutable image digest", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)
    return pinned


def current_deploy(run: Runner) -> tuple[str, str]:
    """(pinned digest, commit) the `deploy` tag carries now; ('none','unknown') if unknown."""
    done = run(
        [
            sys.executable,
            _gate_script(),
            "--image",
            f"{IMAGE_REPOSITORY}:{DEPLOY_TAG}",
            "--resolve-only",
        ]
    )
    if done.returncode != 0 or "\t" not in done.stdout:
        return "none", "unknown"
    pinned, _, sha = done.stdout.strip().partition("\t")
    return (pinned, sha) if _SHA.fullmatch(sha) else ("none", "unknown")


def render_summary(
    review: Review, *, held: bool, approval_ref: str, pinned: str, command: str
) -> str:
    data = review.as_dict()
    lines = [
        "## 배포 승격(promote)",
        "",
        f"- 후보: `{review.release}`",
        f"- 배포 중(`{DEPLOY_TAG}` 태그): `{review.previous}`",
        f"- 이미지: `{pinned}`",
    ]
    if held:
        lines += [
            "- 결과: **보류** — `deploy` 태그를 옮기지 않았다(pi 는 이전 버전 유지).",
            "",
            "### 보류 사유",
            *[f"- {reason}" for reason in data["reasons"]],
            "",
            "### 걸린 경로",
            *(
                [f"- `{path}`" for path in data["paths"]]
                or ["- (경로 없음: 범위를 확인하지 못함)"]
            ),
            "",
            "### 운영자가 할 일",
            "1. hk 에 위 범위를 승인하는 결정을 남긴다(사람 승인 · 후보 SHA 정확히 일치).",
            "2. 승인되면 아래 명령으로 같은 SHA 를 다시 승격한다. 승인은 범위 보류만 면제하고 "
            "CI·main 조상·이미지 확인은 그대로 다시 한다.",
            "",
            "```bash",
            command,
            "```",
        ]
    elif approval_ref:
        lines += [
            f"- 결과: **승격** — 승인 `{approval_ref}` 로 범위 보류를 면제했다.",
            *[f"- 면제된 사유: {reason}" for reason in data["reasons"]],
        ]
    else:
        lines.append(
            "- 결과: **승격** — `deploy` 태그를 같은 digest 로 옮겼다(이미지 재빌드 없음)."
        )
    return "\n".join(lines) + "\n"


def notify_discord(message: str) -> None:
    """Best effort: a missing webhook or a send failure never changes the result."""
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook.startswith("https://"):
        return
    body = json.dumps({"content": message[:1800]}).encode()
    request = urllib.request.Request(
        hook,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "auto-trader-promote",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()  # noqa: S310 - https webhook from a secret
    except Exception:  # noqa: BLE001 - reporting must never fail the promote job
        print("promote: discord notice failed (ignored)", file=sys.stderr)


def promote(args: argparse.Namespace, run: Runner = _run) -> int:
    sha = args.sha
    if not _SHA.fullmatch(sha):
        print("promote: --sha must be a full 40-hex commit SHA", file=sys.stderr)
        return EXIT_REFUSED
    approval_ref = args.approval_ref or ""
    if approval_ref and not _APPROVAL_REF.fullmatch(approval_ref):
        print(
            "promote: --approval-ref is not a valid hk decision reference",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if approval_ref and not args.approval_gated:
        # A free-form string is not an approval. The waiver runs only in the job bound to the
        # GitHub Environment whose required reviewers are the human approval.
        print(
            "promote: an approval reference is accepted only from the environment-gated job",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        pinned = verify_candidate(sha, args.repo, run)
    except SystemExit:
        notify_discord(
            f"⛔ AT 배포 승격 거부 {sha[:7]} — 게이트(CI·main 조상·이미지 확인)를 통과하지 못했다. deploy 태그는 그대로다."
        )
        raise
    previous_digest, previous = current_deploy(run)
    review = review_range(Path(args.repo_root), previous, sha)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    command = (
        f"gh workflow run promote-deploy.yml --ref main -f promote_sha={sha} "
        "-f approval_ref=<hk 결정 문서 키>"
    )

    def emit(text: str) -> None:
        print(text)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(text)

    if review.status == "same":
        if previous_digest == pinned:
            emit(
                f"## 배포 승격\n\n`{sha[:7]}` 는 이미 `{DEPLOY_TAG}` 태그가 가리킨다. 할 일 없음.\n"
            )
            return 0
        # Same commit, different digest (a rebuild): the tag follows the verified image.
        review.status = "routine"
    if review.status == "stale":
        emit(
            f"## 배포 승격\n\n`{sha[:7]}` 는 `{DEPLOY_TAG}` 태그({previous[:7]})보다 오래된 빌드다. "
            "태그를 되돌리지 않는다.\n"
        )
        if approval_ref:
            # An approved dispatch that changes nothing must not look like success.
            emit(
                f"\n**승인 `{approval_ref}` 는 적용되지 않았다** — 승인한 SHA 가 이미 배포 중 커밋의 조상이다.\n"
            )
            return 1
        return 0
    held = review.held and not approval_ref
    if held:
        emit(
            render_summary(
                review, held=True, approval_ref="", pinned=pinned, command=command
            )
        )
        print(
            f"::warning::deploy held for operator approval: {sha[:7]} ({len(review.paths)} paths)",
            flush=True,
        )
        notify_discord(
            f"⏸️ AT 배포 보류 {sha[:7]} — 승인 필요: "
            + "; ".join(sorted(set(review.reasons))[:3])
            + f" · 걸린 경로 {len(set(review.paths))}개 · 다시 승격: {command}"
        )
        return 0
    done = run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            f"{IMAGE_REPOSITORY}:{DEPLOY_TAG}",
            pinned,
        ]
    )
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        print("promote: moving the deploy tag failed", file=sys.stderr)
        return 1
    after_digest, after_sha = current_deploy(run)
    if after_digest != pinned or after_sha != sha:
        print(
            f"promote: the deploy tag reads back {after_sha[:7]} {after_digest}, expected {sha[:7]} {pinned}",
            file=sys.stderr,
        )
        return 1
    emit(
        render_summary(
            review,
            held=False,
            approval_ref=approval_ref,
            pinned=pinned,
            command=command,
        )
    )
    return 0


def evaluate(args: argparse.Namespace) -> int:
    review = review_range(Path(args.repo_root), args.previous, args.sha)
    print(json.dumps(review.as_dict(), ensure_ascii=False, indent=2))
    return EXIT_HOLD if review.held else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "promote", help="verify the SHA, review the range, move the deploy tag"
    )
    run.add_argument("--sha", required=True)
    run.add_argument("--repo", default=REPOSITORY)
    run.add_argument("--repo-root", default=".")
    run.add_argument("--approval-ref", default="")
    run.add_argument(
        "--approval-gated",
        action="store_true",
        help="set only by the job bound to the approval GitHub Environment",
    )
    run.set_defaults(func=promote)
    ev = sub.add_parser(
        "evaluate", help="print the deploy_approval review of previous..sha"
    )
    ev.add_argument("--previous", required=True, help="full SHA or 'unknown'")
    ev.add_argument("--sha", required=True)
    ev.add_argument("--repo-root", default=".")
    ev.set_defaults(func=evaluate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
