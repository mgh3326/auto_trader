"""ROB-981 (ROB-974 R2 H6-A) CP1 -- pure, generic exact-48 campaign-identity
kernel.

Deliberately GENERIC and predecessor-port-injected, mirroring
``rob946_campaign_identity``'s discipline for the OLD 24-row S1/S2 campaign
(ROB-940/946), generalized to the NEW 48-row S3/S4 campaign (ROB-974 R2).
Callers (tests today; the real H2/H3 adapter builder at CP8) supply
already-verified H1/H2/H3 components -- this module never fabricates a
competing "production" H2/H3 manifest of its own and exposes NO production
builder (that is CP8-only, gated on orch-supplied verified merge SHAs). Every
row/contract fixture built here carries an explicit ``provenance`` field so a
test-only identity can never be silently mistaken for a production one.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
shared ``research_contracts.canonical_hash`` typed-canonical authority.
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from research_contracts.canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)

__all__ = [
    "CANONICAL_ROW_ORDER",
    "CONFIGS_PER_STRATEGY",
    "EXPECTED_STRATEGY_SLUGS",
    "EXPECTED_TOTAL_ROWS",
    "CampaignConfigRow",
    "ComponentDriftError",
    "EnvelopeIdMismatchError",
    "H6AIdentityError",
    "H6ARowSpec",
    "Provenance",
    "ProvenanceMismatchError",
    "RowCountError",
    "RowIdError",
    "StaleSourcePinError",
    "StrategyContractProvenance",
    "assert_specs_in_canonical_order",
    "build_campaign_row_specs",
    "derive_row_experiment_id",
    "validate_campaign_rows",
    "validate_same_strategy_components_identical",
    "verify_row_experiment_id",
]

_ROW_ID_RE = re.compile(r"^S(?P<slug>[34])-(?P<idx>\d{2})$")
CONFIGS_PER_STRATEGY = 24
EXPECTED_TOTAL_ROWS = 48
EXPECTED_STRATEGY_SLUGS: tuple[str, ...] = ("S3", "S4")
CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(
    f"{slug}-{i:02d}"
    for slug in EXPECTED_STRATEGY_SLUGS
    for i in range(CONFIGS_PER_STRATEGY)
)

# The 11-slot identity component authority (ROB-846) is shared verbatim --
# ROB-974-specific content (folds/embargo/PBO/gates/pair-order/tri-state/
# H1/H2/H3 contract hashes) is folded INTO these fixed slots by the caller
# (mirrors rob946_campaign_identity's own discipline; this module never adds
# a 12th slot).
_NON_PARAMS_COMPONENTS: tuple[str, ...] = (
    "strategy",
    "code",
    "dataset_manifest",
    "universe",
    "pit",
    "frozen_config",
    "policy",
    "benchmark",
    "cost",
    "mdd",
)

Provenance = Literal["fixture_identity", "production"]
_ALLOWED_PROVENANCE = frozenset({"fixture_identity", "production"})
# CP8 (research/nautilus_scalping/rob974_h6a_h2h3_adapter.py) is the ONLY
# module authorized to construct provenance="production" values -- it does so
# only after independently re-deriving and verifying the real merged H2/H3
# production surface (see that module's ``verify_h2h3_contract``). This
# module itself fabricates NO competing production H2/H3 manifest and adds no
# H2/H3 verification of its own; a bare "production" literal here is only a
# type-membership check. The actual drift protection lives at two structural
# points below: ``StrategyContractProvenance`` requires a pinned
# ``expected_contract_hash`` for "production", and ``build_campaign_row_specs``
# refuses any mix of "fixture_identity"/"production" across the two contracts
# or the 48 rows (``ProvenanceMismatchError``).


class H6AIdentityError(ValueError):
    """Base error for the ROB-981 H6-A identity kernel."""


class RowCountError(H6AIdentityError):
    """The campaign does not have exactly 48 rows (24 S3 + 24 S4)."""


class RowIdError(H6AIdentityError):
    """A row_id is malformed, duplicated, reordered, or the per-strategy set
    is wrong (missing/cross-strategy overlap)."""


class ComponentDriftError(H6AIdentityError):
    """A non-``params`` component differs between two rows of the SAME
    strategy -- only ``params`` (config_row) may vary within a strategy."""


class EnvelopeIdMismatchError(H6AIdentityError):
    """An envelope-embedded experiment_id does not equal the value
    independently re-derived from trusted components (covers arbitrary/
    forged/stale run IDs standing in for a real experiment_id)."""


class StaleSourcePinError(H6AIdentityError):
    """A caller-asserted ``expected_contract_hash`` does not match the
    contract hash actually supplied -- refused before any row is built."""


class ProvenanceMismatchError(H6AIdentityError):
    """The two strategy contracts, or a row among the 48, disagree on
    ``provenance`` -- a real production campaign can never be built from a
    mix of fixture and production components."""


def _freeze(obj: Any) -> Any:
    """Recursively converts dict/list into an immutable structure
    (``types.MappingProxyType`` + ``tuple``) with a full deep, non-aliasing
    copy -- a caller's own pre-construction dict can never leak into (or
    later mutate) a sealed ``H6ARowSpec.components``, and the sealed
    object's own nested fields can never be mutated in place either.
    Mirrors ``rob944_frozen_campaign._freeze``/``rob974_h6a_payload._freeze``.
    """
    if isinstance(obj, Mapping):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list | tuple):
        return tuple(_freeze(v) for v in obj)
    return obj


def _unfreeze(obj: Any) -> Any:
    """The dual of ``_freeze`` -- recognizes BOTH an already-frozen
    ``MappingProxyType``/``tuple`` snapshot and a plain ``dict``/``list``
    (e.g. a fresh, still-mutable ``components`` dict mid-construction) so a
    single call normalizes either shape to canonical-hash-safe built-ins."""
    if isinstance(obj, Mapping):
        return {k: _unfreeze(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_unfreeze(v) for v in obj]
    return obj


def _provenance_field(value: object, *, field: str) -> Provenance:
    if value not in _ALLOWED_PROVENANCE:
        raise H6AIdentityError(
            f"{field} must be one of {sorted(_ALLOWED_PROVENANCE)}, got {value!r}"
        )
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class CampaignConfigRow:
    """One of the 48 approved (strategy, parameter-set) rows.

    Carries no symbol/pair field of its own -- ``universe``/``pair_order`` are
    shared components injected by the caller, never a per-row override.
    """

    row_id: str
    params: dict[str, Any]
    hypothesis: str
    authority_label: str
    provenance: Provenance

    def __post_init__(self) -> None:
        _provenance_field(self.provenance, field="CampaignConfigRow.provenance")

    def strategy_slug(self) -> str:
        match = _ROW_ID_RE.match(self.row_id)
        if not match:
            raise RowIdError(
                f"row_id {self.row_id!r} does not match the required S<3|4>-NN pattern"
            )
        return f"S{match.group('slug')}"


@dataclass(frozen=True)
class StrategyContractProvenance:
    """One strategy's (S3 or S4) verified H3 structured contract identity.

    ``expected_contract_hash``, if given, is compared against
    ``contract_hash`` and a mismatch fails closed (a stale/tampered pin)
    exactly like ``rob946_campaign_identity.StrategySourceProvenance``.
    """

    strategy_slug: str
    strategy_key: str
    strategy_version: str
    contract_hash: str
    contract_key: str
    provenance: Provenance
    expected_contract_hash: str | None = None

    def __post_init__(self) -> None:
        _provenance_field(
            self.provenance, field="StrategyContractProvenance.provenance"
        )
        if self.provenance == "production" and self.expected_contract_hash is None:
            raise H6AIdentityError(
                f"{self.strategy_slug}: provenance='production' requires a pinned "
                "expected_contract_hash -- refusing to construct an unpinned production "
                "contract (stale/tampered source pin protection)"
            )

    def verified_contract_hash(self) -> str:
        if (
            self.expected_contract_hash is not None
            and self.contract_hash != self.expected_contract_hash
        ):
            raise StaleSourcePinError(
                f"{self.strategy_slug}: structured contract hash mismatch -- stale or "
                "tampered H3 source pin"
            )
        return self.contract_hash


@dataclass(frozen=True)
class H6ARowSpec:
    """One row's full identity, ready to feed an app-side
    ``StrategyExperimentIdentity`` (CP5) -- this module never imports that
    schema, keeping the pure research side app-free."""

    row_id: str
    strategy_key: str
    strategy_version: str
    hypothesis: str
    components: Mapping[str, Any]
    provenance: Provenance
    experiment_id: str


def validate_campaign_rows(rows: list[CampaignConfigRow]) -> None:
    """Fail closed unless ``rows`` is exactly 24 S3 + 24 S4 rows, no dup/
    missing/cross-strategy-overlap, in the required id shape."""
    if len(rows) != EXPECTED_TOTAL_ROWS:
        raise RowCountError(
            f"expected exactly {EXPECTED_TOTAL_ROWS} campaign rows, got {len(rows)}"
        )
    ids = [row.row_id for row in rows]
    if len(set(ids)) != len(ids):
        duplicates = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
        raise RowIdError(f"duplicate row_id(s): {duplicates}")

    by_slug: dict[str, list[str]] = {}
    for row in rows:
        by_slug.setdefault(row.strategy_slug(), []).append(row.row_id)

    if sorted(by_slug) != sorted(EXPECTED_STRATEGY_SLUGS):
        raise RowIdError(
            f"expected exactly strategy slugs {sorted(EXPECTED_STRATEGY_SLUGS)}, got "
            f"{sorted(by_slug)}"
        )
    for slug in EXPECTED_STRATEGY_SLUGS:
        expected_ids = [f"{slug}-{i:02d}" for i in range(CONFIGS_PER_STRATEGY)]
        if sorted(by_slug[slug]) != expected_ids:
            raise RowIdError(
                f"{slug}: expected exactly {expected_ids}, got {sorted(by_slug[slug])}"
            )


def _build_strategy_component(
    slug: str, contract: StrategyContractProvenance
) -> dict[str, Any]:
    return {
        "slug": slug,
        "strategy_key": contract.strategy_key,
        "strategy_version": contract.strategy_version,
    }


def _build_code_component(contract: StrategyContractProvenance) -> dict[str, Any]:
    # H3's structured strategy_contract_hash stands in for "code provenance"
    # (no importable S3/S4 generator source exists yet in this fixture-only
    # phase) -- also carries contract_key so a key-only collision changes it.
    return {
        "contract_hash": contract.verified_contract_hash(),
        "contract_key": contract.contract_key,
    }


def _build_params_component(row: CampaignConfigRow) -> dict[str, Any]:
    """The ONLY component allowed to vary within a strategy's 24 rows."""
    return {
        "row_id": row.row_id,
        "hypothesis": row.hypothesis,
        "authority_label": row.authority_label,
        **row.params,
    }


