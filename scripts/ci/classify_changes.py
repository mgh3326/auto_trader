#!/usr/bin/env python3
"""Deterministic changed-path classifier that decides what a PR tests.

ROB-1294 built this in shadow; ROB-1454 makes it decide. On a pull request
``.github/workflows/test.yml`` runs lint plus the test files named here, and
falls back to the full suite only on ``run_all``. Main pushes and
workflow_dispatch always run the full suite, whatever this script says.

Selection (``select_affected_tests``)
-------------------------------------
The universe is the union of ``ci_shards/shard-*.txt``: exactly the files the
full suite runs. A changed path selects the universe files that reference it
by module name or repo path, plus the directory that mirrors it under
``tests/``. This is deliberately not a transitive import closure: on recorded
merges the closure reached 1169 of 2024 files, which is the full suite with
extra steps. What it misses is what the full suite on main is for.

Fail-closed contract
--------------------
Two distinct non-green outcomes exist and neither is ever laundered into a
"nothing to run" result:

``run_all`` (exit 0, ``result="run_all"``)
    The conservative answer. Emitted whenever the change set cannot be proven
    to be confined to a known lane: an unknown/unclassified path, a rename, a
    copy, a delete, a type change, an unmerged path, any shared CI/config/test
    infrastructure file, an empty change set, an absent base SHA, a path that
    selects no test, or a selection too broad for one job.

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
import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------

#: The full suite: what ``run_all`` selects and what every main push runs.
ALL_JOBS: tuple[str, ...] = ("lint", "test", "taskiq-smoke")

#: The PR job that runs the selected test files instead of the shards.
AFFECTED_JOB = "affected-tests"

#: Lane -> jobs that lane needs on a PR. Lint runs on every PR regardless, so
#: an empty set means "no test job": ``docs`` has nothing to test and
#: ``frontend`` is covered by the unconditional ``frontend`` job.
_NARROW_JOBS = frozenset({"lint", AFFECTED_JOB})
LANE_JOBS: dict[str, frozenset[str]] = {
    "docs": frozenset(),
    "app": _NARROW_JOBS,
    "tests": _NARROW_JOBS,
    "research": _NARROW_JOBS,
    "scripts": _NARROW_JOBS,
    "frontend": frozenset(),
    "migrations": _NARROW_JOBS,
    "config": _NARROW_JOBS,
    "manifests": _NARROW_JOBS,
}

#: Lanes where a path that selects no test means "we do not know what this
#: touches", so it forces ``run_all``. ``research`` is exempt: research code is
#: isolated from the runtime and its suites run in their own jobs. ``docs``,
#: ``frontend`` and ``manifests`` select no test files by definition.
_MUST_MAP_LANES: frozenset[str] = frozenset(
    {"app", "tests", "scripts", "migrations", "config"}
)

#: A selection bigger than one shard (~500 files, 5-9 minutes) would not fit one
#: job's timeout and is no longer narrow, so it becomes the full suite instead.
MAX_AFFECTED_TEST_FILES = 500

#: Pseudo-lanes that always force ``run_all``. They are reported so an operator
#: can see *why* a run was not narrowed.
FORCING_LANES: frozenset[str] = frozenset({"ci_shared", "unknown"})

#: Change statuses (``git diff --name-status``) that are safe to classify by
#: path. Everything else -- rename, copy, delete, type change, unmerged,
#: unknown -- forces ``run_all`` because the *removed* side of the change can
#: affect a lane the surviving path does not name.
CLASSIFIABLE_STATUSES: frozenset[str] = frozenset({"A", "M"})

_NULL_SHA_RE = re.compile(r"\A0{7,64}\Z")

#: A one-line manifest edit is how every new test file lands (see
#: docs/runbooks/ci-file-shard-manifests.md §4), so it must not force run_all.
#: ``weights.json`` stays unknown: only the duration refresh writes it.
_SHARD_MANIFEST_RE = re.compile(r"\Aci_shards/shard-\d+\.txt\Z")

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
    affected_tests: tuple[str, ...] = ()
    check_manifests: bool = False

    @property
    def result(self) -> str:
        return "run_all" if self.run_all else "classified"

    @property
    def run_affected_tests(self) -> bool:
        """Whether the PR needs the ``affected-tests`` job at all."""

        return not self.run_all and (bool(self.affected_tests) or self.check_manifests)


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
    if _SHARD_MANIFEST_RE.match(path):
        return "manifests"
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


# --------------------------------------------------------------------------
# Affected-test selection
# --------------------------------------------------------------------------


@dataclass
class TestUniverse:
    """The test files the full suite runs, with lazily cached source text."""

    __test__ = False  # not a pytest class, despite the name

    files: frozenset[str]
    read: Callable[[str], str]
    _texts: dict[str, str] = field(default_factory=dict)

    def text(self, path: str) -> str:
        if path not in self._texts:
            self._texts[path] = self.read(path)
        return self._texts[path]

    @classmethod
    def from_manifests(cls, repo_root: Path) -> TestUniverse:
        """Load ``ci_shards/shard-*.txt``. No manifests means an empty universe,
        which selects nothing and therefore falls back to ``run_all``."""

        files: set[str] = set()
        for manifest in sorted((repo_root / "ci_shards").glob("shard-*.txt")):
            files.update(manifest.read_text(encoding="utf-8").split())

        def read(path: str) -> str:
            try:
                return (repo_root / path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                # A manifest naming a file that is not there is a broken tree,
                # not a reason to select less.
                raise ClassifierError(
                    f"shard manifest lists {path!r} but it cannot be read: {exc}"
                ) from exc

        return cls(files=frozenset(files), read=read)


def _module_name(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    dotted = path[: -len(".py")].replace("/", ".")
    return dotted.removesuffix(".__init__")


def _imports_name(source: str, parent: str, name: str) -> bool:
    """True when ``source`` has ``from parent import name``."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Unparseable means we cannot prove it does not import the module.
        return True
    return any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == parent
        and any(alias.name in (name, "*") for alias in node.names)
        for node in ast.walk(tree)
    )


