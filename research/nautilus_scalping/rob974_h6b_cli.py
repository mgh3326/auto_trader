"""ROB-984 H6-B closed CLI and pure pre-dependency plan.

Only ``--plan`` is actionable before the verified H4/H5 integration gate.
The plan uses H6-A's real fixture identity/payload APIs and deliberately
contains no production full-campaign hash or campaign run id.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from io import TextIOBase

import rob974_h6a_smoke

from app.services.rob974_h6b_materializer import (
    AUTHORITY_OR_PREFLIGHT_REFUSED,
    CLI_USAGE_OR_PLAN_ERROR,
    ContractFixturePlan,
    compute_exact_48_mapping_hash,
)

__all__ = [
    "build_contract_fixture_plan",
    "main",
    "render_plan_bytes",
    "run_cli",
]


def build_contract_fixture_plan() -> ContractFixturePlan:
    """Pure H6-A-backed fixture plan; no DB/corpus/fs/process/env/clock use."""
    h6a = rob974_h6a_smoke.build_smoke_plan()
    mapping = tuple(
        (spec.row_id, spec.experiment_id) for spec in h6a.envelope.row_specs
    )
    mapping_hash = compute_exact_48_mapping_hash(dict(mapping))
    return ContractFixturePlan(
        ordered_mapping=mapping,
        contract_fixture_mapping_hash=mapping_hash,
        h6a_payload_schema_version=h6a.envelope.schema_version,
        h6a_source_pins=tuple(h6a.envelope.source_pins.as_dict().items()),
        _fixture_campaign_hash=h6a.full_campaign_hash,
        _fixture_run_id=h6a.campaign_run_id,
    )


def render_plan_bytes(plan: ContractFixturePlan) -> bytes:
    if type(plan) is not ContractFixturePlan:
        raise TypeError("plan must be exact ContractFixturePlan")
    return (
        json.dumps(
            plan.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="rob974-h6b", add_help=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--expected-full-campaign-hash")
    parser.add_argument("--campaign-run-id")
    parser.add_argument("--expected-mapping-hash")
    parser.add_argument("--approved-db-host")
    parser.add_argument("--approved-db-port")
    parser.add_argument("--approved-db-name")
    parser.add_argument("--approved-db-user")
    parser.add_argument("--write-opt-in")
    parser.add_argument("--output-root")
    parser.add_argument("--integration-head-sha")
    parser.add_argument("--integration-tree-sha")
    parser.add_argument("--feature-source-sha256")
    parser.add_argument("--engine-source-sha256")
    parser.add_argument("--runner-source-sha256")
    parser.add_argument("--pbo-implementation-sha256")
    parser.add_argument("--one-shot-approval")
    return parser


_RUN_REQUIRED = (
    "expected_full_campaign_hash",
    "campaign_run_id",
    "expected_mapping_hash",
    "approved_db_host",
    "approved_db_port",
    "approved_db_name",
    "approved_db_user",
    "write_opt_in",
    "output_root",
    "integration_head_sha",
    "integration_tree_sha",
    "feature_source_sha256",
    "engine_source_sha256",
    "runner_source_sha256",
    "pbo_implementation_sha256",
    "one_shot_approval",
)


def run_cli(
    argv: Sequence[str], *, stdout: TextIOBase, stderr: TextIOBase
) -> int:
    """Return the closed application exit code; never calls ``sys.exit``."""
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR
    try:
        arguments = _parser().parse_args(list(argv))
    except (ValueError, TypeError):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    if arguments.plan:
        disallowed = [name for name in _RUN_REQUIRED if getattr(arguments, name)]
        if disallowed:
            stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
            return CLI_USAGE_OR_PLAN_ERROR
        stdout.write(render_plan_bytes(build_contract_fixture_plan()).decode("utf-8"))
        return 0

    missing = [name for name in _RUN_REQUIRED if getattr(arguments, name) is None]
    if missing:
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    # Even a syntactically complete run request is refused before any target,
    # session, H4 child, or filesystem interaction until actual H4/H5 replace
    # the fixture plan at CP8.
    build_contract_fixture_plan()
    stderr.write("AUTHORITY_OR_PREFLIGHT_REFUSED contract_fixture_not_launchable\n")
    return AUTHORITY_OR_PREFLIGHT_REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        tuple(sys.argv[1:] if argv is None else argv),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())

