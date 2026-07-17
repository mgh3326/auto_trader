"""ROB-944 (H4, ROB-940) — acyclic full-campaign identity envelope (pure,
stdlib).

Q4 (``orch-fable-answer-rob944-20260717.md``, final): the envelope is built
ACYCLICALLY:

  1. freeze material components and actual source/code/dataset-manifest
     hashes (this module's ``build_production_*``/``load_production_*``
     helpers, all verifying against a pinned expected hash);
  2. build the 24 H6 identity specs FROM those components
     (``rob946_campaign_identity.build_campaign_experiment_specs`` — that
     module stays untouched, generic, manifest-injected);
  3. build ONE top-level envelope containing the 24 specs' full components
     (ordered) plus H4-owned provenance not already in a H6 identity
     component: the exact fold schedule, the Q2/Q3 funding PIT policy, the
     ``scenario_execution`` independent-run declaration, the data-gap
     rejection policy, and the historical-only posture disclosure;
  4. hash that envelope ONCE (``full_campaign_hash``).

The final hash is NEVER fed back into any of its own inputs or embedded in
identity-bearing source — an inbox echo (if/when written) is external
evidence only, never a second identity input.

Production strategy identifiers (Q1, final, Fable-promoted 2026-07-17):
``S1 = ROB940-S1-DONCHIAN-15M / s1-v1``, ``S2 = ROB940-S2-SHOCK-REVERSAL-5M /
s2-v1`` — human-readable, strategy-distinct; code provenance is captured by
the separate ``code`` identity component (source SHA-256), so the name does
not need to encode it.

Captain audit supplement (2026-07-17), item 3 + frozen-lineage follow-up:
``@dataclass(frozen=True)`` only blocks attribute REBINDING
(``envelope.rows = ...``) — it does nothing to the MUTABLE dicts/lists
living inside those attributes, so
``envelope.funding_pit_policy["entry_gate"]["max_expected_cost_bps"] = 999``
would otherwise succeed and change a later ``full_campaign_hash()`` call on
the very same object. ``__post_init__`` closes this by recursively
converting every nested dict/list field into an immutable structure
(``types.MappingProxyType`` + ``tuple``, see ``_freeze``) — the SAME pass
also performs a full deep, non-aliasing copy (a `_freeze`'d structure shares
no mutable node with its input), so a caller's own pre-construction dict
(e.g. the injected ``dataset_manifest``) can never leak in either. Any
attempt to mutate a nested field post-construction raises ``TypeError``
(``mappingproxy`` objects and ``tuple``s do not support item assignment).
``to_dict()`` rebuilds a plain (mutable) dict via ``_unfreeze`` on the way
OUT, so a caller mutating the RETURNED dict can never affect the sealed
internal state either.

No DB/network/app/broker/random/current-time imports — pure stdlib plus the
sibling rob940_*/rob941_*/rob944_* modules and ``rob946_campaign_identity``
(all already pure themselves), deterministic given its input.
"""

from __future__ import annotations

import copy
import hashlib
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rob941_frozen_scope as frozen
import rob944_folds as foldmod
import rob946_campaign_identity as identity
from rob940_signal_manifest import (
    FROZEN_S1_CONFIGS,
    FROZEN_S2_CONFIGS,
    FrozenSignalConstants,
    S1Config,
    S2Config,
    assert_matches_frozen_s1_config,
    assert_matches_frozen_s2_config,
)
from rob940_signal_manifest import (
    signal_manifest_hash as _ACTUAL_H3_SIGNAL_MANIFEST_HASH,
)
from rob940_signal_s2 import SPEC_DEVIATIONS as _S2_SPEC_DEVIATIONS
from rob941_manifest import CorpusManifest
from rob944_gap_funding import (
    FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS,
    REASON_DATA_GAP_IN_POSITION,
    REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
    REASON_FUNDING_EVIDENCE_UNAVAILABLE,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
    MIN_ELIGIBLE_SYMBOLS,
    MIN_SYMBOL_TRAIN_TRADES,
)
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_GLOBAL_CORPUS_LOAD_FAILED,
    REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    REASON_NEVER_SELECTED_IN_ANY_FOLD,
)

SCHEMA_VERSION = "rob944_full_campaign.v1"