def build_campaign_row_specs(
    rows: list[CampaignConfigRow],
    *,
    contracts: Mapping[str, StrategyContractProvenance],
    shared_components: Mapping[str, Any],
    pit_component_by_slug: Mapping[str, Any],
    frozen_config_component_by_slug: Mapping[str, Any],
    policy_component_by_slug: Mapping[str, Any],
    cost_component_by_slug: Mapping[str, Any],
) -> tuple[H6ARowSpec, ...]:
    """Build all 48 row identity specs from injected rows/contracts/shared
    components, in canonical order. Nothing is written/persisted here.

    Fail-closed order: row-shape (count/id pattern) -> strategy-contract
    presence/distinctness/stale-pin -> per-row component assembly ->
    same-strategy component-identity check -> independent experiment_id
    derivation.
    """
    validate_campaign_rows(rows)

    if set(contracts) != set(EXPECTED_STRATEGY_SLUGS):
        raise H6AIdentityError(
            f"expected strategy contracts for exactly {sorted(EXPECTED_STRATEGY_SLUGS)}, "
            f"got {sorted(contracts)}"
        )
    s3_contract, s4_contract = contracts["S3"], contracts["S4"]
    if s3_contract.strategy_key == s4_contract.strategy_key:
        raise H6AIdentityError("S3 and S4 must have different strategy_key")
    # strategy_version is NOT required to differ across S3/S4 -- the real
    # merged H3 manifest pins both contracts at version "1"
    # (rob974_h3_manifest.S3_STRATEGY_CONTRACT/S4_STRATEGY_CONTRACT). Distinct
    # strategy_key alone already guarantees experiment_id uniqueness, since
    # derive_experiment_id hashes strategy_key into the identity payload.
    if s3_contract.verified_contract_hash() == s4_contract.verified_contract_hash():
        raise H6AIdentityError(
            "S3 and S4 must have different structured contract hashes"
        )
    if s3_contract.provenance != s4_contract.provenance:
        raise ProvenanceMismatchError(
            f"S3 contract provenance {s3_contract.provenance!r} != "
            f"S4 contract provenance {s4_contract.provenance!r}"
        )
    common_provenance = s3_contract.provenance
    mismatched_rows = [
        row.row_id for row in rows if row.provenance != common_provenance
    ]
    if mismatched_rows:
        raise ProvenanceMismatchError(
            f"row(s) {sorted(mismatched_rows)} provenance does not match the "
            f"contracts' provenance {common_provenance!r}"
        )

    dataset_manifest_component = dict(shared_components["dataset_manifest"])
    universe_component = dict(shared_components["universe"])
    benchmark_component = dict(shared_components["benchmark"])
    mdd_component = dict(shared_components["mdd"])

    rows_by_id = {row.row_id: row for row in rows}
    specs: list[H6ARowSpec] = []
    for row_id in CANONICAL_ROW_ORDER:
        row = rows_by_id[row_id]
        slug = row.strategy_slug()
        contract = contracts[slug]
        components: dict[str, Any] = {
            "strategy": _build_strategy_component(slug, contract),
            "code": _build_code_component(contract),
            "params": _build_params_component(row),
            "dataset_manifest": dataset_manifest_component,
            "universe": universe_component,
            "pit": dict(pit_component_by_slug[slug]),
            "frozen_config": dict(frozen_config_component_by_slug[slug]),
            "policy": dict(policy_component_by_slug[slug]),
            "benchmark": benchmark_component,
            "cost": dict(cost_component_by_slug[slug]),
            "mdd": mdd_component,
        }
        experiment_id = derive_row_experiment_id(
            contract.strategy_key, contract.strategy_version, components
        )
        specs.append(
            H6ARowSpec(
                row_id=row_id,
                strategy_key=contract.strategy_key,
                strategy_version=contract.strategy_version,
                hypothesis=row.hypothesis,
                components=_freeze(components),
                provenance=common_provenance,
                experiment_id=experiment_id,
            )
        )

    validate_same_strategy_components_identical(specs)
    return tuple(specs)


