"""Durable, hash-bound driver for the US-U1 exploratory falsification runs.

This module is the only supported U1-B entrypoint.  It pins:
- the three frozen candidate IDs and their raw-block contract hashes,
- the nine exploration year roots (2016–2024) under an explicit corpus root,
- the exploration window 2016-01-01..2024-12-31,
- the frozen candidates YAML path (SHA-verified by CandidateRegistry).

It does not read holdout/staging, does not register a scheduler, and does not
retry or substitute a failed candidate.  Each candidate is invoked exactly once
through ``research.us_stage_b.cli.run_from_args``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from .cli import CliArgs, CliRejection, run_from_args
from .parquet_source import CorpusPathAccessError
from .registry import (
    FROZEN_CANDIDATES_SHA256,
    US_CANDIDATE_ORDER,
    CandidateRegistry,
    RegistryStartRejected,
)

__all__ = [
    "ALLOWED_YEARS",
    "CANDIDATE_SPECS",
    "EXPLORATION_END",
    "EXPLORATION_START",
    "build_parser",
    "driver_blob_sha256",
    "main",
    "run_exploration",
]

EXPLORATION_START: Final = date(2016, 1, 1)
EXPLORATION_END: Final = date(2024, 12, 31)
ALLOWED_YEARS: Final[tuple[int, ...]] = tuple(range(2016, 2025))

# Contract hashes from the frozen packet (raw YAML list-item digests).
CANDIDATE_SPECS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "US-TS-MOM-CONT-Z126-H20-v1",
        "25d06a5e81c03a254948ee20b9180d466e5531a63715dd1ba53b7160ce032509",
        "us-ts-mom-cont-z126-h20-v1.json",
    ),
    (
        "US-TS-REV-SHORT-Z3-T126-H3-v1",
        "7052f69ae1ae20de53750276c006959694941a1b2a430c99c8dbbe616cba9836",
        "us-ts-rev-short-z3-t126-h3-v1.json",
    ),
    (
        "US-TS-VOLBREAK-C55-V2-H10-v1",
        "43ff9a4a99ba3717c2d5563aa58c8a482800082c4fa4c41330712c4460848b0f",
        "us-ts-volbreak-c55-v2-h10-v1.json",
    ),
)

_DEFAULT_CORPUS_ROOT: Final = Path(
    "/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/dataset/market=us"
)


@dataclass(frozen=True)
class DriverConfig:
    corpus_root: Path
    candidates_yaml: Path
    output_dir: Path
    run_log: Path
    engine_commit: str
    engine_tree: str


def driver_blob_sha256() -> str:
    """SHA-256 of this driver's source file (hash-bound evidence)."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def adapter_blob_sha256() -> str:
    package = Path(__file__).resolve().parent
    payloads = b"".join(
        sorted(
            (path.name.encode() + b"\0" + path.read_bytes())
            for path in package.glob("*.py")
        )
    )
    return hashlib.sha256(payloads).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.us_stage_b.u1_exploration_driver",
        description=(
            "US-U1 exploratory falsification: exactly three frozen candidates, "
            "one trial each, nine year roots only."
        ),
    )
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=_DEFAULT_CORPUS_ROOT,
        help=(
            "Parent directory that contains year=2016..year=2024 only "
            f"(default: {_DEFAULT_CORPUS_ROOT})"
        ),
    )
    parser.add_argument(
        "--candidates-yaml",
        type=Path,
        required=True,
        help="Frozen 02-active-candidates.yaml (SHA-verified).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for per-candidate JSON, manifest, and checksums.",
    )
    parser.add_argument(
        "--run-log",
        type=Path,
        required=True,
        help="Append-only run.md path (updated after each candidate).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(list(argv) if argv is not None else None)
    engine_commit, engine_tree = _git_identity()
    config = DriverConfig(
        corpus_root=Path(namespace.corpus_root),
        candidates_yaml=Path(namespace.candidates_yaml),
        output_dir=Path(namespace.output_dir),
        run_log=Path(namespace.run_log),
        engine_commit=engine_commit,
        engine_tree=engine_tree,
    )
    try:
        run_exploration(config)
    except (
        CliRejection,
        CorpusPathAccessError,
        RegistryStartRejected,
        RuntimeError,
    ) as exc:
        _append_run_log(
            config.run_log,
            f"\n## DRIVER_FATAL\n\n```\n{exc}\n```\n"
            f"ORDERS=0 / ACCOUNT_CONTACT=0 / DB_WRITES=0 / "
            f"SCHEDULER=0 / DEPLOY=0 / HOLDOUT_READS=0\n",
        )
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


def run_exploration(config: DriverConfig) -> Mapping[str, Any]:
    """Run the three candidates once each; append run.md after every finish."""

    year_roots = _resolve_year_roots(config.corpus_root)
    registry = CandidateRegistry.load(config.candidates_yaml)
    if registry.source_packet_sha256 != FROZEN_CANDIDATES_SHA256:
        raise RuntimeError(
            "candidates YAML SHA drift: "
            f"expected={FROZEN_CANDIDATES_SHA256} "
            f"actual={registry.source_packet_sha256}"
        )
    for strategy_id, expected_hash, _filename in CANDIDATE_SPECS:
        if strategy_id not in US_CANDIDATE_ORDER:
            raise RuntimeError(f"candidate order drift: {strategy_id}")
        binding = registry.binding_for(strategy_id)
        if binding.contract_hash != expected_hash:
            raise RuntimeError(
                f"contract-hash mismatch for {strategy_id}: "
                f"expected={expected_hash} actual={binding.contract_hash}"
            )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    _write_run_header(config, year_roots=year_roots, started_at=started_at)

    candidate_records: list[dict[str, Any]] = []
    for strategy_id, contract_hash, filename in CANDIDATE_SPECS:
        output_path = config.output_dir / filename
        record = _run_one_candidate(
            config=config,
            strategy_id=strategy_id,
            contract_hash=contract_hash,
            year_roots=year_roots,
            output_path=output_path,
        )
        candidate_records.append(record)
        _append_candidate_section(config.run_log, record)

    finished_at = datetime.now(UTC)
    manifest = _build_manifest(
        config=config,
        year_roots=year_roots,
        started_at=started_at,
        finished_at=finished_at,
        candidate_records=candidate_records,
    )
    manifest_path = config.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums_path = config.output_dir / "checksums.sha256"
    checksum_lines = _write_checksums(
        config.output_dir,
        filenames=[spec[2] for spec in CANDIDATE_SPECS] + ["manifest.json"],
    )
    checksums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    _append_run_log(
        config.run_log,
        _footer(
            finished_at=finished_at,
            manifest=manifest,
            checksums_path=checksums_path,
        ),
    )
    return manifest


def _run_one_candidate(
    *,
    config: DriverConfig,
    strategy_id: str,
    contract_hash: str,
    year_roots: Sequence[Path],
    output_path: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "research.us_stage_b.cli",
        "--candidate-id",
        strategy_id,
        "--contract-hash",
        contract_hash,
        "--candidates-yaml",
        str(config.candidates_yaml.resolve()),
        *[item for root in year_roots for item in ("--year-root", str(root))],
        "--exploration-start",
        EXPLORATION_START.isoformat(),
        "--exploration-end",
        EXPLORATION_END.isoformat(),
        "--output",
        str(output_path),
    ]
    started = datetime.now(UTC)
    started_mono = time.perf_counter()
    status = "ok"
    error: str | None = None
    summary: dict[str, Any] = {}
    try:
        if output_path.exists():
            raise CliRejection(
                f"output already exists (no overwrite / no retry): {output_path}"
            )
        run_from_args(
            CliArgs(
                candidate_id=strategy_id,
                contract_hash=contract_hash,
                candidates_yaml=config.candidates_yaml,
                year_roots=tuple(year_roots),
                exploration_start=EXPLORATION_START,
                exploration_end=EXPLORATION_END,
                output=output_path,
                mutate_input=False,
            )
        )
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        summary = _extract_summary(payload)
    except Exception as exc:  # noqa: BLE001 — persist failure as-is, no retry
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    finished = datetime.now(UTC)
    wall_seconds = time.perf_counter() - started_mono
    file_sha = (
        hashlib.sha256(output_path.read_bytes()).hexdigest()
        if output_path.is_file()
        else None
    )
    return {
        "strategy_id": strategy_id,
        "contract_hash": contract_hash,
        "output_path": str(output_path),
        "output_filename": output_path.name,
        "status": status,
        "error": error,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "wall_seconds": wall_seconds,
        "command": command,
        "artifact_sha256": file_sha,
        "summary": summary,
    }


def _extract_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    adapter = payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    obs = (
        payload.get("observation_summary")
        if isinstance(payload.get("observation_summary"), dict)
        else {}
    )
    return {
        "labels": list(payload.get("labels") or []),
        "verdict_state": verdict.get("state"),
        "promotion": verdict.get("promotion"),
        "run_invalid": payload.get("run_invalid"),
        "invalid_reasons": list(payload.get("invalid_reasons") or []),
        "outcome_status_counts": dict(payload.get("outcome_status_counts") or {}),
        "observation_summary": dict(obs),
        "scale_estimate": dict(adapter.get("scale_estimate") or {}),
        "engine_wall_seconds": adapter.get("engine_wall_seconds"),
        "path_access_summary": dict(adapter.get("path_access_summary") or {}),
        "engine_access_summary": dict(adapter.get("engine_access_summary") or {}),
        "cost_literal": dict((payload.get("contract") or {}).get("cost_literal") or {}),
        "cost_profile_verdicts": payload.get("cost_profile_verdicts"),
        "ORDERS": adapter.get("ORDERS", payload.get("orders", 0)),
        "ACCOUNT_CONTACT": adapter.get("ACCOUNT_CONTACT", 0),
        "DB_WRITES": adapter.get("DB_WRITES", payload.get("database_writes", 0)),
        "SCHEDULER": adapter.get("SCHEDULER", 0),
        "DEPLOY": adapter.get("DEPLOY", 0),
        "HOLDOUT_READS": adapter.get("HOLDOUT_READS", 0),
        "FORBIDDEN_ROOT_ENUMERATIONS": adapter.get("FORBIDDEN_ROOT_ENUMERATIONS", 0),
        "config_hash": payload.get("config_hash"),
        "contract_hash": payload.get("contract_hash"),
    }


def _resolve_year_roots(corpus_root: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for year in ALLOWED_YEARS:
        root = (corpus_root / f"year={year}").resolve()
        if not root.is_dir():
            raise RuntimeError(f"missing allowlisted year root: {root}")
        # Refuse any path that somehow includes sealed segments.
        if any(part.lower() in {"holdout", "_staging"} for part in root.parts):
            raise RuntimeError(f"year root intersects forbidden segment: {root}")
        roots.append(root)
    return tuple(roots)


def _git_identity() -> tuple[str, str]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    return commit, tree


def _write_run_header(
    config: DriverConfig,
    *,
    year_roots: Sequence[Path],
    started_at: datetime,
) -> None:
    config.run_log.parent.mkdir(parents=True, exist_ok=True)
    body = f"""# US-U1 exploration run (U1-B)

```
JOB = us-u1-exploration-run-20260805-2332
PURPOSE = EXPLORATORY_FALSIFICATION_ONLY
PROMOTION = FORBIDDEN
ENGINE_COMMIT = {config.engine_commit}
ENGINE_TREE = {config.engine_tree}
DRIVER = research/us_stage_b/u1_exploration_driver.py
DRIVER_BLOB_SHA256 = {driver_blob_sha256()}
ADAPTER_PACKAGE_BLOB_SHA256 = {adapter_blob_sha256()}
CANDIDATES_YAML = {config.candidates_yaml.resolve()}
CANDIDATES_YAML_SHA256 = {FROZEN_CANDIDATES_SHA256}
CORPUS_ROOT = {config.corpus_root.resolve()}
EXPLORATION_WINDOW = {EXPLORATION_START.isoformat()}..{EXPLORATION_END.isoformat()}
YEAR_ROOTS = {len(year_roots)}
PYTHON = {sys.version.replace(chr(10), " ")}
PLATFORM = {platform.platform()}
STARTED_AT = {started_at.isoformat()}
TRIALS = 3 candidates × 1 each (no sweep / no retry-as-new-run)
ORDERS=0 / ACCOUNT_CONTACT=0 / DB_WRITES=0 / SCHEDULER=0 / DEPLOY=0 / HOLDOUT_READS=0
```

## Explicit year roots

"""
    for root in year_roots:
        body += f"- `{root}`\n"
    body += "\n## Candidate progress (append-only)\n"
    config.run_log.write_text(body, encoding="utf-8")


def _append_candidate_section(run_log: Path, record: Mapping[str, Any]) -> None:
    summary = record.get("summary") or {}
    section = f"""
### {record["strategy_id"]}

```
status = {record["status"]}
contract_hash = {record["contract_hash"]}
started_at = {record["started_at"]}
finished_at = {record["finished_at"]}
wall_seconds = {record["wall_seconds"]:.3f}
output = {record["output_path"]}
artifact_sha256 = {record["artifact_sha256"]}
error = {record["error"]!r}
verdict_state = {summary.get("verdict_state")}
promotion = {summary.get("promotion")}
run_invalid = {summary.get("run_invalid")}
outcome_status_counts = {summary.get("outcome_status_counts")}
observation_summary = {summary.get("observation_summary")}
scale_estimate = {summary.get("scale_estimate")}
labels = {summary.get("labels")}
ORDERS={summary.get("ORDERS", 0)} ACCOUNT_CONTACT={summary.get("ACCOUNT_CONTACT", 0)} DB_WRITES={summary.get("DB_WRITES", 0)} SCHEDULER={summary.get("SCHEDULER", 0)} DEPLOY={summary.get("DEPLOY", 0)} HOLDOUT_READS={summary.get("HOLDOUT_READS", 0)} FORBIDDEN_ROOT_ENUMERATIONS={summary.get("FORBIDDEN_ROOT_ENUMERATIONS", 0)}
```

command:

```
{" ".join(str(part) for part in record["command"])}
```
"""
    _append_run_log(run_log, section)


def _append_run_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def _build_manifest(
    *,
    config: DriverConfig,
    year_roots: Sequence[Path],
    started_at: datetime,
    finished_at: datetime,
    candidate_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "job": "us-u1-exploration-run-20260805-2332",
        "purpose": "EXPLORATORY_FALSIFICATION_ONLY",
        "promotion": "FORBIDDEN",
        "engine_commit": config.engine_commit,
        "engine_tree": config.engine_tree,
        "driver": "research/us_stage_b/u1_exploration_driver.py",
        "driver_blob_sha256": driver_blob_sha256(),
        "adapter_package_blob_sha256": adapter_blob_sha256(),
        "candidates_yaml": str(config.candidates_yaml.resolve()),
        "candidates_yaml_sha256": FROZEN_CANDIDATES_SHA256,
        "corpus_root": str(config.corpus_root.resolve()),
        "year_roots": [str(root) for root in year_roots],
        "exploration_window": {
            "start": EXPLORATION_START.isoformat(),
            "end": EXPLORATION_END.isoformat(),
        },
        "python": sys.version,
        "platform": platform.platform(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_seconds_total": (finished_at - started_at).total_seconds(),
        "trials": "3_candidates_x_1_each",
        "candidates": list(candidate_records),
        "invariants": {
            "ORDERS": 0,
            "ACCOUNT_CONTACT": 0,
            "DB_WRITES": 0,
            "SCHEDULER": 0,
            "DEPLOY": 0,
            "HOLDOUT_READS": 0,
        },
    }


def _write_checksums(output_dir: Path, *, filenames: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for name in filenames:
        path = output_dir / name
        if not path.is_file():
            lines.append(f"MISSING  {name}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    return lines


def _footer(
    *,
    finished_at: datetime,
    manifest: Mapping[str, Any],
    checksums_path: Path,
) -> str:
    statuses = [
        f"{item['strategy_id']}={item['status']}" for item in manifest["candidates"]
    ]
    return f"""
## Run complete

```
FINISHED_AT = {finished_at.isoformat()}
WALL_SECONDS_TOTAL = {manifest["wall_seconds_total"]:.3f}
CANDIDATE_STATUSES = {statuses}
MANIFEST = {manifest.get("engine_commit")}
CHECKSUMS = {checksums_path}
ORDERS=0 / ACCOUNT_CONTACT=0 / DB_WRITES=0 / SCHEDULER=0 / DEPLOY=0 / HOLDOUT_READS=0
```
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