# Q1 (final, Fable-promoted 2026-07-17): frozen production strategy identifiers.
PRODUCTION_S1_STRATEGY_KEY = "ROB940-S1-DONCHIAN-15M"
PRODUCTION_S1_STRATEGY_VERSION = "s1-v1"
PRODUCTION_S2_STRATEGY_KEY = "ROB940-S2-SHOCK-REVERSAL-5M"
PRODUCTION_S2_STRATEGY_VERSION = "s2-v1"

# The committed corpus manifest fixture -- verified byte-for-byte against
# this pinned content_hash before use (ROB-941 discipline, re-applied here).
_HERE = Path(__file__).resolve().parent
H1_MANIFEST_PATH = _HERE / "data_manifests" / "rob941_corpus_manifest.v1.json"
H1_MANIFEST_EXPECTED_CONTENT_HASH = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)

_S1_SOURCE_PATH = _HERE / "rob940_signal_s1.py"
_S2_SOURCE_PATH = _HERE / "rob940_signal_s2.py"

H3_MANIFEST_EXPECTED_HASH = (
    "199816d45e79ed52218848dc53c54464754c5befce38dbad6615cf123b628fba"
)

# ROB-942 R1 correction (rob940_cost_model/rob940_engine docstrings): each
# cost scenario is simulated via its OWN independent
# rob940_engine.run_symbol_stream invocation/ledger -- never a net-only
# revaluation of one shared path.
SCENARIO_EXECUTION_SEMANTICS = "independent_run_with_fresh_state"


class FrozenCampaignError(ValueError):
    """The envelope's own frozen-pin verification failed (stale/tampered
    caller-asserted hash) -- distinct from ``rob946_campaign_identity``'s own
    errors, which this module lets propagate unchanged for its own concerns
    (dataset-manifest hash, campaign row shape, strategy-source mismatch)."""


class RowOrderError(FrozenCampaignError):
    """``config_rows`` did not arrive in the exact frozen H3 canonical order.

    Captain contract correction (2026-07-17): ``rob946_campaign_identity``'s
    own ``validate_campaign_rows`` deliberately only checks
    count/uniqueness/slug-membership (SORTED comparison) -- it is a generic,
    order-agnostic mechanism by design and must not be mutated here without a
    fresh consult. Exact row ORDER (aligned 1:1 with the frozen H3 24-row
    manifest: S1-00..S1-11 then S2-00..S2-11, literal input order, not
    sorted) is an H4-owned contract enforced here, before any H6 spec is
    built -- a reorder/13th-row/missing/duplicate/tampered config_id must
    fail closed rather than silently produce a differently-ordered (but
    otherwise "valid") envelope.
    """


CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(f"S1-{i:02d}" for i in range(12)) + tuple(
    f"S2-{i:02d}" for i in range(12)
)


class RowContentTamperError(FrozenCampaignError):
    """A ``CampaignConfigRow``'s ``params``/``hypothesis`` do not exactly
    match the frozen H3 manifest row for the same ``config_id``."""


def _assert_row_matches_frozen_h3_manifest(row: identity.CampaignConfigRow) -> None:
    """Captain exact-membership addendum (2026-07-17): row ORDER alone is
    not enough -- a same-domain param swap or an altered hypothesis string
    riding under an otherwise-correct/correctly-ordered ``config_id`` (e.g.
    ``S1-00``) must also fail closed. Reconstructs the native H3
    ``S1Config``/``S2Config`` from this row's ``params``/``config_id``/
    ``hypothesis`` and reuses H3's OWN membership authority
    (``assert_matches_frozen_s1_config``/``assert_matches_frozen_s2_config``,
    ROB-943 R1 remediation) for exact dataclass-``==`` verification against
    the frozen manifest row -- no new/duplicate tamper-detection logic here.
    This is production-content verification, scoped to
    ``build_production_campaign_config_rows()``'s own output; it is
    deliberately NOT applied inside the generic ``build_frozen_campaign_envelope``
    (which legitimately accepts caller-injected non-production rows for
    isolated hash-sensitivity tests -- test-only fake-row convenience must
    not weaken this production contract, but it also must not be broken by
    it).
    """
    slug = row.strategy_slug()
    try:
        if slug == "S1":
            assert_matches_frozen_s1_config(
                S1Config(
                    L=row.params["L"],
                    q_min=row.params["q_min"],
                    k_SL=row.params["k_SL"],
                    R_TP=row.params["R_TP"],
                    config_id=row.config_id,
                    hypothesis=row.hypothesis,
                )
            )
        else:
            assert_matches_frozen_s2_config(
                S2Config(
                    z_min=row.params["z_min"],
                    v_min=row.params["v_min"],
                    ER_max=row.params["ER_max"],
                    R_min=row.params["R_min"],
                    config_id=row.config_id,
                    hypothesis=row.hypothesis,
                )
            )
    except (KeyError, ValueError) as exc:
        raise RowContentTamperError(
            f"row {row.config_id!r} does not exactly match the frozen H3 manifest "
            "(param swap, altered hypothesis, or malformed params rejected fail-closed)"
        ) from exc


