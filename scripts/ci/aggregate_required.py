#!/usr/bin/env python3
"""Fail-closed aggregator behind the stable ``ci-required`` check.

ROB-1294 (R2B). Branch protection today names six checks directly (``lint``,
``taskiq-smoke``, ``test (3.13, 1..4)``), so any change to shard count or lane
topology is a branch-protection edit. This script is the evaluation half of a
single fixed-name check that could later stand in for all of them.

**This PR does not make ``ci-required`` a required check.** See
``docs/runbooks/ci-required-aggregator.md`` for the operator-only cutover.

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


def evaluate(
    results: Mapping[str, object],
    required: Sequence[str],
    authorized_skips: Iterable[str] = (),
    *,
    allow_undeclared: bool = False,
) -> AggregateVerdict:
    """Apply the truth table above. Pure: no IO, no environment."""

    authorized = set(authorized_skips)
    unknown_authorized = authorized - set(required)
    if unknown_authorized:
        raise AggregateError(
            "--authorize-skip names jobs that are not required: "
            f"{sorted(unknown_authorized)}"
        )

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
        f"### ci-required aggregate: **{str(report.get('result', '')).upper()}**",
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
        if not args.required:
            raise AggregateError(
                "no --required job names given; refusing to run an aggregate "
                "that vacuously passes."
            )
        results = load_results(args)
        verdict = evaluate(
            results,
            args.required,
            args.authorize_skip,
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
                "CI-REQUIRED: PASS — "
                f"{len(verdict.children)} child result(s) all acceptable"
            )
        else:
            detail = ", ".join(
                f"{child.name}={child.result!r}({child.status})"
                for child in verdict.failing
            )
            print(f"CI-REQUIRED: FAIL — {detail}", file=sys.stderr)
    except (AggregateError, OSError) as exc:
        report["result"] = "fail"
        report["error"] = str(exc)
        exit_code = 1
        print(f"CI-REQUIRED: FAIL — {exc}", file=sys.stderr)

    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.json_out:
        Path(args.json_out).write_text(serialized + "\n", encoding="utf-8")
    if args.summary:
        _write_step_summary(_summary_lines(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
