#!/usr/bin/env python3
"""Deterministic changed-path classifier for CI resource lanes.

ROB-1294 (R2B) ships this as **shadow output only**: nothing in
``.github/workflows/test.yml`` consumes its outputs to skip a job. It exists so
that a later change can move shard/lane topology behind a stable required
check without inventing the fail-closed semantics at that moment.

Fail-closed contract
--------------------
Two distinct non-green outcomes exist and neither is ever laundered into a
"nothing to run" result:

``run_all`` (exit 0, ``result="run_all"``)
    The conservative answer. Emitted whenever the change set cannot be proven
    to be confined to a known lane: an unknown/unclassified path, a rename, a
    copy, a delete, a type change, an unmerged path, any shared CI/config/test
    infrastructure file, an empty change set, or an absent base SHA.

``error`` (exit 1, ``result="error"``)
    A hard red. Emitted when the inputs themselves cannot be trusted: a head
    SHA that was not supplied, a supplied SHA that cannot be resolved even
    after a fetch (shallow/incomplete history), a failing ``git`` invocation,
    or a malformed ``--name-status`` record.

There is deliberately no third outcome in which coverage silently shrinks
because something went wrong. ``--on-missing-base fail`` upgrades the absent
base SHA case from ``run_all`` to ``error`` for callers that can guarantee a
base.

Run with ``-h`` for usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------

#: Every CI job name the classifier can address. ``run_all`` selects all of
#: them; these strings are the vocabulary a later resource-lane change would
#: bind actual job ``if:`` conditions to.
ALL_JOBS: tuple[str, ...] = (
    "lint",
    "test",
    "taskiq-smoke",
    "security",
    "research",
    "frontend",
)

#: Lane -> jobs that lane needs. A lane mapping to an empty set is a lane that
#: provably needs no CI job; only ``docs`` qualifies today.
LANE_JOBS: dict[str, frozenset[str]] = {
    "docs": frozenset(),
    "app": frozenset({"lint", "test", "taskiq-smoke", "security"}),
    "tests": frozenset({"lint", "test"}),
    "research": frozenset({"lint", "research"}),
    "scripts": frozenset({"lint", "test"}),
    "frontend": frozenset({"frontend"}),
    "migrations": frozenset({"lint", "test"}),
    "config": frozenset({"lint", "test"}),
}

#: Pseudo-lanes that always force ``run_all``. They are reported so an operator
#: can see *why* a run was not narrowed.
FORCING_LANES: frozenset[str] = frozenset({"ci_shared", "unknown"})

#: Change statuses (``git diff --name-status``) that are safe to classify by
#: path. Everything else -- rename, copy, delete, type change, unmerged,
#: unknown -- forces ``run_all`` because the *removed* side of the change can
#: affect a lane the surviving path does not name.
CLASSIFIABLE_STATUSES: frozenset[str] = frozenset({"A", "M"})

_NULL_SHA_RE = re.compile(r"\A0{7,64}\Z")

# `git diff --raw`/`--name-status` status grammar: a single letter, optionally
# followed by a similarity/dissimilarity score. Scores appear only for R and C
# (similarity) and for M (dissimilarity, emitted for rewrites under -B). Any
# other shape -- ``AA``, ``A1``, ``Z``, ``R1000``, an empty token -- is not
# something git produces, so it is a hard failure rather than a token we
# truncate to its first character and hope about.
_STATUS_RE = re.compile(r"\A(?:[ADTUXB]|[RCM]\d{1,3})\Z|\A[RCM]\Z")
_RENAME_STATUS_RE = re.compile(r"\A[RC]\d{0,3}\Z")
#: A scored ``M`` is a *rewrite*, not an ordinary modify: git considers the
#: file's content substantially replaced. It is graded with the conservative
#: statuses so it forces ``run_all`` instead of being classified by path.
_REWRITE_STATUS_RE = re.compile(r"\AM\d{1,3}\Z")

# Ordered rule table. First match wins, so shared infrastructure must precede
# the broad directory lanes it lives inside (``scripts/ci/`` before
# ``scripts/``, ``tests/conftest.py`` before ``tests/``).
_EXACT_RULES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "ci_shared"),
    ("uv.lock", "ci_shared"),
    ("Makefile", "ci_shared"),
    (".test_durations", "ci_shared"),
    ("conftest.py", "ci_shared"),
    ("tests/conftest.py", "ci_shared"),
    ("tests/_socket_guard.py", "ci_shared"),
    ("tests/_socket_guard_plugin.py", "ci_shared"),
    ("alembic.ini", "ci_shared"),
    ("env.example", "ci_shared"),
    ("scripts/setup-test-env.sh", "ci_shared"),
    ("codecov.yml", "ci_shared"),
    (".codecov.yml", "ci_shared"),
)

_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    (".github/", "ci_shared"),
    ("scripts/ci/", "ci_shared"),
    ("docs/", "docs"),
    ("app/", "app"),
    ("tests/", "tests"),
    ("research/", "research"),
    ("research_contracts/", "research"),
    ("scripts/", "scripts"),
    ("frontend/", "frontend"),
    ("alembic/", "migrations"),
    ("config/", "config"),
)

#: Suffix rules apply to **top-level files only** (no ``/`` in the path).
#: A bare ``*.md`` rule would otherwise swallow every unmatched nested
#: markdown path -- a fixture under a directory the prefix table does not know
#: yet, or a directory whose name merely resembles a known one (git tracks
#: ``" docs/only.md"``, with a leading space, as a *different* directory).
#: Those must stay ``unknown`` and force ``run_all``.
_TOP_LEVEL_SUFFIX_RULES: tuple[tuple[str, str], ...] = ((".md", "docs"),)


class ClassifierError(RuntimeError):
    """A condition that must turn the classifier job red, never green."""


@dataclass(frozen=True)
class ChangeEntry:
    """One record from ``git diff --name-status -z``."""

    status: str
    path: str
    old_path: str | None = None


@dataclass
class Classification:
    """The deterministic verdict for a change set."""

    run_all: bool
    reason: str
    lanes: tuple[str, ...] = ()
    jobs: tuple[str, ...] = ()
    path_lanes: dict[str, str] = field(default_factory=dict)
    forcing_paths: tuple[str, ...] = ()
    changed_path_count: int = 0

    @property
    def result(self) -> str:
        return "run_all" if self.run_all else "classified"


def classify_path(path: str) -> str:
    """Map a repo-relative path to a lane name (``unknown`` when unmatched).

    Matching is against the **literal** path git reported. Nothing is
    stripped or otherwise normalized first: git happily tracks a file named
    ``" docs/only.md"`` (leading space) and emits it verbatim under
    ``--name-status -z``, and a classifier that trimmed it would answer
    "docs-only, no jobs needed" for a path that is not in ``docs/`` at all.
    An unmatched literal is ``unknown``, which forces ``run_all``.
    """

    if not path:
        return "unknown"
    for exact, lane in _EXACT_RULES:
        if path == exact:
            return lane
    for prefix, lane in _PREFIX_RULES:
        if path.startswith(prefix):
            return lane
    if "/" not in path:
        for suffix, lane in _TOP_LEVEL_SUFFIX_RULES:
            if path.endswith(suffix):
                return lane
    return "unknown"


def classify_entries(entries: Sequence[ChangeEntry]) -> Classification:
    """Classify a change set. Pure: no git, no filesystem, no environment."""

    if not entries:
        return Classification(
            run_all=True,
            reason="empty_change_set",
            lanes=("unknown",),
            jobs=tuple(sorted(ALL_JOBS)),
            changed_path_count=0,
        )

    path_lanes: dict[str, str] = {}
    forcing: set[str] = set()

    for entry in entries:
        if entry.status not in CLASSIFIABLE_STATUSES:
            # Rename/copy/delete/type-change/unmerged: the pre-image path is
            # part of the blast radius, so no narrowing is defensible.
            lane = "unknown"
        else:
            lane = classify_path(entry.path)
        path_lanes[entry.path] = lane
        if lane in FORCING_LANES:
            forcing.add(entry.path)

    lanes = tuple(sorted(set(path_lanes.values())))
    if forcing:
        return Classification(
            run_all=True,
            reason="forcing_path",
            lanes=lanes,
            jobs=tuple(sorted(ALL_JOBS)),
            path_lanes=path_lanes,
            forcing_paths=tuple(sorted(forcing)),
            changed_path_count=len(path_lanes),
        )

    jobs: set[str] = set()
    for lane in lanes:
        jobs |= LANE_JOBS[lane]
    return Classification(
        run_all=False,
        reason="classified",
        lanes=lanes,
        jobs=tuple(sorted(jobs)),
        path_lanes=path_lanes,
        changed_path_count=len(path_lanes),
    )


def parse_name_status(payload: str) -> list[ChangeEntry]:
    """Parse NUL-separated ``git diff --name-status -z`` output.

    Raises ClassifierError on a truncated record rather than dropping it --
    a dropped record is exactly how coverage silently shrinks.
    """

    if payload == "":
        return []
    if not payload.endswith("\0"):
        raise ClassifierError(
            "git --name-status -z output is not NUL-terminated; the stream is "
            "truncated and the missing records cannot be assumed harmless."
        )
    # A correctly terminated stream splits into the fields plus exactly one
    # empty trailing sentinel. Any *other* empty field is an embedded NUL that
    # would previously have been dropped, silently resynchronising the parser
    # onto a different status/path pairing.
    fields = payload.split("\0")[:-1]
    for position, item in enumerate(fields):
        if item == "":
            raise ClassifierError(
                f"git --name-status -z output has an empty field at position "
                f"{position}; an embedded NUL means the record boundaries "
                "cannot be trusted."
            )

    entries: list[ChangeEntry] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if not _STATUS_RE.match(status):
            raise ClassifierError(
                f"unrecognised git status token {status!r}: expected one of "
                "A/C/D/M/R/T/U/X/B, optionally scored for R, C and M."
            )
        if _RENAME_STATUS_RE.match(status):
            if index + 2 >= len(fields):
                raise ClassifierError(
                    f"malformed rename/copy record {status!r} in git output: "
                    "expected an old path and a new path"
                )
            entries.append(
                ChangeEntry(
                    status=status[0], path=fields[index + 2], old_path=fields[index + 1]
                )
            )
            index += 3
            continue
        if index + 1 >= len(fields):
            raise ClassifierError(
                f"malformed record {status!r} in git output: expected a path"
            )
        # A scored M is a rewrite: keep the whole token so it lands outside
        # CLASSIFIABLE_STATUSES and forces run_all, and so the report shows
        # what git actually said rather than a truncated "M".
        graded = status if _REWRITE_STATUS_RE.match(status) else status[0]
        entries.append(ChangeEntry(status=graded, path=fields[index + 1]))
        index += 2
    return entries


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_commit(repo_root: Path, sha: str) -> None:
    """Make ``sha`` locally reachable or raise. Never downgrades to a skip."""

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

    raise ClassifierError(
        f"commit {sha!r} is not reachable locally and could not be fetched "
        f"(git stderr: {fetch.stderr.strip() or unshallow.stderr.strip()!r}). "
        "Incomplete history is a hard failure here, not a narrower run."
    )


def collect_entries(repo_root: Path, base_sha: str, head_sha: str) -> list[ChangeEntry]:
    """Resolve both commits and return their change set."""

    _resolve_commit(repo_root, base_sha)
    _resolve_commit(repo_root, head_sha)
    diff = _run_git(repo_root, "diff", "--name-status", "-z", f"{base_sha}..{head_sha}")
    if diff.returncode != 0:
        raise ClassifierError(
            f"`git diff --name-status {base_sha}..{head_sha}` failed (exit "
            f"{diff.returncode}): {diff.stderr.strip()!r}. This is a hard "
            "failure, not a narrower run."
        )
    return parse_name_status(diff.stdout)


def _is_absent_sha(value: str | None) -> bool:
    """True for an empty value or git's all-zero 'no such commit' sentinel."""

    if value is None:
        return True
    stripped = value.strip()
    return not stripped or bool(_NULL_SHA_RE.match(stripped))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def build_outputs(classification: Classification) -> dict[str, str]:
    """The flat ``key=value`` surface written to ``$GITHUB_OUTPUT``."""

    selected = set(classification.jobs)
    outputs: dict[str, str] = {
        "result": classification.result,
        "run_all": "true" if classification.run_all else "false",
        "reason": classification.reason,
        "lanes": ",".join(classification.lanes),
        "jobs": ",".join(classification.jobs),
        "changed_path_count": str(classification.changed_path_count),
        # Shadow marker: ROB-1294 wires no job condition to these outputs.
        "shadow": "true",
    }
    for job in ALL_JOBS:
        key = "run_" + job.replace("-", "_")
        outputs[key] = "true" if job in selected else "false"
    return outputs