def _assert_canonical_row_order(config_rows: list[identity.CampaignConfigRow]) -> None:
    actual = tuple(row.config_id for row in config_rows)
    if actual != CANONICAL_ROW_ORDER:
        raise RowOrderError(
            "config_rows must arrive in the exact frozen H3 canonical order "
            f"{CANONICAL_ROW_ORDER!r}, got {actual!r} -- reorder, missing, "
            "duplicate, extra, and tampered config_id are all rejected here"
        )


def build_funding_pit_policy_component() -> dict:
    """Q2/Q3 (orch-fable-answer-rob944-20260717.md, final): the frozen
    funding PIT gate/PnL contract, as an explicit identity component."""
    return {
        "position_window": "half_open_entry_inclusive_exit_exclusive",  # Q2=A
        "entry_gate": {
            "rule": ("last_known_rate_and_interval_project_first_crossing_after_entry"),
            "max_expected_cost_bps": FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS,
            "reject_only_if_signed_cost_strictly_greater_than_max": True,
            "exactly_at_max_remains_eligible": True,
            "no_lookahead": True,
            "reason_codes": {
                "evidence_unavailable": REASON_FUNDING_EVIDENCE_UNAVAILABLE,
                "expected_cost_above_max": REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
            },
        },
        "pnl": "realized_crossings_only_no_future_rate",
    }


def build_data_gap_policy_component() -> dict:
    return {
        "reject_reason": REASON_DATA_GAP_IN_POSITION,
        "predicate": "rob941_gaps.position_touches_gap",
    }


def build_posture_component() -> dict:
    return {
        "historical_screen_only": True,
        "no_broker_execution": True,
        "no_runtime_gate_activation": True,
        "spread_age_lot_gates": "absent_from_historical_deferred_to_demo",
        "optimistic_bias_disclosure": (
            "four symbols simulated as independent single-position streams; "
            "does not model account-global one-position arbitration/skip "
            "(demo-stage arbitration design is out of this historical scope)"
        ),
    }


# Captain audit supplement (2026-07-17): build_campaign_experiment_specs' own
# per-row "code" component only hashes S1/S2 STRATEGY signal source -- it
# never touches the shared H2 execution engine or H4's own runner/CLI/
# controller files. These logical names are STABLE (bare filenames, never an
# absolute path) so the resulting hash is identical across checkout
# locations; each entry's byte-hash is (re)computed at plan/build time
# directly from the current file, so an edit to any of them changes the
# full-campaign hash. Extend this tuple as new H4 execution-affecting files
# are added (H6 preflight gate, CLI) -- do not freeze/echo the campaign
# until this list is final.
_APP_SERVICES_DIR = _HERE.parent.parent / "app" / "services"

