"""Scheduleless CLI for one frozen US Stage-B candidate against explicit year roots.

This entry point is operator-owned and has no TaskIQ/cron/Prefect registration.
It refuses holdout/staging roots, 2025+ exploration bounds, unknown candidates,
contract-hash drift, output overwrites, and any input-mutation request.  Output
is written atomically via a temporary ``.partial`` sibling, streaming JSON so
the full payload string is never held in RAM twice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from .contracts import (
    US_EXPLORATION_END,
    US_EXPLORATION_START,
    US_HOLDOUT_START,
    USCostLiteral,
    USStageBRunContract,
)
from .engine import run_us_stage_b
from .parquet_source import (
    CorpusPathAccessError,
    ParquetUSBarSource,
    PathAccessSpy,
    assert_year_root_allowed,
    is_forbidden_corpus_path,
)
from .registry import CandidateRegistry, RegistryStartRejected

__all__ = [
    "CliRejection",
    "atomic_write_json",
    "build_parser",
    "main",
    "run_from_args",
]


class CliRejection(ValueError):
    """A CLI argument or gate failed closed before any economic side effect."""


@dataclass(frozen=True)
class CliArgs:
    candidate_id: str
    contract_hash: str
    candidates_yaml: Path
    year_roots: tuple[Path, ...]
    exploration_start: date
    exploration_end: date
    output: Path
    mutate_input: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.us_stage_b.cli",
        description=(
            "Run one frozen US Stage-B candidate against explicit exploration "
            "year Parquet roots (scheduleless, read-only corpus)."
        ),
    )
    parser.add_argument(
        "--candidate-id",
        required=True,
        help="Exact frozen strategy_id (no family/name fallback).",
    )
    parser.add_argument(
        "--contract-hash",
        required=True,
        help="Raw YAML list-item SHA-256 that must match the registry binding.",
    )
    parser.add_argument(
        "--candidates-yaml",
        type=Path,
        required=True,
        help="Path to the frozen 02-active-candidates.yaml packet.",
    )
    parser.add_argument(
        "--year-root",
        type=Path,
        action="append",
        dest="year_roots",
        required=True,
        help=(
            "Explicit allowlisted year directory (year=2016..year=2024). "
            "Repeat once per year; parent globs and holdout/staging are refused."
        ),
    )
    parser.add_argument(
        "--exploration-start",
        type=_parse_date,
        required=True,
        help="Inclusive exploration start date (YYYY-MM-DD), >= 2016-01-01.",
    )
    parser.add_argument(
        "--exploration-end",
        type=_parse_date,
        required=True,
        help="Inclusive exploration end date (YYYY-MM-DD), <= 2024-12-31.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="JSON output path (must not already exist; written atomically).",
    )
    parser.add_argument(
        "--mutate-input",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(list(argv) if argv is not None else None)
    args = CliArgs(
        candidate_id=namespace.candidate_id,
        contract_hash=namespace.contract_hash,
        candidates_yaml=Path(namespace.candidates_yaml),
        year_roots=tuple(Path(root) for root in namespace.year_roots),
        exploration_start=namespace.exploration_start,
        exploration_end=namespace.exploration_end,
        output=Path(namespace.output),
        mutate_input=bool(namespace.mutate_input),
    )
    try:
        result_path = run_from_args(args)
    except CliRejection as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    except (CorpusPathAccessError, RegistryStartRejected) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    print(str(result_path))
    return 0


def run_from_args(args: CliArgs) -> Path:
    """Execute one candidate run and atomically publish the JSON artifact."""

    if args.mutate_input:
        raise CliRejection("input mutation request refused")

    _validate_exploration_bounds(args.exploration_start, args.exploration_end)
    _validate_year_roots(args.year_roots)
    _validate_output_path(args.output, year_roots=args.year_roots)
    _cleanup_stranded_partials(args.output)

    try:
        registry = CandidateRegistry.load(args.candidates_yaml)
        binding = registry.binding_for(args.candidate_id)
    except RegistryStartRejected as exc:
        raise CliRejection(str(exc)) from exc

    if binding.contract_hash != args.contract_hash:
        raise CliRejection(
            "contract-hash mismatch: "
            f"expected={binding.contract_hash} provided={args.contract_hash}"
        )

    access_spy = PathAccessSpy()
    source = ParquetUSBarSource.from_year_roots(
        args.year_roots,
        access_spy=access_spy,
    )
    sessions = tuple(
        session
        for session in source.corpus_sessions()
        if args.exploration_start <= session <= args.exploration_end
    )
    if not sessions:
        raise CliRejection(
            "no corpus sessions remain inside the explicit exploration window"
        )

    symbols = source.symbols()
    _emit_scale_estimate(
        sessions=len(sessions),
        symbols=len(symbols),
        rows_loaded=int(source.access_summary().get("rows_loaded", 0)),
        candidate_id=args.candidate_id,
    )

    contract = USStageBRunContract(
        candidate=binding,
        exploration_start=args.exploration_start,
        exploration_end=args.exploration_end,
        cost=USCostLiteral(base_bp_per_side=10, sensitivity_bp_per_side=5),
    )
    started = time.perf_counter()
    run_result = run_us_stage_b(
        source=source,
        contract=contract,
        corpus_sessions=sessions,
    )
    elapsed = time.perf_counter() - started
    payload = run_result.to_dict()
    payload["adapter"] = {
        "kind": "parquet_us_bar_source",
        "year_roots": [str(root) for root in args.year_roots],
        "path_access_summary": source.access_summary(),
        "engine_access_summary": dict(run_result.access_summary),
        "scale_estimate": {
            "sessions": len(sessions),
            "symbols": len(symbols),
            "cell_evals": len(sessions) * len(symbols),
            "rows_loaded": int(source.access_summary().get("rows_loaded", 0)),
        },
        "engine_wall_seconds": elapsed,
        "ORDERS": 0,
        "ACCOUNT_CONTACT": 0,
        "DB_WRITES": 0,
        "SCHEDULER": 0,
        "DEPLOY": 0,
        "HOLDOUT_READS": 0,
        "FORBIDDEN_ROOT_ENUMERATIONS": access_spy.summary()[
            "forbidden_root_enumerations"
        ],
    }
    if access_spy.forbidden_root_enumerations():
        raise CliRejection("forbidden root was enumerated during load")
    atomic_write_json(args.output, payload)
    return args.output


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Stream JSON to a unique ``.partial``, fsync, then ``os.replace``.

    Overwrites of the final path are refused.  A stranded partial left by an
    earlier SIGKILL/OOM is removed before a new attempt (SHOULD-2).  The full
    payload is never materialised as a second Python ``str`` + ``bytes`` pair;
    ``json.dump`` writes directly to the file object.
    """

    if path.exists():
        raise CliRejection(f"output overwrite refused: {path}")
    if is_forbidden_corpus_path(path):
        raise CliRejection(f"output path under forbidden corpus segment: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _cleanup_stranded_partials(path)
    partial = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.partial")
    try:
        with partial.open("w", encoding="utf-8") as handle:
            _dump_json(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise CliRejection(f"output overwrite refused: {path}")
        os.replace(partial, path)
    except Exception:
        if partial.exists():
            partial.unlink(missing_ok=True)
        raise


def _dump_json(payload: dict[str, Any], handle: TextIO) -> None:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")


def _cleanup_stranded_partials(path: Path) -> None:
    """Remove orphaned partial siblings for this output basename.

    SIGKILL/OOM can leave ``*.partial`` files because process-local ``except``
    handlers never run.  Cleaning them before a new write prevents silent
    disk clutter; the final artifact path remains no-overwrite.
    """

    parent = path.parent
    if not parent.is_dir():
        return
    legacy = path.with_name(path.name + ".partial")
    if legacy.exists() and legacy.is_file():
        legacy.unlink(missing_ok=True)
    prefix = f"{path.name}."
    for child in parent.iterdir():
        if not child.is_file():
            continue
        if child.name.startswith(prefix) and child.name.endswith(".partial"):
            child.unlink(missing_ok=True)


def _emit_scale_estimate(
    *,
    sessions: int,
    symbols: int,
    rows_loaded: int,
    candidate_id: str,
) -> None:
    cell_evals = sessions * symbols
    print(
        "SCALE_ESTIMATE "
        f"candidate={candidate_id} "
        f"sessions={sessions} "
        f"symbols={symbols} "
        f"cell_evals={cell_evals} "
        f"rows_loaded={rows_loaded}",
        file=sys.stderr,
    )


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {raw!r}") from exc


def _validate_exploration_bounds(start: date, end: date) -> None:
    if start > end:
        raise CliRejection("exploration_start is after exploration_end")
    if start < US_EXPLORATION_START:
        raise CliRejection("exploration starts no earlier than 2016-01-01")
    if end > US_EXPLORATION_END or end >= US_HOLDOUT_START:
        raise CliRejection("exploration window intersects the 2025+ sealed holdout")


def _validate_year_roots(year_roots: Sequence[Path]) -> None:
    if not year_roots:
        raise CliRejection("at least one --year-root is required")
    seen: set[int] = set()
    for root in year_roots:
        if is_forbidden_corpus_path(root):
            raise CliRejection(f"holdout or staging path refused as year root: {root}")
        try:
            year = assert_year_root_allowed(root)
        except CorpusPathAccessError as exc:
            raise CliRejection(str(exc)) from exc
        if year in seen:
            raise CliRejection(f"duplicate --year-root for year={year}")
        seen.add(year)


def _validate_output_path(output: Path, *, year_roots: Sequence[Path]) -> None:
    if output.exists():
        raise CliRejection(f"output overwrite refused: {output}")
    if is_forbidden_corpus_path(output):
        raise CliRejection(f"output path under forbidden corpus segment: {output}")
    try:
        resolved_output = output.resolve(strict=False)
    except OSError as exc:
        raise CliRejection(f"cannot resolve output path: {output}") from exc
    for root in year_roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            continue
        if resolved_output == resolved_root or resolved_root in resolved_output.parents:
            raise CliRejection(
                "output path must not write into a corpus year root: "
                f"{output} under {root}"
            )
        if resolved_output in resolved_root.parents:
            raise CliRejection(
                "output path must not be an ancestor of a corpus year root"
            )


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