def _write_github_output(outputs: Mapping[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key in sorted(outputs):
            handle.write(f"{key}={outputs[key]}\n")


def _write_step_summary(lines: Iterable[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _summary_lines(report: Mapping[str, object]) -> list[str]:
    lines = [
        "### Change classifier (shadow — drives no job skip)",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in ("result", "reason", "run_all", "lanes", "jobs", "changed_path_count"):
        lines.append(f"| {key} | `{report.get(key)}` |")
    if report.get("error"):
        lines.append(f"| error | `{report['error']}` |")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", help="base commit (else $CLASSIFY_BASE_SHA)")
    parser.add_argument("--head-sha", help="head commit (else $CLASSIFY_HEAD_SHA)")
    parser.add_argument(
        "--name-status-file",
        help="read `git diff --name-status -z` output from this file instead "
        "of invoking git (used by tests and by callers that already have it)",
    )
    parser.add_argument(
        "--on-missing-base",
        choices=("run-all", "fail"),
        default="run-all",
        help="behaviour when no base SHA is supplied (default: run-all)",
    )
    parser.add_argument("--repo-root", default=".", help="repository root")
    parser.add_argument("--json-out", help="write the machine-readable report here")
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="append outputs to $GITHUB_OUTPUT",
    )
    parser.add_argument(
        "--summary", action="store_true", help="append a $GITHUB_STEP_SUMMARY table"
    )
    args = parser.parse_args(argv)

    report: dict[str, object] = {
        "base_sha": None,
        "head_sha": None,
        "result": "error",
        "reason": None,
        "run_all": True,
        "lanes": [],
        "jobs": sorted(ALL_JOBS),
        "path_lanes": {},
        "forcing_paths": [],
        "changed_path_count": 0,
        "shadow": True,
        "error": None,
    }
    outputs: dict[str, str] = {}
    exit_code = 0

    try:
        if args.name_status_file:
            payload = Path(args.name_status_file).read_text(encoding="utf-8")
            classification = classify_entries(parse_name_status(payload))
        else:
            base_sha = args.base_sha or os.environ.get("CLASSIFY_BASE_SHA")
            head_sha = args.head_sha or os.environ.get("CLASSIFY_HEAD_SHA")
            if _is_absent_sha(head_sha):
                raise ClassifierError(
                    "no head commit given: pass --head-sha or set "
                    "$CLASSIFY_HEAD_SHA. The classifier never falls back to a "
                    "default ref such as HEAD or origin/main."
                )
            head_sha = str(head_sha).strip()
            report["head_sha"] = head_sha
            repo_root = Path(args.repo_root).resolve()
            # Resolve the supplied head BEFORE the absent-base short-circuit.
            # An unresolvable supplied commit is a hard red whether or not a
            # base was given; letting the missing-base branch return first
            # reported an invalid head as a conservative green run_all and
            # made it indistinguishable from a legitimate first-push head.
            _resolve_commit(repo_root, head_sha)
            if _is_absent_sha(base_sha):
                if args.on_missing_base == "fail":
                    raise ClassifierError(
                        "no base commit given (--base-sha / $CLASSIFY_BASE_SHA) "
                        "and --on-missing-base=fail was requested."
                    )
                classification = Classification(
                    run_all=True,
                    reason="base_sha_missing",
                    lanes=("unknown",),
                    jobs=tuple(sorted(ALL_JOBS)),
                )
            else:
                base_sha = str(base_sha).strip()
                report["base_sha"] = base_sha
                classification = classify_entries(
                    collect_entries(repo_root, base_sha, head_sha)
                )

        report.update(
            {
                "result": classification.result,
                "reason": classification.reason,
                "run_all": classification.run_all,
                "lanes": list(classification.lanes),
                "jobs": list(classification.jobs),
                "path_lanes": dict(sorted(classification.path_lanes.items())),
                "forcing_paths": list(classification.forcing_paths),
                "changed_path_count": classification.changed_path_count,
            }
        )
        outputs = build_outputs(classification)
    except (ClassifierError, OSError) as exc:
        report["result"] = "error"
        report["reason"] = "classifier_error"
        report["error"] = str(exc)
        # On error the outputs still say "run everything" so that any consumer
        # reading a partially written $GITHUB_OUTPUT cannot infer a narrower
        # run -- but the job itself is red regardless.
        outputs = build_outputs(
            Classification(
                run_all=True,
                reason="classifier_error",
                lanes=("unknown",),
                jobs=tuple(sorted(ALL_JOBS)),
            )
        )
        outputs["result"] = "error"
        print(f"CHANGE CLASSIFIER: ERROR — {exc}", file=sys.stderr)
        exit_code = 1

    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.json_out:
        Path(args.json_out).write_text(serialized + "\n", encoding="utf-8")
    if args.github_output:
        _write_github_output(outputs)
    if args.summary:
        _write_step_summary(_summary_lines(report))

    if exit_code == 0:
        print(
            f"CHANGE CLASSIFIER: {report['result'].upper()} — "  # type: ignore[union-attr]
            f"reason={report['reason']} lanes={report['lanes']} "
            f"jobs={report['jobs']} (shadow: drives no job skip)"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
