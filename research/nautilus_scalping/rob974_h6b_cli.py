"""ROB-984 H6-B closed CLI and deterministic production identity plan.

``--plan`` is pure and projects the actual merged H4/H6-A identity plus H5
integration provenance.  The retained contract-fixture builder exists only
for the immutable CP1-CP7 call-spy regressions and is never launchable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from io import TextIOBase

import rob974_h6a_diagnostics
import rob974_h6a_smoke

from app.services.rob974_h6b_materializer import (
    AUTHORITY_OR_PREFLIGHT_REFUSED,
    CLI_USAGE_OR_PLAN_ERROR,
    ContractFixturePlan,
    H6BDiagnosticCapture,
    ProductionIdentityPlan,
    compute_exact_48_mapping_hash,
)
from app.services.rob974_h6b_materializer import (
    build_production_identity_plan as _build_production_identity_plan,
)

__all__ = [
    "ActualRob970DiagnosticPort",
    "build_contract_fixture_plan",
    "build_production_identity_plan",
    "main",
    "render_plan_bytes",
    "run_cli",
]

_BOUNDARY_TO_SANITIZER_STAGE = {
    "feature": "generator",
    "generator": "generator",
    "funding_gate": "funding_gate",
    "engine": "engine",
    "metric": "engine",
    "materializer": "engine",
}
_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line ([0-9]+), in ([^\n]+)$')
_RAW_OBJECT_TAIL_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Record|DTO|Event|Bar[0-9A-Za-z_]*)\([^\n]*"
)
_CHAIN_MARKERS = (
    "Traceback (most recent call last):",
    "The above exception was the direct cause",
    "During handling of the above exception",
    "...<truncated>...",
)


def _metadata_only_traceback(traceback_text: str) -> str:
    """Remove every source-context/caret line after ROB-970 sanitization."""
    kept: list[str] = []
    for line in traceback_text.splitlines():
        stripped = line.strip()
        if _FRAME_RE.match(line) or any(
            stripped.startswith(item) for item in _CHAIN_MARKERS
        ):
            kept.append(line)
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):", stripped):
            kept.append(stripped)
    return "\n".join(kept) + "\n"


def _remove_raw_object_repr(text: str) -> str:
    """Tight H6-B projection: raw DTO/record tails are never operator text."""
    return _RAW_OBJECT_TAIL_RE.sub("<redacted-record>", text)


class ActualRob970DiagnosticPort:
    """Projection adapter over the merged ROB-970/H6-A capture authority."""

    provenance = "actual_merged_rob970_h6a"

    def capture_live_exception(
        self,
        exc: BaseException,
        *,
        catch_boundary: str,
        strategy: str,
        config_id: str,
    ) -> H6BDiagnosticCapture:
        try:
            sanitizer_stage = _BOUNDARY_TO_SANITIZER_STAGE[catch_boundary]
        except KeyError as cause:
            raise ValueError("unknown H6-B catch boundary") from cause
        evidence = rob974_h6a_diagnostics.capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage=sanitizer_stage,
            strategy=strategy,
            config_id=config_id,
        )
        safe_traceback = _remove_raw_object_repr(
            _metadata_only_traceback(evidence.traceback_text)
        )
        safe_message = _remove_raw_object_repr(evidence.message)
        frames = [
            match.groups()
            for line in safe_traceback.splitlines()
            if (match := _FRAME_RE.match(line)) is not None
        ]
        if not frames:
            raise ValueError("live diagnostic lacks a sanitized traceback frame")
        filename, line_number, function = frames[-1]
        return H6BDiagnosticCapture(
            catch_boundary=catch_boundary,
            sanitizer_stage=sanitizer_stage,
            exception_type=evidence.exception_type,
            message=safe_message,
            traceback_text=safe_traceback,
            innermost_file=filename,
            innermost_function=function,
            innermost_line=int(line_number),
            signature=evidence.signature,
            occurrence_count=evidence.occurrence_count,
            truncated=evidence.truncated,
            has_cause=exc.__cause__ is not None,
            has_context=exc.__context__ is not None and not exc.__suppress_context__,
        )


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


def build_production_identity_plan() -> ProductionIdentityPlan:
    """Pure actual H4/H6-A identity; H5 remains a pure downstream adapter."""
    return _build_production_identity_plan()


def render_plan_bytes(plan: ContractFixturePlan | ProductionIdentityPlan) -> bytes:
    if type(plan) not in (ContractFixturePlan, ProductionIdentityPlan):
        raise TypeError("plan must be an exact H6-B plan type")
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


def run_cli(argv: Sequence[str], *, stdout: TextIOBase, stderr: TextIOBase) -> int:
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
        stdout.write(
            render_plan_bytes(build_production_identity_plan()).decode("utf-8")
        )
        return 0

    missing = [name for name in _RUN_REQUIRED if getattr(arguments, name) is None]
    if missing:
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    # Physical launch wiring remains closed on the feature branch.  Only the
    # captain's post-merge tree refreeze and SHA-sealed v3 operator packet may
    # bind the empirical session/corpus/output adapters.  Refusal remains
    # before target/session/H4/filesystem effects.
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
