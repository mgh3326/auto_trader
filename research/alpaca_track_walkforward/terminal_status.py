"""Materialize H4 terminal execution status without evaluating performance.

ROB-1062 originally returned in-memory ``FamilyFoldResult`` objects only.
There was no repository artifact schema, writer, or canonical output path for
H6 to consume.  This module supplies that missing boundary while remaining
strictly count/status-only:

* the canonical synthetic corpus is executed through the signal/fill ledger;
* TRAIN and OOS ``BlindCounts`` are derived from the continuous lifecycle;
* no performance-view helper and no mask reveal function is called;
* all 16 configs x 8 folds are written as one hash-bound artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import blind_counts as bc
import fold_schedule as fs
import run_manifest as rm
import runner
import seal_consumption as h3_seal
import synthetic_corpus as corpus

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "CANONICAL_TERMINAL_ARTIFACT_PATH",
    "TerminalArtifactError",
    "TerminalCell",
    "TerminalExecutionArtifact",
    "build_terminal_execution_artifact",
    "canonical_execution_code_hash",
    "canonical_fold_schedule_hash",
    "load_terminal_execution_artifact",
    "materialize_terminal_execution_artifact",
]

SCHEMA_VERSION = "alpaca_track_h4_terminal_execution_status.v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_TERMINAL_ARTIFACT_PATH = (
    Path(__file__).resolve().parent
    / "terminal_artifacts"
    / "rob1062-h4-synthetic-ac27-v1.json"
)

# These are the exact H3+H4 bytes that produce the terminal count/status
# evidence.  Test modules and the H6 consumer are intentionally excluded.
_EXECUTION_SOURCE_PATHS = (
    "research/alpaca_track_signals/dats_engine.py",
    "research/alpaca_track_signals/dats_state.py",
    "research/alpaca_track_signals/decision_calendar.py",
    "research/alpaca_track_signals/indicators.py",
    "research/alpaca_track_signals/output_schema.py",
    "research/alpaca_track_signals/reason_codes.py",
    "research/alpaca_track_signals/seal_consumption.py",
    "research/alpaca_track_signals/sizing.py",
    "research/alpaca_track_signals/wcmb_engine.py",
    "research/alpaca_track_signals/wcmb_ranking.py",
    "research/alpaca_track_walkforward/blind_counts.py",
    "research/alpaca_track_walkforward/config_selection.py",
    "research/alpaca_track_walkforward/context_binding.py",
    "research/alpaca_track_walkforward/fill_model.py",
    "research/alpaca_track_walkforward/fold_schedule.py",
    "research/alpaca_track_walkforward/oos_mask.py",
    "research/alpaca_track_walkforward/pnl_views.py",
    "research/alpaca_track_walkforward/provider_evidence.py",
    "research/alpaca_track_walkforward/run_manifest.py",
    "research/alpaca_track_walkforward/runner.py",
    "research/alpaca_track_walkforward/synthetic_corpus.py",
    "research/alpaca_track_walkforward/terminal_status.py",
    "research/alpaca_track_walkforward/trade_ledger.py",
    "research/alpaca_track_walkforward/wf_seal_consumption.py",
)


class TerminalArtifactError(ValueError):
    """The H4 terminal artifact is malformed, drifted, or ambiguous."""


def _require_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise TerminalArtifactError(f"{name} must be a non-empty built-in str")
    return value


def _require_hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise TerminalArtifactError(f"{name} must be lowercase SHA-256 hex")
    return value


def _require_count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TerminalArtifactError(f"{name} must be a non-negative built-in int")
    return value


def _blind_counts_payload(counts: bc.BlindCounts) -> dict[str, object]:
    if type(counts) is not bc.BlindCounts:
        raise TypeError("counts must be an exact BlindCounts instance")
    return {
        "total_decision_records": counts.total_decision_records,
        "modeled_entries_count": counts.modeled_entries_count,
        "closed_trades_count": counts.closed_trades_count,
        "open_positions_count": counts.open_positions_count,
        "entry_unfilled_count": counts.entry_unfilled_count,
        "exit_unfilled_count": counts.exit_unfilled_count,
        "fill_window_incomplete_count": counts.fill_window_incomplete_count,
        "holding_days": list(counts.holding_days),
        "reason_code_histogram": dict(counts.reason_code_histogram),
    }


def _blind_counts_from_payload(value: object, name: str) -> bc.BlindCounts:
    if type(value) is not dict:
        raise TerminalArtifactError(f"{name} must be an object")
    expected = {
        "total_decision_records",
        "modeled_entries_count",
        "closed_trades_count",
        "open_positions_count",
        "entry_unfilled_count",
        "exit_unfilled_count",
        "fill_window_incomplete_count",
        "holding_days",
        "reason_code_histogram",
    }
    if set(value) != expected:
        raise TerminalArtifactError(f"{name} fields do not match the v1 schema")
    holding_days = value["holding_days"]
    histogram = value["reason_code_histogram"]
    if type(holding_days) is not list:
        raise TerminalArtifactError(f"{name}.holding_days must be a list")
    if type(histogram) is not dict:
        raise TerminalArtifactError(f"{name}.reason_code_histogram must be an object")
    return bc.BlindCounts(
        total_decision_records=_require_count(
            value["total_decision_records"],
            f"{name}.total_decision_records",
        ),
        modeled_entries_count=_require_count(
            value["modeled_entries_count"],
            f"{name}.modeled_entries_count",
        ),
        closed_trades_count=_require_count(
            value["closed_trades_count"],
            f"{name}.closed_trades_count",
        ),
        open_positions_count=_require_count(
            value["open_positions_count"],
            f"{name}.open_positions_count",
        ),
        entry_unfilled_count=_require_count(
            value["entry_unfilled_count"],
            f"{name}.entry_unfilled_count",
        ),
        exit_unfilled_count=_require_count(
            value["exit_unfilled_count"],
            f"{name}.exit_unfilled_count",
        ),
        fill_window_incomplete_count=_require_count(
            value["fill_window_incomplete_count"],
            f"{name}.fill_window_incomplete_count",
        ),
        holding_days=tuple(
            _require_count(item, f"{name}.holding_days item") for item in holding_days
        ),
        reason_code_histogram={
            _require_string(
                reason, f"{name}.reason_code_histogram key"
            ): _require_count(count, f"{name}.reason_code_histogram[{reason!r}]")
            for reason, count in histogram.items()
        },
    )


def _incomplete_reason(
    train_counts: bc.BlindCounts,
    oos_counts: bc.BlindCounts,
) -> str | None:
    phases = []
    if train_counts.is_incomplete:
        phases.append("TRAIN")
    if oos_counts.is_incomplete:
        phases.append("OOS")
    if not phases:
        return None
    return "blind-count structural incompleteness in " + "+".join(phases)


@dataclass(frozen=True, slots=True)
class TerminalCell:
    family: str
    config_id: str
    config_hash: str
    fold_id: str
    status: str
    reason: str | None
    train_blind_counts: bc.BlindCounts
    oos_blind_counts: bc.BlindCounts

    def __post_init__(self) -> None:
        _require_string(self.family, "family")
        _require_string(self.config_id, "config_id")
        _require_hex64(self.config_hash, "config_hash")
        _require_string(self.fold_id, "fold_id")
        if self.status not in ("executed", "structural_incomplete"):
            raise TerminalArtifactError(
                "cell status must be executed or structural_incomplete"
            )
        if type(self.train_blind_counts) is not bc.BlindCounts:
            raise TypeError("train_blind_counts must be an exact BlindCounts instance")
        if type(self.oos_blind_counts) is not bc.BlindCounts:
            raise TypeError("oos_blind_counts must be an exact BlindCounts instance")

        expected_reason = _incomplete_reason(
            self.train_blind_counts,
            self.oos_blind_counts,
        )
        expected_status = (
            "structural_incomplete" if expected_reason is not None else "executed"
        )
        if self.status != expected_status or self.reason != expected_reason:
            raise TerminalArtifactError(
                "cell status/reason does not match its blind-count completeness"
            )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.family, self.config_id, self.fold_id)

    @property
    def observation_count(self) -> int:
        """Number of TRAIN+OOS decision records structurally observed."""

        return (
            self.train_blind_counts.total_decision_records
            + self.oos_blind_counts.total_decision_records
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "family": self.family,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            "fold_id": self.fold_id,
            "status": self.status,
            "reason": self.reason,
            "observation_count": self.observation_count,
            "train_blind_counts": _blind_counts_payload(self.train_blind_counts),
            "oos_blind_counts": _blind_counts_payload(self.oos_blind_counts),
        }


@dataclass(frozen=True, slots=True)
class TerminalExecutionArtifact:
    run_id: str
    corpus_manifest_hash: str
    fold_schedule_hash: str
    code_hash: str
    cells: tuple[TerminalCell, ...]
    artifact_hash: str

    def __post_init__(self) -> None:
        _require_string(self.run_id, "run_id")
        _require_hex64(self.corpus_manifest_hash, "corpus_manifest_hash")
        _require_hex64(self.fold_schedule_hash, "fold_schedule_hash")
        _require_hex64(self.code_hash, "code_hash")
        _require_hex64(self.artifact_hash, "artifact_hash")
        if type(self.cells) is not tuple or any(
            type(cell) is not TerminalCell for cell in self.cells
        ):
            raise TypeError("cells must be a tuple of exact TerminalCell values")
        if len(self.cells) != 128:
            raise TerminalArtifactError(
                "terminal artifact must contain exactly 128 cells"
            )
        if len({cell.key for cell in self.cells}) != 128:
            raise TerminalArtifactError("terminal artifact contains duplicate cells")
        config_keys = {(cell.family, cell.config_id) for cell in self.cells}
        if len(config_keys) != 16:
            raise TerminalArtifactError(
                "terminal artifact must contain exactly 16 configs"
            )
        if {cell.fold_id for cell in self.cells} != {
            f"fold-{index}" for index in range(8)
        }:
            raise TerminalArtifactError("terminal artifact must cover fold-0..fold-7")
        object.__setattr__(
            self, "cells", tuple(sorted(self.cells, key=lambda c: c.key))
        )

    def _semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "corpus_manifest_hash": self.corpus_manifest_hash,
            "fold_schedule_hash": self.fold_schedule_hash,
            "code_hash": self.code_hash,
            "cells": [cell.to_payload() for cell in self.cells],
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._semantic_payload(),
            "artifact_hash": self.artifact_hash,
        }

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        corpus_manifest_hash: str,
        fold_schedule_hash: str,
        code_hash: str,
        cells: tuple[TerminalCell, ...],
    ) -> TerminalExecutionArtifact:
        provisional = cls(
            run_id=run_id,
            corpus_manifest_hash=corpus_manifest_hash,
            fold_schedule_hash=fold_schedule_hash,
            code_hash=code_hash,
            cells=cells,
            artifact_hash="0" * 64,
        )
        return cls(
            run_id=run_id,
            corpus_manifest_hash=corpus_manifest_hash,
            fold_schedule_hash=fold_schedule_hash,
            code_hash=code_hash,
            cells=provisional.cells,
            artifact_hash=canonical_sha256(provisional._semantic_payload()),
        )


def canonical_fold_schedule_hash() -> str:
    manifest = rm.canonical_run_manifest()
    folds = fs.build_fold_schedule(manifest.anchor_oos_start_ms)
    payload = [
        {
            "fold_id": f"fold-{fold.fold_index}",
            "fold_index": fold.fold_index,
            "train_start_ms": fold.train_start_ms,
            "train_end_ms": fold.train_end_ms,
            "embargo_start_ms": fold.embargo_start_ms,
            "embargo_end_ms": fold.embargo_end_ms,
            "oos_start_ms": fold.oos_start_ms,
            "oos_end_ms": fold.oos_end_ms,
        }
        for fold in folds
    ]
    return canonical_sha256(
        {
            "schema_version": "alpaca_track_h4_fold_schedule.v1",
            "folds": payload,
        }
    )


def canonical_execution_code_hash() -> str:
    files = []
    for logical_path in _EXECUTION_SOURCE_PATHS:
        files.append(
            {
                "logical_path": logical_path,
                "raw_sha256": hashlib.sha256(
                    (_REPO_ROOT / logical_path).read_bytes()
                ).hexdigest(),
            }
        )
    return canonical_sha256(
        {
            "schema_version": "alpaca_track_h3_h4_execution_source_bundle.v1",
            "files": files,
        }
    )


def _blind_counts_for_phase(
    *,
    phase: str,
    run_result: runner._ContinuousRunResult,
    fold: fs.Fold,
) -> bc.BlindCounts:
    if phase == "TRAIN":
        phase_start_ms, phase_end_ms = fold.train_start_ms, fold.train_end_ms
    elif phase == "OOS":
        phase_start_ms, phase_end_ms = fold.oos_start_ms, fold.oos_end_ms
    else:
        raise ValueError(f"unknown phase {phase!r}")

    def in_phase(timestamp_ms: int) -> bool:
        return phase_start_ms <= timestamp_ms < phase_end_ms

    records = tuple(
        record for record in run_result.all_records if in_phase(record.decision_ts_ms)
    )
    modeled_entries = tuple(
        entry
        for entry in run_result.modeled_entry_evidence
        if in_phase(entry.entry_fill_ts_ms)
    )
    closed_trades = tuple(
        trade
        for trade in run_result.closed_trades
        if in_phase(trade.entry_fill_ts_ms) and in_phase(trade.exit_fill_ts_ms)
    )
    fill_attempts = tuple(
        attempt
        for attempt in run_result.fill_attempts
        if in_phase(attempt.decision_ts_ms)
    )
    return bc.compute_blind_counts(
        records,
        closed_trades=closed_trades,
        open_positions_count=len(modeled_entries) - len(closed_trades),
        fill_attempts=fill_attempts,
        modeled_entries_count=len(modeled_entries),
    )


def _build_family_fold_terminal_cells(
    task: tuple[int, str],
) -> tuple[dict[str, object], ...]:
    fold_index, family = task
    manifest = rm.canonical_run_manifest()
    folds = fs.build_fold_schedule(manifest.anchor_oos_start_ms)
    fold = folds[fold_index]
    bundle = h3_seal.load_sealed_configs_and_params()
    fold_id = f"fold-{fold.fold_index}"
    num_days = (fold.oos_end_ms - fold.train_start_ms) // corpus.DAY_MS
    bars_by_symbol = corpus.build_bars_by_symbol(
        window_start_ms=fold.train_start_ms,
        num_days=num_days,
    )
    universe_provider = corpus.make_universe_snapshot_provider()
    minute_provider = corpus.make_minute_bars_provider(
        window_start_ms=fold.train_start_ms,
    )
    provider_snapshot = runner._materialize_provider_snapshot(
        family=family,
        fold=fold,
        fold_id=fold_id,
        bars_by_symbol=bars_by_symbol,
        universe_snapshot_provider=universe_provider,
        minute_bars_provider=minute_provider,
    )
    cells = []
    for config in bundle.configs:
        if config.family != family:
            continue
        run_result = runner._run_continuous_decisions(
            config=config,
            family=family,
            fold=fold,
            bars_by_symbol=bars_by_symbol,
            universe_snapshot_provider=universe_provider,
            minute_bars_provider=minute_provider,
            provider_snapshot=provider_snapshot,
        )
        train_counts = _blind_counts_for_phase(
            phase="TRAIN",
            run_result=run_result,
            fold=fold,
        )
        oos_counts = _blind_counts_for_phase(
            phase="OOS",
            run_result=run_result,
            fold=fold,
        )
        reason = _incomplete_reason(train_counts, oos_counts)
        cells.append(
            TerminalCell(
                family=family,
                config_id=config.config_id,
                config_hash=config.canonical_hash,
                fold_id=fold_id,
                status=("structural_incomplete" if reason is not None else "executed"),
                reason=reason,
                train_blind_counts=train_counts,
                oos_blind_counts=oos_counts,
            )
        )
    return tuple(cell.to_payload() for cell in cells)


def build_terminal_execution_artifact(
    *,
    max_workers: int | None = None,
) -> TerminalExecutionArtifact:
    """Execute the canonical H4 identity and return only count/status evidence."""

    manifest = rm.canonical_run_manifest()
    tasks = tuple(
        (fold_index, family)
        for fold_index in range(fs.OOS_FOLDS)
        for family in ("AP-A1", "AP-A2")
    )
    if max_workers is None:
        max_workers = min(8, os.process_cpu_count() or 1)
    if type(max_workers) is not int or max_workers <= 0:
        raise ValueError("max_workers must be a positive built-in int")
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        task_results = tuple(executor.map(_build_family_fold_terminal_cells, tasks))
    cell_payloads = tuple(
        cell_payload for task_cells in task_results for cell_payload in task_cells
    )
    cells = tuple(
        _cell_from_payload(cell_payload, index)
        for index, cell_payload in enumerate(cell_payloads)
    )

    return TerminalExecutionArtifact.create(
        run_id=manifest.run_id,
        corpus_manifest_hash=manifest.manifest_hash,
        fold_schedule_hash=canonical_fold_schedule_hash(),
        code_hash=canonical_execution_code_hash(),
        cells=cells,
    )


def _cell_from_payload(value: object, index: int) -> TerminalCell:
    if type(value) is not dict:
        raise TerminalArtifactError(f"cells[{index}] must be an object")
    expected = {
        "family",
        "config_id",
        "config_hash",
        "fold_id",
        "status",
        "reason",
        "observation_count",
        "train_blind_counts",
        "oos_blind_counts",
    }
    if set(value) != expected:
        raise TerminalArtifactError(f"cells[{index}] fields do not match v1 schema")
    train_counts = _blind_counts_from_payload(
        value["train_blind_counts"],
        f"cells[{index}].train_blind_counts",
    )
    oos_counts = _blind_counts_from_payload(
        value["oos_blind_counts"],
        f"cells[{index}].oos_blind_counts",
    )
    reason = value["reason"]
    if reason is not None:
        reason = _require_string(reason, f"cells[{index}].reason")
    cell = TerminalCell(
        family=_require_string(value["family"], f"cells[{index}].family"),
        config_id=_require_string(value["config_id"], f"cells[{index}].config_id"),
        config_hash=_require_hex64(
            value["config_hash"],
            f"cells[{index}].config_hash",
        ),
        fold_id=_require_string(value["fold_id"], f"cells[{index}].fold_id"),
        status=_require_string(value["status"], f"cells[{index}].status"),
        reason=reason,
        train_blind_counts=train_counts,
        oos_blind_counts=oos_counts,
    )
    if (
        _require_count(
            value["observation_count"],
            f"cells[{index}].observation_count",
        )
        != cell.observation_count
    ):
        raise TerminalArtifactError(
            f"cells[{index}].observation_count does not match blind counts"
        )
    return cell


def load_terminal_execution_artifact(path: Path) -> TerminalExecutionArtifact:
    """Load and authenticate one exact terminal artifact."""

    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalArtifactError(
            "terminal artifact is not valid UTF-8 JSON"
        ) from exc
    if type(payload) is not dict:
        raise TerminalArtifactError("terminal artifact root must be an object")
    expected = {
        "schema_version",
        "run_id",
        "corpus_manifest_hash",
        "fold_schedule_hash",
        "code_hash",
        "cells",
        "artifact_hash",
    }
    if set(payload) != expected:
        raise TerminalArtifactError("terminal artifact fields do not match v1 schema")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise TerminalArtifactError("unsupported terminal artifact schema")
    artifact_hash = _require_hex64(payload["artifact_hash"], "artifact_hash")
    semantic_payload = {
        key: value for key, value in payload.items() if key != "artifact_hash"
    }
    if canonical_sha256(semantic_payload) != artifact_hash:
        raise TerminalArtifactError("terminal artifact hash verification failed")
    raw_cells = payload["cells"]
    if type(raw_cells) is not list:
        raise TerminalArtifactError("cells must be a list")
    artifact = TerminalExecutionArtifact(
        run_id=_require_string(payload["run_id"], "run_id"),
        corpus_manifest_hash=_require_hex64(
            payload["corpus_manifest_hash"],
            "corpus_manifest_hash",
        ),
        fold_schedule_hash=_require_hex64(
            payload["fold_schedule_hash"],
            "fold_schedule_hash",
        ),
        code_hash=_require_hex64(payload["code_hash"], "code_hash"),
        cells=tuple(
            _cell_from_payload(value, index) for index, value in enumerate(raw_cells)
        ),
        artifact_hash=artifact_hash,
    )
    manifest = rm.canonical_run_manifest()
    if (
        artifact.run_id != manifest.run_id
        or artifact.corpus_manifest_hash != manifest.manifest_hash
        or artifact.fold_schedule_hash != canonical_fold_schedule_hash()
        or artifact.code_hash != canonical_execution_code_hash()
    ):
        raise TerminalArtifactError(
            "terminal artifact provenance does not match current canonical execution"
        )
    if artifact.to_bytes() != raw:
        raise TerminalArtifactError("terminal artifact is not canonical byte encoding")
    return artifact


def materialize_terminal_execution_artifact(
    path: Path = CANONICAL_TERMINAL_ARTIFACT_PATH,
) -> TerminalExecutionArtifact:
    """Create a new artifact with exclusive-create semantics; never overwrite."""

    if path.exists():
        raise FileExistsError(f"refusing to overwrite terminal artifact: {path}")
    artifact = build_terminal_execution_artifact()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(artifact.to_bytes())
    return artifact


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize count/status-only ROB-1062 H4 terminal evidence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CANONICAL_TERMINAL_ARTIFACT_PATH,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    artifact = materialize_terminal_execution_artifact(args.output)
    status_counts: Mapping[str, int] = MappingProxyType(
        {
            status: sum(1 for cell in artifact.cells if cell.status == status)
            for status in ("executed", "structural_incomplete")
        }
    )
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "artifact_hash": artifact.artifact_hash,
                "cells": len(artifact.cells),
                "status_counts": dict(status_counts),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
