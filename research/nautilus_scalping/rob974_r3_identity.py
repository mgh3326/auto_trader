"""Pure ROB-974 R3 exact-12 H6-A identity lineage.

R2's public exact-48 kernel is intentionally not imported or modified.  This
module independently seals the literal R3 3+9 roster while reusing only the
shared typed-canonical hashing authority.
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from rob974_r3_shape import R3_CANONICAL_ROW_ORDER

from research_contracts.canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)

__all__ = [
    "Exact12IdentityError",
    "R3CampaignConfigRow",
    "R3RowSpec",
    "R3StrategyContractProvenance",
    "build_r3_campaign_row_specs",
    "derive_r3_row_experiment_id",
    "validate_r3_campaign_rows",
    "validate_r3_same_strategy_components",
    "verify_r3_row_experiment_id",
]

_ROW_ID_RE = re.compile(r"^(?P<slug>S[34])-R3-\d{2}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_SLUGS: tuple[str, str] = ("S3", "S4")
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
_PROVENANCE = frozenset({"fixture_identity", "production"})
_RESERVED_PARAM_KEYS = frozenset({"row_id", "hypothesis", "authority_label"})


class Exact12IdentityError(ValueError):
    """R3 identity input differs from the literal exact-12 contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _unfreeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _unfreeze(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_unfreeze(item) for item in value]
    return value


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise Exact12IdentityError(f"{name} must be lowercase 64-hex")
    return value


def _provenance(value: object, name: str) -> Provenance:
    if value not in _PROVENANCE:
        raise Exact12IdentityError(f"{name} is outside the closed provenance set")
    return value  # type: ignore[return-value]


def _slug(row_id: str) -> str:
    match = _ROW_ID_RE.fullmatch(row_id)
    if match is None:
        raise Exact12IdentityError(
            f"row ID {row_id!r} is not a literal S3-R3-NN/S4-R3-NN ID"
        )
    return match.group("slug")


@dataclass(frozen=True, slots=True)
class R3CampaignConfigRow:
    row_id: str
    params: dict[str, Any]
    hypothesis: str
    authority_label: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if type(self.row_id) is not str:
            raise Exact12IdentityError("row_id must be exact str")
        _slug(self.row_id)
        if type(self.params) is not dict:
            raise Exact12IdentityError("params must be an exact dict")
        if _RESERVED_PARAM_KEYS & self.params.keys():
            raise Exact12IdentityError(
                "params cannot override row_id, hypothesis, or authority_label"
            )
        if type(self.hypothesis) is not str or not self.hypothesis:
            raise Exact12IdentityError("hypothesis must be non-empty exact str")
        if type(self.authority_label) is not str or not self.authority_label:
            raise Exact12IdentityError("authority_label must be non-empty exact str")
        _provenance(self.provenance, "row provenance")

    @property
    def strategy_slug(self) -> str:
        return _slug(self.row_id)


