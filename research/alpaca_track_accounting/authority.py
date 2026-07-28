"""ROB-1064 H6 — exact H2/H4 authority binding and current honest seal.

The committed current report is intentionally ``structural_incomplete``:
the repository contains H2 identities and H4's deterministic input/run
authority, but no materialized terminal H4 status artifact that H6 may
truthfully treat as observed.  H6 therefore accounts for all 16 configs and
all 128 cells without inventing zeros or reading current market tape, and it
blocks H5 with ``performance_usable=false``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import accounting as acct
import configs as h2_configs
import fold_schedule as h4_folds
import run_manifest as h4_manifest

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "CURRENT_INCOMPLETE_REASON",
    "build_current_seal",
    "canonical_expected_configs",
    "canonical_fold_ids",
    "canonical_trial_provenance",
]

CURRENT_INCOMPLETE_REASON = (
    "H4 terminal execution evidence is not materialized in the repository; "
    "observation is null and no current tape or zero default was substituted"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
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
    "research/alpaca_track_walkforward/trade_ledger.py",
    "research/alpaca_track_walkforward/wf_seal_consumption.py",
)


def canonical_expected_configs() -> tuple[acct.ExpectedConfig, ...]:
    """Load the exact, immutable 16-row H2 config authority."""

    return tuple(
        acct.ExpectedConfig(
            strategy=config.family,
            config_id=config.config_id,
            config_hash=config.canonical_hash,
        )
        for config in h2_configs.build_all_configs()
    )


def _canonical_folds() -> tuple[h4_folds.Fold, ...]:
    manifest = h4_manifest.canonical_run_manifest()
    return h4_folds.build_fold_schedule(manifest.anchor_oos_start_ms)


def canonical_fold_ids() -> tuple[str, ...]:
    """Return the exact eight H4 fold identifiers in canonical order."""

    return tuple(f"fold-{fold.fold_index}" for fold in _canonical_folds())


def _fold_schedule_hash() -> str:
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
        for fold in _canonical_folds()
    ]
    return canonical_sha256(
        {
            "schema_version": "alpaca_track_h4_fold_schedule.v1",
            "folds": payload,
        }
    )


def _execution_code_hash() -> str:
    """Hash exact H3+H4 execution bytes; never mtimes or directory order."""

    files = []
    for logical_path in _EXECUTION_SOURCE_PATHS:
        raw = (_REPO_ROOT / logical_path).read_bytes()
        files.append(
            {
                "logical_path": logical_path,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return canonical_sha256(
        {
            "schema_version": "alpaca_track_h3_h4_execution_source_bundle.v1",
            "files": files,
        }
    )


def canonical_trial_provenance() -> acct.TrialProvenance:
    """Bind trials to exact corpus, fold schedule, code bytes, and run ID."""

    manifest = h4_manifest.canonical_run_manifest()
    return acct.TrialProvenance(
        corpus_manifest_hash=manifest.manifest_hash,
        fold_schedule_hash=_fold_schedule_hash(),
        code_hash=_execution_code_hash(),
        run_id=manifest.run_id,
    )


def build_current_seal() -> acct.AccountingSeal:
    """Build the current honest incomplete seal without executing H4/H5."""

    configs = canonical_expected_configs()
    folds = canonical_fold_ids()
    provenance = canonical_trial_provenance()
    trials = []
    for config in configs:
        cells = tuple(
            acct.FoldCell(
                strategy=config.strategy,
                config_id=config.config_id,
                fold_id=fold_id,
                status="structural_incomplete",
                observation_count=None,
                unobserved_reason=CURRENT_INCOMPLETE_REASON,
            )
            for fold_id in folds
        )
        trials.append(
            acct.TrialRecord(
                strategy=config.strategy,
                config_id=config.config_id,
                config_hash=config.config_hash,
                provenance=provenance,
                primary=True,
                retry_count=0,
                status_events=(
                    acct.StatusEvent(
                        sequence=0,
                        status="registered",
                        reason=None,
                    ),
                    acct.StatusEvent(
                        sequence=1,
                        status="structural_incomplete",
                        reason=CURRENT_INCOMPLETE_REASON,
                    ),
                ),
                cells=cells,
            )
        )
    return acct.seal_trial_accounting(
        tuple(trials),
        expected_configs=configs,
        expected_fold_ids=folds,
    )