def _references(path: str, universe: TestUniverse) -> set[str]:
    """Universe files that name ``path`` as a module or a repo-relative path."""

    module = _module_name(path)
    patterns = [re.escape(path)]
    if module:
        patterns.append(re.escape(module))
    # The lookbehind keeps `app.foo` from matching `xapp.foo` or `a.app.foo`;
    # the lookahead keeps it from matching `app.foo_bar`, and still allows
    # `app.foo.attr`, which is how patch() targets name a module.
    pattern = re.compile(rf"(?<![\w./-])(?:{'|'.join(patterns)})(?![\w-])")
    parent, _, name = (module or "").rpartition(".")
    from_import = (
        re.compile(rf"\bfrom\s+{re.escape(parent)}\s+import\b") if parent else None
    )
    needle = name or path.rsplit("/", 1)[-1]

    hits: set[str] = set()
    for test_file in universe.files:
        source = universe.text(test_file)
        if needle not in source:
            continue
        if pattern.search(source) or (
            from_import
            and from_import.search(source)
            and _imports_name(source, parent, name)
        ):
            hits.add(test_file)
    return hits


def _mirror_prefixes(path: str) -> tuple[str, ...]:
    """``tests/`` directories that mirror ``path``.

    ``app/services/foo/bar.py`` -> ``tests/services/foo/bar/`` and
    ``tests/services/foo/``. The parent is skipped when it is only one level
    below the source root (``tests/services/``): that is most of the suite, not
    this module's surface.
    """

    parts = path.split("/")
    if parts[0] == "app":
        parts = parts[1:]
    stem = parts[-1].split(".", 1)[0]
    parents = parts[:-1]
    prefixes = []
    if stem != "__init__":
        prefixes.append("/".join(["tests", *parents, stem]) + "/")
    if len(parents) >= 2:
        prefixes.append("/".join(["tests", *parents]) + "/")
    return tuple(prefixes)