def validate_same_strategy_components_identical(
    specs: list[H6ARowSpec] | tuple[H6ARowSpec, ...],
) -> None:
    """Fail closed unless every non-``params`` component is identical across
    all rows sharing a ``strategy_key`` -- only ``params`` (config_row) may
    vary within one strategy's 24 rows."""
    by_strategy: dict[str, list[H6ARowSpec]] = {}
    for spec in specs:
        by_strategy.setdefault(spec.strategy_key, []).append(spec)
    for strategy_key, group in by_strategy.items():
        first = group[0]
        for other in group[1:]:
            for name in _NON_PARAMS_COMPONENTS:
                if first.components[name] != other.components[name]:
                    raise ComponentDriftError(
                        f"strategy_key {strategy_key!r}: component {name!r} differs between "
                        f"{first.row_id!r} and {other.row_id!r} -- only 'params' may vary "
                        "within one strategy's rows"
                    )
            if first.hypothesis != other.hypothesis:
                raise ComponentDriftError(
                    f"strategy_key {strategy_key!r}: hypothesis differs between "
                    f"{first.row_id!r} and {other.row_id!r}"
                )


def derive_row_experiment_id(
    strategy_key: str, strategy_version: str, components: Mapping[str, Any]
) -> str:
    """Independently derive one row's canonical experiment_id from trusted
    components -- the SAME ROB-846 typed-canonical authority the app-side
    registry uses (``compute_identity_hashes``/``derive_experiment_id``).
    Accepts either a plain dict (pre-freeze, during construction) or an
    already-frozen ``MappingProxyType`` snapshot (post-construction,
    ``verify_row_experiment_id``) -- ``_unfreeze`` normalizes either shape
    to plain dict/list before hashing, since the canonical-hash encoder
    only recognizes built-in ``dict``/``list``."""
    hashes = compute_identity_hashes(_unfreeze(components))
    return derive_experiment_id(strategy_key, strategy_version, hashes)


def verify_row_experiment_id(spec: H6ARowSpec, *, envelope_experiment_id: str) -> None:
    """Reject an envelope-embedded experiment_id (or an arbitrary run ID
    standing in for one) that does not equal the independently re-derived
    value -- never trust the envelope's own claim."""
    recomputed = derive_row_experiment_id(
        spec.strategy_key, spec.strategy_version, spec.components
    )
    if (
        envelope_experiment_id != recomputed
        or envelope_experiment_id != spec.experiment_id
    ):
        raise EnvelopeIdMismatchError(
            f"row {spec.row_id!r}: envelope experiment_id does not match the value "
            "independently re-derived from trusted components"
        )


def assert_specs_in_canonical_order(specs: tuple[H6ARowSpec, ...]) -> None:
    """Reject a reordered/permuted spec sequence -- canonical order is
    exactly ``S3-00..S3-23,S4-00..S4-23``."""
    actual = tuple(spec.row_id for spec in specs)
    if actual != CANONICAL_ROW_ORDER:
        raise RowIdError(
            "spec sequence is not in the exact canonical S3-00..S3-23,S4-00..S4-23 order"
        )