EXECUTION_CODE_LOGICAL_FILES: tuple[tuple[str, Path], ...] = (
    ("rob940_bars_agg.py", _HERE / "rob940_bars_agg.py"),
    ("rob940_engine.py", _HERE / "rob940_engine.py"),
    ("rob940_cost_model.py", _HERE / "rob940_cost_model.py"),
    ("rob944_folds.py", _HERE / "rob944_folds.py"),
    ("rob944_selection.py", _HERE / "rob944_selection.py"),
    ("rob944_signal_ordering.py", _HERE / "rob944_signal_ordering.py"),
    ("rob944_gap_funding.py", _HERE / "rob944_gap_funding.py"),
    ("rob944_scenario_evidence.py", _HERE / "rob944_scenario_evidence.py"),
    ("rob944_walkforward.py", _HERE / "rob944_walkforward.py"),
    ("rob944_frozen_campaign.py", _HERE / "rob944_frozen_campaign.py"),
    ("run_rob944_campaign.py", _HERE / "run_rob944_campaign.py"),
    (
        "app/services/rob944_campaign_controller.py",
        _APP_SERVICES_DIR / "rob944_campaign_controller.py",
    ),
    # Captain freeze-provenance blocker (2026-07-17): actually invoked by
    # --run and material to results, but previously omitted -- their byte
    # changes would NOT have altered full_campaign_hash. rob941_funding_sidecar.py
    # and rob941_gaps.py are the delegated authorities for Fable Q2/PIT
    # (position window) and gap-in-position semantics respectively;
    # rob941_manifest.py's parsing/validation is execution-affecting since
    # rob941_offline_loader.py imports and relies on it at real corpus-load
    # time (not just at freeze/plan time).
    ("rob941_offline_loader.py", _HERE / "rob941_offline_loader.py"),
    ("rob941_funding_sidecar.py", _HERE / "rob941_funding_sidecar.py"),
    ("rob941_gaps.py", _HERE / "rob941_gaps.py"),
    ("rob941_manifest.py", _HERE / "rob941_manifest.py"),
)


def build_execution_code_provenance_component(
    files: tuple[tuple[str, Path], ...] | None = None,
) -> dict[str, str]:
    """Actual, byte-derived SHA-256 per logical execution-code filename.

    ``files`` defaults to :data:`EXECUTION_CODE_LOGICAL_FILES` (the real
    production set); tests may inject an alternate ``files`` tuple (e.g.
    pointing at a temp directory) to prove sensitivity to byte content
    without touching real repository files. It is safe for this module to
    hash its OWN file bytes (self-reference) -- the file's source text never
    contains the resulting ``full_campaign_hash`` value, so there is no
    circularity, only ordinary "this campaign's identity changed because its
    own definition changed" sensitivity.
    """
    resolved = files if files is not None else EXECUTION_CODE_LOGICAL_FILES
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in resolved
    }


def build_h3_fixed_constants_component() -> dict:
    """H3's fixed (non-tunable) constants and spec-deviation register --
    material to execution but NOT covered by ``signal_manifest_hash`` (which
    identifies only the 24-row tunable-config table)."""
    return {
        "frozen_signal_constants": dict(FrozenSignalConstants._asdict()),
        "s2_spec_deviations": list(_S2_SPEC_DEVIATIONS),
        "s2_target_direction_invalid_reason_code": "target_direction_invalid",
    }


def build_h4_reason_contract_component() -> dict:
    """H4-owned selection/funding-gate/data-gap reason codes and thresholds
    -- an execution-affecting contract that lives in H4's own modules, not
    in any H1/H2/H3/H6 identity component."""
    return {
        "selection": {
            "min_symbol_train_trades": MIN_SYMBOL_TRAIN_TRADES,
            "min_eligible_symbols": MIN_ELIGIBLE_SYMBOLS,
            "insufficient_symbol_evidence_reason": INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
            "insufficient_eligible_symbols_reason": INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        },
        "funding_gate": {
            "max_expected_cost_bps": FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS,
            "evidence_unavailable_reason": REASON_FUNDING_EVIDENCE_UNAVAILABLE,
            "expected_cost_above_max_reason": REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
        },
        "data_gap_reason": REASON_DATA_GAP_IN_POSITION,
        "insufficient_train_evidence_all_folds_reason": (
            REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS
        ),
        "attempt_statuses": ["completed", "rejected", "crashed", "timeout"],
        # Captain freeze-audit addendum (2026-07-17, item C): the frozen
        # declared scenario_statuses must exactly cover the runtime terminal
        # AggregateScenarioStatus contract (rob944_walkforward), including
        # "rejected" (a gap-touching trade forces the whole scenario trial
        # rejected) and "never_selected" (a config that never won a single
        # fold across the whole campaign) -- not only the 3 raw per-run
        # ScenarioRunOutcome statuses.
        "scenario_statuses": [
            "completed",
            "rejected",
            "crashed",
            "timeout",
            "never_selected",
        ],
        "child_execution_failure_reasons": {
            # Captain security correction (2026-07-17): raw exception/log
            # text never becomes a persisted reason_code -- these FIXED
            # codes are the only non-selection/non-funding/non-gap reasons a
            # crashed/timeout/global-failure attempt can carry.
            "crashed": REASON_CHILD_EXECUTION_CRASHED,
            "timeout": REASON_CHILD_EXECUTION_TIMEOUT,
            "global_corpus_load_failed": REASON_GLOBAL_CORPUS_LOAD_FAILED,
        },
        "never_selected_in_any_fold_reason": REASON_NEVER_SELECTED_IN_ANY_FOLD,
    }


