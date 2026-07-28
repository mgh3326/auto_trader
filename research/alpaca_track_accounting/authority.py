"""ROB-1064 H6 — exact H2/H4 authority binding and append-only seals.

``rob-1064-current.json`` remains the immutable historical record from before
H4 terminal evidence existed.  A distinct new seal consumes H4's materialized,
count/status-only terminal artifact.  Neither path reads current market tape
or substitutes zero observations.
"""

from __future__ import annotations

from pathlib import Path

import accounting as acct
import configs as h2_configs
import fold_schedule as h4_folds
import run_manifest as h4_manifest
import terminal_status as h4_terminal

__all__ = [
    "CURRENT_INCOMPLETE_REASON",
    "MATERIALIZED_SEAL_PATH",
    "build_current_seal",
    "build_materialized_seal",
    "canonical_expected_configs",
    "canonical_fold_ids",
    "canonical_trial_provenance",
    "materialize_materialized_seal",
]

CURRENT_INCOMPLETE_REASON = (
    "H4 terminal execution evidence is not materialized in the repository; "
    "observation is null and no current tape or zero default was substituted"
)

MATERIALIZED_SEAL_PATH = (
    Path(__file__).resolve().parent
    / "sealed_reports"
    / "rob-1064-run-2026-07-29-h4-terminal-v1.json"
)
_LEGACY_INCOMPLETE_PROVENANCE = acct.TrialProvenance(
    corpus_manifest_hash=(
        "4ad4ea2ddbfc795f6ecb0188be5801ed1a8db06ca4c9933f0fe884e6eac68113"
    ),
    fold_schedule_hash=(
        "f3012c40c655190eb63da4278103b231c4d339689bf92876e588dfb60a937210"
    ),
    code_hash=("bcf44a27c96988d2b3301ec66a5221d7eb7bcb3746746105198af75de8856e2d"),
    run_id="rob1062-h4-synthetic-ac27-v1",
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


def canonical_trial_provenance() -> acct.TrialProvenance:
    """Bind trials to exact corpus, fold schedule, code bytes, and run ID."""

    manifest = h4_manifest.canonical_run_manifest()
    return acct.TrialProvenance(
        corpus_manifest_hash=manifest.manifest_hash,
        fold_schedule_hash=h4_terminal.canonical_fold_schedule_hash(),
        code_hash=h4_terminal.canonical_execution_code_hash(),
        run_id=manifest.run_id,
    )


def build_current_seal() -> acct.AccountingSeal:
    """Rebuild the immutable pre-materialization seal as historical evidence."""

    configs = canonical_expected_configs()
    folds = canonical_fold_ids()
    provenance = _LEGACY_INCOMPLETE_PROVENANCE
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


def build_materialized_seal() -> acct.AccountingSeal:
    """Build a new H6 seal from the authenticated H4 terminal artifact."""

    artifact = h4_terminal.load_terminal_execution_artifact(
        h4_terminal.CANONICAL_TERMINAL_ARTIFACT_PATH
    )
    configs = canonical_expected_configs()
    folds = canonical_fold_ids()
    provenance = canonical_trial_provenance()
    if (
        artifact.corpus_manifest_hash != provenance.corpus_manifest_hash
        or artifact.fold_schedule_hash != provenance.fold_schedule_hash
        or artifact.code_hash != provenance.code_hash
        or artifact.run_id != provenance.run_id
    ):
        raise acct.AuthorityError(
            "H4 terminal artifact provenance does not match H6 authority"
        )

    cells_by_config: dict[tuple[str, str], list[h4_terminal.TerminalCell]] = {}
    for cell in artifact.cells:
        cells_by_config.setdefault((cell.family, cell.config_id), []).append(cell)

    trials = []
    for config in configs:
        terminal_cells = cells_by_config.get(config.key, [])
        h6_cells = []
        incomplete_reasons = []
        for cell in terminal_cells:
            if cell.config_hash != config.config_hash:
                raise acct.AuthorityError(
                    f"H4 config hash mismatch for {config.config_id}"
                )
            if cell.status == "structural_incomplete":
                incomplete_reasons.append(
                    f"{cell.fold_id}: {cell.reason or 'unspecified structural issue'}"
                )
                observation_count = None
                unobserved_reason = cell.reason
            else:
                observation_count = cell.observation_count
                unobserved_reason = None
            h6_cells.append(
                acct.FoldCell(
                    strategy=config.strategy,
                    config_id=config.config_id,
                    fold_id=cell.fold_id,
                    status=cell.status,
                    observation_count=observation_count,
                    unobserved_reason=unobserved_reason,
                )
            )

        events = [
            acct.StatusEvent(sequence=0, status="registered", reason=None),
            acct.StatusEvent(sequence=1, status="executed", reason=None),
        ]
        if incomplete_reasons:
            events.append(
                acct.StatusEvent(
                    sequence=2,
                    status="structural_incomplete",
                    reason="; ".join(incomplete_reasons),
                )
            )
        trials.append(
            acct.TrialRecord(
                strategy=config.strategy,
                config_id=config.config_id,
                config_hash=config.config_hash,
                provenance=provenance,
                primary=True,
                retry_count=0,
                status_events=tuple(events),
                cells=tuple(h6_cells),
            )
        )

    return acct.seal_trial_accounting(
        tuple(trials),
        expected_configs=configs,
        expected_fold_ids=folds,
    )


def materialize_materialized_seal(
    path: Path = MATERIALIZED_SEAL_PATH,
) -> acct.AccountingSeal:
    """Write one new seal with exclusive-create semantics; never overwrite."""

    if path.exists():
        raise FileExistsError(f"refusing to overwrite accounting seal: {path}")
    seal = build_materialized_seal()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(seal.to_bytes())
    return seal
