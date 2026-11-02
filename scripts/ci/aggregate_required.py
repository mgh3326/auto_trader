#!/usr/bin/env python3
"""Fail-closed evaluator behind the stable ``PR gate`` check.

ROB-1294 built this in shadow as ``ci-required``; ROB-1454 makes it the verdict
a PR merges on. ``--pr-gate`` derives which skips are authorized from the
classifier's own outputs, so a job counts as skipped-by-design only when the
classifier said it was not needed. See ``docs/runbooks/ci-required-aggregator.md``.

Truth table (per required child)
--------------------------------
==================  ==============================================
child result        verdict
==================  ==============================================
``success``         pass
``skipped``         pass **only** if the name was passed to
                    ``--authorize-skip``; otherwise red
``failure``         red
``cancelled``       red
absent from input   red (``missing``)
anything else       red (``unexpected``) -- includes ``""``, ``null``,
                    ``neutral`` and any future GitHub result string
==================  ==============================================

A child present in the input but not declared via ``--required`` is also red
(``undeclared``): that means the workflow's ``needs:`` list and this script's
required list have drifted apart, and drift is exactly what a stable gate
must not absorb quietly. ``--allow-undeclared`` opts out.

Malformed input (non-JSON, non-object, a child whose value is neither a
string nor an object carrying ``result``) is red, never green.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Child results that are individually acceptable without an explicit
#: authorization. Deliberately a one-element set.
PASSING_RESULTS: frozenset[str] = frozenset({"success"})

#: Job ids in ``.github/workflows/test.yml`` that the PR gate reasons about.
CLASSIFIER_JOB = "change-classifier"
AFFECTED_JOB = "affected-tests"
FULL_SUITE_ONLY_JOBS: tuple[str, ...] = ("test", "taskiq-smoke")

#: Results we recognise but treat as red unless authorized.
KNOWN_RESULTS: frozenset[str] = frozenset(
    {"success", "failure", "cancelled", "skipped"}
)


class AggregateError(RuntimeError):
    """Malformed input. Always red."""


@dataclass
class ChildVerdict:
    name: str
    result: str | None
    status: str  # pass | failure | cancelled | unauthorized_skip | missing
    # | unexpected | undeclared
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class AggregateVerdict:
    children: list[ChildVerdict] = field(default_factory=list)

    @property
    def failing(self) -> list[ChildVerdict]:
        return [child for child in self.children if not child.ok]

    @property
    def passed(self) -> bool:
        return not self.failing

    @property
    def result(self) -> str:
        return "pass" if self.passed else "fail"


def _extract_result(name: str, raw: object) -> str | None:
    """Pull the ``result`` string out of one ``needs.<job>`` value."""

    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        if "result" not in raw:
            return None
        value = raw["result"]
        if value is None:
            return None
        if not isinstance(value, str):
            raise AggregateError(
                f"child {name!r} has a non-string result {value!r}; refusing to guess."
            )
        return value
    raise AggregateError(
        f"child {name!r} has an unsupported shape {type(raw).__name__}; expected "
        "an object with a `result` field or a bare result string."
    )


def _validate_names(kind: str, names: Sequence[str]) -> None:
    """Reject blank and duplicate job names before anything can pass.

    argparse happily turns ``--required ''`` into a one-element list, which
    used to satisfy the "refusing to run a vacuous aggregate" check while
    naming no real job; and a repeated ``--required lint`` used to be counted
    twice. Both are configuration errors, not children.
    """

    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise AggregateError(
                f"{kind} contains a blank or whitespace-only job name "
                f"({name!r}); a gate cannot be anchored to an unnamed job."
            )
        if name in seen:
            raise AggregateError(
                f"{kind} contains the duplicate job name {name!r}; each "
                "required child must be declared exactly once."
            )
        seen.add(name)


def validate_configuration(
    required: Sequence[str], authorized_skips: Sequence[str] = ()
) -> None:
    """Fail closed on a malformed required/authorized-skip declaration."""

    if not required:
        raise AggregateError(
            "no --required job names given; refusing to run an aggregate "
            "that vacuously passes."
        )
    _validate_names("--required", required)
    _validate_names("--authorize-skip", authorized_skips)
    unknown_authorized = set(authorized_skips) - set(required)
    if unknown_authorized:
        raise AggregateError(
            "--authorize-skip names jobs that are not required: "
            f"{sorted(unknown_authorized)}"
        )


def evaluate(
    results: Mapping[str, object],
    required: Sequence[str],
    authorized_skips: Sequence[str] = (),
    *,
    allow_undeclared: bool = False,
) -> AggregateVerdict:
    """Apply the truth table above. Pure: no IO, no environment."""

    validate_configuration(required, authorized_skips)
    authorized = set(authorized_skips)
    verdict = AggregateVerdict()
    for name in required:
        if name not in results:
            verdict.children.append(
                ChildVerdict(
                    name=name,
                    result=None,
                    status="missing",
                    note="declared required but absent from the results payload",
                )
            )
            continue
        result = _extract_result(name, results[name])
        if result in PASSING_RESULTS:
            verdict.children.append(ChildVerdict(name, result, "pass"))
        elif result == "skipped":
            if name in authorized:
                verdict.children.append(
                    ChildVerdict(name, result, "pass", "authorized skip")
                )
            else:
                verdict.children.append(
                    ChildVerdict(
                        name,
                        result,
                        "unauthorized_skip",
                        "skipped without --authorize-skip; a skipped required "
                        "child is not coverage",
                    )
                )
        elif result == "failure":
            verdict.children.append(ChildVerdict(name, result, "failure"))
        elif result == "cancelled":
            verdict.children.append(
                ChildVerdict(
                    name, result, "cancelled", "cancelled is not a passing result"
                )
            )
        else:
            verdict.children.append(
                ChildVerdict(
                    name,
                    result,
                    "unexpected",
                    f"unrecognised result {result!r}; known results are "
                    f"{sorted(KNOWN_RESULTS)}",
                )
            )

    if not allow_undeclared:
        for name in sorted(set(results) - set(required)):
            verdict.children.append(
                ChildVerdict(
                    name=name,
                    result=_extract_result(name, results[name]),
                    status="undeclared",
                    note="present in the results payload but not declared "
                    "--required; the workflow `needs:` list and the required "
                    "list have drifted",
                )
            )

    return verdict


def _classifier_flag(outputs: Mapping[str, object], key: str) -> bool:
    value = outputs.get(key)
    if value not in ("true", "false"):
        raise AggregateError(
            f"{CLASSIFIER_JOB} output {key!r} is {value!r}, not 'true' or 'false'; "
            "refusing to infer that a job was not needed."
        )
    return value == "true"


def pr_gate_authorized_skips(
    event_name: str, results: Mapping[str, object]
) -> tuple[str, ...]:
    """Which skips this run's classifier verdict makes legitimate.

    ==================================  ==========================================
    run                                 may be skipped
    ==================================  ==========================================
    not a pull request (main, dispatch)  ``affected-tests``
    PR, ``run_all=true``                ``affected-tests``
    PR, narrow, tests selected          ``test``, ``taskiq-smoke``
    PR, narrow, nothing to test         the above and ``affected-tests``
    ==================================  ==========================================

    Everything else must report ``success``, so the full suite can never be
    skipped into green on a run that needed it.
    """

    if not event_name.strip():
        raise AggregateError("--event-name is empty; cannot tell which rule applies.")
    if event_name != "pull_request":
        return (AFFECTED_JOB,)
    classifier = results.get(CLASSIFIER_JOB)
    outputs = classifier.get("outputs") if isinstance(classifier, Mapping) else None
    if not isinstance(outputs, Mapping):
        raise AggregateError(
            f"{CLASSIFIER_JOB} has no outputs in the results payload; a skip cannot "
            "be authorized without its verdict."
        )
    if _classifier_flag(outputs, "run_all"):
        return (AFFECTED_JOB,)
    if _classifier_flag(outputs, "run_affected_tests"):
        return FULL_SUITE_ONLY_JOBS
    return (*FULL_SUITE_ONLY_JOBS, AFFECTED_JOB)


def load_results(args: argparse.Namespace) -> Mapping[str, object]:
    sources = [
        bool(args.results_json),
        bool(args.results_env),
        bool(args.results_file),
    ]
    if sum(sources) != 1:
        raise AggregateError(
            "exactly one of --results-json / --results-env / --results-file is "
            "required."
        )
    if args.results_json:
        payload = args.results_json
        origin = "--results-json"
    elif args.results_env:
        origin = f"${args.results_env}"
        raw = os.environ.get(args.results_env)
        if raw is None:
            raise AggregateError(f"environment variable {origin} is not set.")
        payload = raw
    else:
        origin = str(args.results_file)
        payload = Path(args.results_file).read_text(encoding="utf-8")

    if not payload.strip():
        raise AggregateError(f"{origin} is empty; refusing to treat that as green.")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AggregateError(f"{origin} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AggregateError(
            f"{origin} must decode to an object mapping job name -> result, got "
            f"{type(parsed).__name__}."
        )
    return parsed


def _summary_lines(report: Mapping[str, object]) -> list[str]:
    lines = [
        f"### PR gate: **{str(report.get('result', '')).upper()}**",
        "",
        "| child | result | verdict | note |",
        "|---|---|---|---|",
    ]
    children = report.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            lines.append(
                f"| `{child.get('name')}` | `{child.get('result')}` | "
                f"`{child.get('status')}` | {child.get('note') or ''} |"
            )
    if report.get("error"):
        lines += ["", f"**error:** `{report['error']}`"]
    return lines


def _write_step_summary(lines: Iterable[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-json", help="inline JSON results payload")
    parser.add_argument(
        "--results-env", help="name of an env var holding the JSON payload"
    )
    parser.add_argument("--results-file", help="file holding the JSON payload")
    parser.add_argument(
        "--required",
        action="append",
        default=[],
        help="a required child job name (repeatable)",
    )
    parser.add_argument(
        "--authorize-skip",
        action="append",
        default=[],
        help="a required child whose `skipped` result is explicitly acceptable "
        "(repeatable). Everything else that is skipped is red.",
    )
    parser.add_argument(
        "--allow-undeclared",
        action="store_true",
        help="do not fail on children present in the payload but not --required",
    )
    parser.add_argument(
        "--pr-gate",
        action="store_true",
        help="derive the authorized skips from the classifier outputs in the "
        "payload (needs --event-name); cannot be combined with --authorize-skip",
    )
    parser.add_argument(
        "--event-name", default="", help="github.event_name, read by --pr-gate"
    )
    parser.add_argument("--json-out", help="write the machine-readable report here")
    parser.add_argument(
        "--summary", action="store_true", help="append a $GITHUB_STEP_SUMMARY table"
    )
    args = parser.parse_args(argv)

    report: dict[str, object] = {
        "result": "fail",
        "required": list(args.required),
        "authorized_skips": sorted(set(args.authorize_skip)),
        "children": [],
        "failing": [],
        "error": None,
    }
    exit_code = 1

    try:
        # Validated before the payload is even read: a malformed gate
        # declaration must never reach a verdict.
        validate_configuration(args.required, args.authorize_skip)
        if args.pr_gate and args.authorize_skip:
            raise AggregateError("--pr-gate and --authorize-skip are exclusive.")
        results = load_results(args)
        authorized = (
            pr_gate_authorized_skips(args.event_name, results)
            if args.pr_gate
            else tuple(args.authorize_skip)
        )
        report["authorized_skips"] = sorted(authorized)
        verdict = evaluate(
            results,
            args.required,
            authorized,
            allow_undeclared=args.allow_undeclared,
        )
        report["children"] = [
            {
                "name": child.name,
                "result": child.result,
                "status": child.status,
                "note": child.note,
            }
            for child in verdict.children
        ]
        report["failing"] = [child.name for child in verdict.failing]
        report["result"] = verdict.result
        exit_code = 0 if verdict.passed else 1
        if verdict.passed:
            print(
                "PR GATE: PASS — "
                f"{len(verdict.children)} child result(s) all acceptable"
            )
        else:
            detail = ", ".join(
                f"{child.name}={child.result!r}({child.status})"
                for child in verdict.failing
            )
            print(f"PR GATE: FAIL — {detail}", file=sys.stderr)
    except (AggregateError, OSError) as exc:
        report["result"] = "fail"
        report["error"] = str(exc)
        exit_code = 1
        print(f"PR GATE: FAIL — {exc}", file=sys.stderr)

    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.json_out:
        Path(args.json_out).write_text(serialized + "\n", encoding="utf-8")
    if args.summary:
        _write_step_summary(_summary_lines(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