class ExperimentIdDerivationError(FrozenCampaignError):
    """The 24 rows did not derive exactly 24 unique canonical experiment IDs."""


def _derive_experiment_ids_from_rows(rows: tuple[dict, ...]) -> tuple[str, ...]:
    """Q4 addendum (captain, 2026-07-17): the acyclic envelope must contain
    the exact ORDERED 24 experiment IDs as an explicit, hashed component --
    not merely a value re-derivable off-envelope by the CLI after the fact.
    This is step 3 of the acyclic chain (component hashes -> IDs -> top
    payload/hash): each row's ``components`` dict (already built in step 2,
    from frozen step-1 components) is hashed per-component and combined into
    one canonical experiment_id via the SAME
    ``research_contracts.canonical_hash`` authority the CLI and H6 registry
    both use (reached here via the ``identity`` module's own ``canonical_hash``
    import -- no new/duplicate hashing authority). The result is then embedded
    in the envelope BEFORE the top-level ``full_campaign_hash()`` call, so
    that hash now explicitly commits to the ordered 24 IDs themselves, not
    only to the raw components they were derived from. Order is exactly
    ``rows``' own order (one experiment_id per row, same index) -- there is
    no separate sort/reorder step, so index-for-index correspondence with
    ``rows`` is structural, not asserted after the fact.
    """
    ids = tuple(
        identity.canonical_hash.derive_experiment_id(
            row["strategy_key"],
            row["strategy_version"],
            identity.canonical_hash.compute_identity_hashes(row["components"]),
        )
        for row in rows
    )
    if len(ids) != 24 or len(set(ids)) != 24:
        raise ExperimentIdDerivationError(
            f"expected exactly 24 unique experiment IDs derived from 24 rows, got "
            f"{len(ids)} total ({len(set(ids))} unique)"
        )
    return ids


def _fold_to_dict(fold: foldmod.Fold) -> dict:
    return {
        "fold_id": fold.fold_id,
        "fold_index": fold.fold_index,
        "train_start_ms": fold.train_start_ms,
        "train_end_ms": fold.train_end_ms,
        "embargo_start_ms": fold.embargo_start_ms,
        "embargo_end_ms": fold.embargo_end_ms,
        "oos_start_ms": fold.oos_start_ms,
        "oos_end_ms": fold.oos_end_ms,
    }


def _freeze(obj: Any) -> Any:
    """Recursively convert dict/list/tuple into an immutable structure
    (``types.MappingProxyType`` + ``tuple``). This is simultaneously a full,
    non-aliasing deep copy (every nested dict/list is rebuilt into a BRAND
    NEW node) and a mutation guard (any attempted item assignment on the
    result raises ``TypeError``) -- the two properties the frozen-lineage
    correction requires together."""
    if isinstance(obj, types.MappingProxyType):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, dict):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(v) for v in obj)
    return obj