def _tests_for_path(path: str, lane: str, universe: TestUniverse) -> set[str]:
    if lane == "tests":
        if path in universe.files:
            return {path}
        selected = _references(path, universe)
        directory = path.rpartition("/")[0]
        # A helper or conftest serves its own directory. tests/ itself is not
        # a surface: the root conftest is ci_shared and forces run_all earlier.
        if directory != "tests":
            selected |= {f for f in universe.files if f.startswith(directory + "/")}
        return selected
    prefixes = _mirror_prefixes(path)
    return _references(path, universe) | {
        f for f in universe.files if f.startswith(prefixes)
    }


def select_affected_tests(
    classification: Classification, universe: TestUniverse
) -> Classification:
    """Turn lanes into a concrete file list, or fall back to ``run_all``.

    Pure apart from ``universe``. A ``run_all`` input is returned unchanged.
    """

    if classification.run_all:
        return classification

    selected: set[str] = set()
    unmapped: set[str] = set()
    for path, lane in classification.path_lanes.items():
        if lane in ("docs", "frontend", "manifests"):
            continue
        tests = _tests_for_path(path, lane, universe)
        if not tests and lane in _MUST_MAP_LANES:
            unmapped.add(path)
        selected |= tests

    if unmapped:
        return replace(
            classification,
            run_all=True,
            reason="unmapped_path",
            jobs=tuple(sorted(ALL_JOBS)),
            forcing_paths=tuple(sorted(unmapped)),
        )
    if len(selected) > MAX_AFFECTED_TEST_FILES:
        return replace(
            classification,
            run_all=True,
            reason="selection_too_broad",
            jobs=tuple(sorted(ALL_JOBS)),
        )

    check_manifests = "manifests" in classification.lanes
    # Only a real selection or a manifest check earns the job; a lane that asked
    # for it but selected nothing (research with no suite here) does not.
    jobs = set(classification.jobs)
    if not selected and not check_manifests:
        jobs.discard(AFFECTED_JOB)
    return replace(
        classification,
        affected_tests=tuple(sorted(selected)),
        check_manifests=check_manifests,
        jobs=tuple(sorted(jobs)),
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
    """Resolve both commits and return what ``head`` changed since they diverged.

    Three dots on purpose. ``pull_request.base.sha`` is the base branch tip, not
    the merge base, so a two-dot diff also reports whatever main gained after the
    branch point: a file added on main shows up as a delete here and forces
    run_all on nearly every PR.
    """

    _resolve_commit(repo_root, base_sha)
    _resolve_commit(repo_root, head_sha)
    diff = _run_git(
        repo_root, "diff", "--name-status", "-z", f"{base_sha}...{head_sha}"
    )
    if diff.returncode != 0:
        raise ClassifierError(
            f"`git diff --name-status {base_sha}...{head_sha}` failed (exit "
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
        "run_affected_tests": "true" if classification.run_affected_tests else "false",
        "check_manifests": "true" if classification.check_manifests else "false",
        # A JSON array, not a shell string: read it through an env var.
        "affected_tests": json.dumps(list(classification.affected_tests)),
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
        "### Change classifier",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in ("result", "reason", "run_all", "lanes", "jobs", "changed_path_count"):
        lines.append(f"| {key} | `{report.get(key)}` |")
    lines.append(f"| affected_tests | `{len(report.get('affected_tests') or [])}` |")
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
        "affected_tests": [],
        "check_manifests": False,
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

        classification = select_affected_tests(
            classification, TestUniverse.from_manifests(Path(args.repo_root).resolve())
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
                "affected_tests": list(classification.affected_tests),
                "check_manifests": classification.check_manifests,
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
            f"jobs={report['jobs']} "
            f"affected_tests={len(report['affected_tests'])}"  # type: ignore[arg-type]
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