@dataclass(frozen=True, slots=True)
class R3StrategyContractProvenance:
    strategy_slug: str
    strategy_key: str
    strategy_version: str
    contract_hash: str
    contract_key: str
    provenance: Provenance
    expected_contract_hash: str | None = None

    def __post_init__(self) -> None:
        if self.strategy_slug not in _EXPECTED_SLUGS:
            raise Exact12IdentityError("strategy_slug must be S3 or S4")
        for name in ("strategy_key", "strategy_version", "contract_key"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise Exact12IdentityError(f"{name} must be non-empty exact str")
        _hex64(self.contract_hash, "contract_hash")
        _provenance(self.provenance, "contract provenance")
        if self.provenance == "production" and self.expected_contract_hash is None:
            raise Exact12IdentityError(
                "production R3 contract requires expected_contract_hash"
            )
        if self.expected_contract_hash is not None:
            _hex64(self.expected_contract_hash, "expected_contract_hash")

    def verified_contract_hash(self) -> str:
        if (
            self.expected_contract_hash is not None
            and self.contract_hash != self.expected_contract_hash
        ):
            raise Exact12IdentityError("R3 strategy contract source pin drift")
        return self.contract_hash


@dataclass(frozen=True, slots=True)
class R3RowSpec:
    row_id: str
    strategy_key: str
    strategy_version: str
    hypothesis: str
    components: Mapping[str, Any]
    provenance: Provenance
    experiment_id: str


def validate_r3_campaign_rows(rows: tuple[R3CampaignConfigRow, ...]) -> None:
    if type(rows) is not tuple:
        raise Exact12IdentityError("R3 rows must be an exact tuple")
    if len(rows) != 12:
        raise Exact12IdentityError("R3 identity requires exactly 12 rows")
    if any(type(row) is not R3CampaignConfigRow for row in rows):
        raise Exact12IdentityError("R3 rows must use exact R3CampaignConfigRow")
    actual = tuple(row.row_id for row in rows)
    if actual != R3_CANONICAL_ROW_ORDER:
        raise Exact12IdentityError(
            "R3 rows must be literal S3-R3-00..02,S4-R3-00..08 order"
        )
    if len(set(actual)) != 12:
        raise Exact12IdentityError("R3 rows contain a duplicate row ID")
    if (
        tuple(row.strategy_slug for row in rows[:3]) != ("S3",) * 3
        or tuple(row.strategy_slug for row in rows[3:]) != ("S4",) * 9
    ):
        raise Exact12IdentityError("R3 family split must be exactly 3+9")


def derive_r3_row_experiment_id(
    strategy_key: str, strategy_version: str, components: Mapping[str, Any]
) -> str:
    hashes = compute_identity_hashes(_unfreeze(components))
    return derive_experiment_id(strategy_key, strategy_version, hashes)


def validate_r3_same_strategy_components(
    specs: tuple[R3RowSpec, ...],
) -> None:
    by_strategy: dict[str, list[R3RowSpec]] = {}
    for spec in specs:
        by_strategy.setdefault(spec.strategy_key, []).append(spec)
    for strategy_key, group in by_strategy.items():
        first = group[0]
        for other in group[1:]:
            for component in _NON_PARAMS_COMPONENTS:
                if first.components[component] != other.components[component]:
                    raise Exact12IdentityError(
                        f"{strategy_key}: {component} drifted within R3 family"
                    )
            if first.hypothesis != other.hypothesis:
                raise Exact12IdentityError(
                    f"{strategy_key}: hypothesis drifted within R3 family"
                )


def build_r3_campaign_row_specs(
    rows: tuple[R3CampaignConfigRow, ...],
    *,
    contracts: Mapping[str, R3StrategyContractProvenance],
    shared_components: Mapping[str, Any],
    pit_component_by_slug: Mapping[str, Any],
    frozen_config_component_by_slug: Mapping[str, Any],
    policy_component_by_slug: Mapping[str, Any],
    cost_component_by_slug: Mapping[str, Any],
) -> tuple[R3RowSpec, ...]:
    """Build the exact R3 identities after validating the complete roster."""

    validate_r3_campaign_rows(rows)
    if type(contracts) is not dict or tuple(contracts) != _EXPECTED_SLUGS:
        raise Exact12IdentityError("contracts must be exact ordered S3/S4 dict")
    s3_contract = contracts["S3"]
    s4_contract = contracts["S4"]
    if (
        type(s3_contract) is not R3StrategyContractProvenance
        or type(s4_contract) is not R3StrategyContractProvenance
    ):
        raise Exact12IdentityError("contracts must use exact R3 provenance DTOs")
    if s3_contract.strategy_slug != "S3" or s4_contract.strategy_slug != "S4":
        raise Exact12IdentityError("contract key/slug association drift")
    if s3_contract.strategy_key == s4_contract.strategy_key:
        raise Exact12IdentityError("R3 strategy keys must be distinct")
    if s3_contract.verified_contract_hash() == s4_contract.verified_contract_hash():
        raise Exact12IdentityError("R3 strategy contract hashes must be distinct")
    if s3_contract.provenance != s4_contract.provenance:
        raise Exact12IdentityError("R3 contract provenance mismatch")
    provenance = s3_contract.provenance
    if any(row.provenance != provenance for row in rows):
        raise Exact12IdentityError("R3 row/contract provenance mismatch")

    try:
        shared = {
            name: dict(shared_components[name])
            for name in ("dataset_manifest", "universe", "benchmark", "mdd")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise Exact12IdentityError(
            "R3 shared identity components are malformed"
        ) from exc

    specs: list[R3RowSpec] = []
    for row in rows:
        slug = row.strategy_slug
        contract = contracts[slug]
        try:
            components: dict[str, Any] = {
                "strategy": {
                    "slug": slug,
                    "lineage": "R3",
                    "strategy_key": contract.strategy_key,
                    "strategy_version": contract.strategy_version,
                },
                "code": {
                    "contract_hash": contract.verified_contract_hash(),
                    "contract_key": contract.contract_key,
                },
                "params": {
                    "row_id": row.row_id,
                    "hypothesis": row.hypothesis,
                    "authority_label": row.authority_label,
                    **row.params,
                },
                "dataset_manifest": shared["dataset_manifest"],
                "universe": shared["universe"],
                "pit": dict(pit_component_by_slug[slug]),
                "frozen_config": dict(frozen_config_component_by_slug[slug]),
                "policy": dict(policy_component_by_slug[slug]),
                "benchmark": shared["benchmark"],
                "cost": dict(cost_component_by_slug[slug]),
                "mdd": shared["mdd"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise Exact12IdentityError(
                f"R3 identity components are malformed for {row.row_id}"
            ) from exc
        experiment_id = derive_r3_row_experiment_id(
            contract.strategy_key, contract.strategy_version, components
        )
        specs.append(
            R3RowSpec(
                row_id=row.row_id,
                strategy_key=contract.strategy_key,
                strategy_version=contract.strategy_version,
                hypothesis=row.hypothesis,
                components=_freeze(components),
                provenance=provenance,
                experiment_id=experiment_id,
            )
        )
    result = tuple(specs)
    if len({spec.experiment_id for spec in result}) != 12:
        raise Exact12IdentityError("R3 experiment IDs must be unique")
    validate_r3_same_strategy_components(result)
    return result


def verify_r3_row_experiment_id(
    spec: R3RowSpec, *, envelope_experiment_id: str
) -> None:
    if type(spec) is not R3RowSpec:
        raise Exact12IdentityError("spec must be exact R3RowSpec")
    recomputed = derive_r3_row_experiment_id(
        spec.strategy_key, spec.strategy_version, spec.components
    )
    if envelope_experiment_id != spec.experiment_id or recomputed != spec.experiment_id:
        raise Exact12IdentityError(
            f"{spec.row_id}: R3 envelope experiment ID failed independent derivation"
        )
