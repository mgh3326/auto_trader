"""Default-disabled CRS-24 CLI with a pure plan and closed run boundary."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from io import TextIOBase

from rob1040_crs24_contracts import (
    CONTRACT_SHA256,
    FILTER_MANIFEST_SHA256,
    FOLD_SCHEDULE_SHA256,
    PREREGISTRATION_SHA256,
    contract_payload,
    validate_contract,
)

CLI_USAGE_ERROR = 2
RUN_AUTHORITY_CLOSED = 78


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="rob1040-crs24", add_help=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    return parser


def plan_payload() -> dict[str, object]:
    validate_contract()
    contract = contract_payload()
    return {
        "schema_version": "rob1040.crs24.corr1.launch_plan.v1",
        "posture": "implementation_only",
        "launch_state": "closed_pending_merge_refreeze_and_separate_approval",
        "allowed_now": ["synthetic_unit", "read_only_static_check"],
        "authorities": {
            "preregistration_sha256": PREREGISTRATION_SHA256,
            "contract_sha256": CONTRACT_SHA256,
            "filter_manifest_sha256": FILTER_MANIFEST_SHA256,
            "fold_schedule_sha256": FOLD_SCHEDULE_SHA256,
        },
        "universe": contract["universe"],
        "config_slots": contract["config_slots"],
        "calendar": {
            "authority": contract["calendar"]["authority"],
            "fold_count": len(contract["calendar"]["folds"]),
            "scheduled_per_fold": contract["calendar"]["scheduled_per_fold"],
            "horizon_eligible_per_fold": contract["calendar"][
                "horizon_eligible_per_fold"
            ],
            "fold_horizon_closed_per_fold": contract["calendar"][
                "fold_horizon_closed_per_fold"
            ],
        },
    }


def render_plan_bytes() -> bytes:
    return (
        json.dumps(
            plan_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def run_cli(
    argv: Sequence[str],
    *,
    stdout: TextIOBase,
    stderr: TextIOBase,
) -> int:
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        stderr.write("CLI_USAGE_ERROR\n")
        return CLI_USAGE_ERROR
    try:
        arguments = _parser().parse_args(list(argv))
    except (TypeError, ValueError):
        stderr.write("CLI_USAGE_ERROR\n")
        return CLI_USAGE_ERROR
    if arguments.run:
        stderr.write(
            "RUN_AUTHORITY_CLOSED merge_refreeze_and_separate_approval_required\n"
        )
        return RUN_AUTHORITY_CLOSED
    stdout.write(render_plan_bytes().decode("utf-8"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        tuple(sys.argv[1:] if argv is None else argv),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLI_USAGE_ERROR",
    "RUN_AUTHORITY_CLOSED",
    "main",
    "plan_payload",
    "render_plan_bytes",
    "run_cli",
]