def _unfreeze(obj: Any) -> Any:
    """The inverse of :func:`_freeze` -- rebuilds an ordinary, fully mutable
    dict/list structure. Used ONLY inside ``to_dict()``/hashing so a caller
    mutating the RETURNED structure can never reach the sealed internal
    state, and so ``canonical_sha256`` (which expects plain dict/list)
    receives ordinary types rather than ``MappingProxyType``/``tuple``
    (``copy.deepcopy`` cannot even traverse a bare ``mappingproxy``)."""
    if isinstance(obj, types.MappingProxyType):
        return {k: _unfreeze(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_unfreeze(v) for v in obj]
    return obj


@dataclass(frozen=True)
class FrozenCampaignEnvelope:
    """The full ROB-940/H4 campaign identity envelope. ``__post_init__``
    recursively FREEZES every nested dict/list field (see ``_freeze``) --
    this is both the sealing/deep-copy boundary (captain audit item 3) AND
    a hard mutation guard (captain frozen-lineage correction): unlike a bare
    ``@dataclass(frozen=True)``, which only blocks attribute REBINDING,
    nested field mutation (e.g.
    ``envelope.funding_pit_policy["entry_gate"]["x"] = 999``) now raises
    ``TypeError`` instead of silently corrupting a later
    ``full_campaign_hash()`` call -- regardless of whether the envelope was
    built via ``build_frozen_campaign_envelope`` or constructed directly.
    """

    schema_version: str
    window_start_iso: str
    window_end_iso: str
    universe: tuple[str, ...]
    dataset_manifest_hash: str
    signal_manifest_hash: str
    rows: tuple[dict, ...]
    experiment_ids: tuple[str, ...]
    fold_schedule: tuple[dict, ...]
    scenario_execution: str
    funding_pit_policy: dict
    data_gap_policy: dict
    posture: dict
    execution_code_provenance: dict[str, str]
    h3_fixed_constants: dict
    h4_reason_contract: dict

    def __post_init__(self) -> None:
        for name in (
            "universe",
            "rows",
            "experiment_ids",
            "fold_schedule",
            "funding_pit_policy",
            "data_gap_policy",
            "posture",
            "execution_code_provenance",
            "h3_fixed_constants",
            "h4_reason_contract",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def to_dict(self) -> dict:
        """A freshly-rebuilt plain-dict view -- mutating the RETURNED
        structure must never leak back into this sealed envelope."""
        return {
            "schema_version": self.schema_version,
            "window_start_iso": self.window_start_iso,
            "window_end_iso": self.window_end_iso,
            "universe": _unfreeze(self.universe),
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "signal_manifest_hash": self.signal_manifest_hash,
            "rows": _unfreeze(self.rows),
            "experiment_ids": _unfreeze(self.experiment_ids),
            "fold_schedule": _unfreeze(self.fold_schedule),
            "scenario_execution": self.scenario_execution,
            "funding_pit_policy": _unfreeze(self.funding_pit_policy),
            "data_gap_policy": _unfreeze(self.data_gap_policy),
            "posture": _unfreeze(self.posture),
            "execution_code_provenance": _unfreeze(self.execution_code_provenance),
            "h3_fixed_constants": _unfreeze(self.h3_fixed_constants),
            "h4_reason_contract": _unfreeze(self.h4_reason_contract),
        }

    def full_campaign_hash(self) -> str:
        """The ONE top-level hash (Q4 step 4) -- never fed back into any of
        this envelope's own inputs."""
        return identity.canonical_hash.canonical_sha256(self.to_dict())


def build_frozen_campaign_envelope(
    *,
    config_rows: list[identity.CampaignConfigRow],
    sources: dict[str, identity.StrategySourceProvenance],
    dataset_manifest: dict,
    dataset_manifest_expected_hash: str,
    fold_schedule: tuple[foldmod.Fold, ...],
    signal_manifest_hash_value: str,
    expected_signal_manifest_hash: str,
    schema_version: str = SCHEMA_VERSION,
    execution_code_files: tuple[tuple[str, Path], ...] | None = None,
) -> FrozenCampaignEnvelope:
    """Build the acyclic envelope from already-frozen components (Q4 steps
    1-3). Fail-closed BEFORE building the 24 H6 specs if the caller's
    asserted H3 ``signal_manifest_hash`` pin is stale; the dataset-manifest
    hash and campaign-row-shape checks are ``rob946_campaign_identity``'s own
    (its errors propagate unchanged, never re-wrapped or swallowed).
    ``FrozenCampaignEnvelope.__post_init__`` performs the actual sealing/
    freezing -- this function just assembles plain dicts.
    """
    if signal_manifest_hash_value != expected_signal_manifest_hash:
        raise FrozenCampaignError(
            "H3 signal_manifest_hash mismatch (expected "
            f"{expected_signal_manifest_hash}, actual {signal_manifest_hash_value}) "
            "-- stale or tampered H3 signal manifest"
        )
    _assert_canonical_row_order(config_rows)
    # Captain boundary correction (2026-07-17): H4 owns exactly ONE frozen
    # production envelope -- unlike H6's rob946_campaign_identity (a
    # deliberately generic, manifest-injected mechanism), this builder must
    # not silently accept a same-domain param swap or altered hypothesis
    # riding under an otherwise-correct/correctly-ordered config_id. Every
    # row is verified against the frozen H3 manifest BEFORE any spec/ID is
    # built.
    for row in config_rows:
        _assert_row_matches_frozen_h3_manifest(row)

    specs = identity.build_campaign_experiment_specs(
        config_rows,
        sources=sources,
        dataset_manifest=dataset_manifest,
        dataset_manifest_expected_hash=dataset_manifest_expected_hash,
    )

    rows = tuple(
        {
            "strategy_key": spec.strategy_key,
            "strategy_version": spec.strategy_version,
            "hypothesis": spec.hypothesis,
            "components": spec.components,
        }
        for spec in specs
    )
    fold_dicts = tuple(_fold_to_dict(f) for f in fold_schedule)
    # Q4 addendum (step 3): derive the ordered 24 experiment IDs from the
    # already-built rows/components BEFORE constructing the envelope, so the
    # top-level full_campaign_hash (step 4) explicitly commits to them --
    # acyclic (IDs never feed back into the components they were derived
    # from; the final hash is never fed back into anything).
    experiment_ids = _derive_experiment_ids_from_rows(rows)

    return FrozenCampaignEnvelope(
        schema_version=schema_version,
        window_start_iso=frozen.WINDOW_START_ISO,
        window_end_iso=frozen.WINDOW_END_ISO,
        universe=frozen.UNIVERSE,
        dataset_manifest_hash=dataset_manifest_expected_hash,
        signal_manifest_hash=expected_signal_manifest_hash,
        rows=rows,
        experiment_ids=experiment_ids,
        fold_schedule=fold_dicts,
        scenario_execution=SCENARIO_EXECUTION_SEMANTICS,
        funding_pit_policy=build_funding_pit_policy_component(),
        data_gap_policy=build_data_gap_policy_component(),
        posture=build_posture_component(),
        execution_code_provenance=build_execution_code_provenance_component(
            execution_code_files
        ),
        h3_fixed_constants=build_h3_fixed_constants_component(),
        h4_reason_contract=build_h4_reason_contract_component(),
    )


def build_production_campaign_config_rows() -> list[identity.CampaignConfigRow]:
    """The exact frozen 24 rows, read directly FROM ``rob940_signal_manifest``'s
    own frozen tuples -- never a second hand-copied 24-row table. Each
    constructed row is immediately self-verified via
    ``_assert_row_matches_frozen_h3_manifest`` (reconstruct + H3's own
    dataclass-``==`` membership check) -- a regression guard against a future
    field-mapping bug in this translation (e.g. an accidental ``k_SL``/``R_TP``
    swap) rather than a tautology, since ``params``/``hypothesis`` here are a
    hand-written transcription of the S1Config/S2Config fields, not the same
    object.
    """
    rows: list[identity.CampaignConfigRow] = []
    for c in FROZEN_S1_CONFIGS:
        rows.append(
            identity.CampaignConfigRow(
                config_id=c.config_id,
                params={"L": c.L, "q_min": c.q_min, "k_SL": c.k_SL, "R_TP": c.R_TP},
                hypothesis=c.hypothesis,
            )
        )
    for c in FROZEN_S2_CONFIGS:
        rows.append(
            identity.CampaignConfigRow(
                config_id=c.config_id,
                params={
                    "z_min": c.z_min,
                    "v_min": c.v_min,
                    "ER_max": c.ER_max,
                    "R_min": c.R_min,
                },
                hypothesis=c.hypothesis,
            )
        )
    for row in rows:
        _assert_row_matches_frozen_h3_manifest(row)
    return rows


def load_production_dataset_manifest() -> dict:
    """Load + verify the committed H1 corpus manifest fixture against the
    pinned ``H1_MANIFEST_EXPECTED_CONTENT_HASH``. Returns a fresh deep copy
    (no aliasing with any cached/module-level object)."""
    manifest = CorpusManifest.load(H1_MANIFEST_PATH)
    actual_hash = manifest.content_hash()
    if actual_hash != H1_MANIFEST_EXPECTED_CONTENT_HASH:
        raise FrozenCampaignError(
            f"H1 corpus manifest content_hash mismatch (expected "
            f"{H1_MANIFEST_EXPECTED_CONTENT_HASH}, actual {actual_hash}) -- the "
            "committed fixture has drifted from the frozen pin"
        )
    return copy.deepcopy(manifest.to_dict())


def build_production_strategy_sources(
    *,
    expected_s1_source_sha256: str | None = None,
    expected_s2_source_sha256: str | None = None,
) -> dict[str, identity.StrategySourceProvenance]:
    """Real, byte-derived S1/S2 strategy source provenance, read from the
    actual committed source files. A stale ``expected_*_source_sha256`` pin
    fails closed on ``.verified_source_sha256()`` (H6's own discipline).

    Captain config/plan audit (2026-07-18, item B): reads raw bytes and
    decodes them via a STRICT UTF-8 decode -- never ``Path.read_text()``,
    which opens the file in universal-newline TEXT mode and silently
    translates any ``\\r\\n``/``\\r`` sequence to ``\\n`` before this
    function ever sees the string. ``StrategySourceProvenance`` (merged
    H6, ``rob946_campaign_identity.py`` -- not modified here) computes
    ``hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()``; a
    plain ``bytes.decode("utf-8")`` (no I/O layer, no newline translation)
    is a LOSSLESS round-trip for valid UTF-8 bytes, so
    ``source_text.encode("utf-8")`` reproduces the ORIGINAL file bytes
    exactly -- ``hashlib.sha256(path.read_bytes()).hexdigest()`` --
    regardless of the file's actual line-ending convention.
    """
    return {
        "S1": identity.StrategySourceProvenance(
            strategy_key=PRODUCTION_S1_STRATEGY_KEY,
            strategy_version=PRODUCTION_S1_STRATEGY_VERSION,
            source_text=_S1_SOURCE_PATH.read_bytes().decode("utf-8"),
            expected_source_sha256=expected_s1_source_sha256,
        ),
        "S2": identity.StrategySourceProvenance(
            strategy_key=PRODUCTION_S2_STRATEGY_KEY,
            strategy_version=PRODUCTION_S2_STRATEGY_VERSION,
            source_text=_S2_SOURCE_PATH.read_bytes().decode("utf-8"),
            expected_source_sha256=expected_s2_source_sha256,
        ),
    }


def build_production_frozen_campaign_envelope(
    *,
    expected_signal_manifest_hash: str = H3_MANIFEST_EXPECTED_HASH,
    expected_dataset_manifest_hash: str = H1_MANIFEST_EXPECTED_CONTENT_HASH,
    execution_code_base_dir: Path | None = None,
) -> FrozenCampaignEnvelope:
    """The real, production ROB-940 campaign envelope -- pure, no network/DB/
    child execution. Every hash pin is independently re-verified against the
    actual committed source/fixture, never trusted as a bare literal.

    ``execution_code_base_dir`` is a TEST-ONLY escape hatch: when given, the
    execution-code provenance component is computed from files of the same
    logical names under that directory instead of the real repo files --
    used to prove the envelope's hash is genuinely sensitive to execution
    code bytes without mutating real source. Production callers never pass
    this.
    """
    dataset_manifest = load_production_dataset_manifest()
    actual_dm_hash = identity.canonical_hash.canonical_sha256(dataset_manifest)
    if actual_dm_hash != expected_dataset_manifest_hash:
        raise FrozenCampaignError(
            "H1 dataset manifest hash mismatch (expected "
            f"{expected_dataset_manifest_hash}, actual {actual_dm_hash})"
        )
    fold_schedule = foldmod.generate_frozen_fold_schedule(
        frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
    )
    execution_code_files = None
    if execution_code_base_dir is not None:
        execution_code_files = tuple(
            (name, execution_code_base_dir / name)
            for name, _real_path in EXECUTION_CODE_LOGICAL_FILES
        )
    return build_frozen_campaign_envelope(
        config_rows=build_production_campaign_config_rows(),
        sources=build_production_strategy_sources(),
        dataset_manifest=dataset_manifest,
        dataset_manifest_expected_hash=actual_dm_hash,
        fold_schedule=fold_schedule,
        signal_manifest_hash_value=_ACTUAL_H3_SIGNAL_MANIFEST_HASH,
        expected_signal_manifest_hash=expected_signal_manifest_hash,
        execution_code_files=execution_code_files,
    )
