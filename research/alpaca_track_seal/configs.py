"""ROB-1060 H2 — the exactly-16 canonical config domain.

Run A preregistration (SHA-256 ``67b5d3c2...``) SS10/S17: ``TOTAL_CONFIGS =
16``, never 17, never a 3rd family. AP-A1 DATS grid is the full Cartesian
product ``f in {14,21} x s in {56,84} x m in {28,56}`` (S11.4) with
``threshold = +-0.005`` FIXED (not a swept parameter). AP-A2 WCM-B grid is the
full Cartesian product ``L in {14,28} x k in {5,6} x b in {1,2}`` (S12.4) with
the positive filter ``Score > 0`` FIXED.

Naming is deterministic: ``AP-A1-00..07`` / ``AP-A2-00..07``, index assigned
by canonical nested-loop order (outer axis first, ascending on every axis) --
NOT insertion order or any hash-derived order, so re-running this module
always assigns the SAME index to the SAME grid point.

Every config hashes via the repo's typed canonical AST authority
(``research_contracts.canonical_hash`` via the local ``canonical_hash`` shim)
-- floats hash by ``float.hex()`` (1-ULP sensitive), dict keys sort
canonically (container-permutation invariant). ``build_all_configs`` takes NO
parameters: there is no caller-reachable extension point for a 17th slot or a
3rd family, by construction, not merely by convention.

Pure stdlib + ``canonical_hash``. No app/DB/network import.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import canonical_hash

__all__ = [
    "AP_A1_FIXED_THRESHOLD",
    "AP_A2_FIXED_POSITIVE_FILTER",
    "TOTAL_CONFIGS",
    "ConfigCountError",
    "ConfigDomainError",
    "ConfigSpec",
    "CrossFamilySupersedesError",
    "DuplicateConfigError",
    "assert_valid_supersedes",
    "build_ap_a1_configs",
    "build_ap_a2_configs",
    "build_all_configs",
    "canonical_config_hash",
    "validate_config_domain",
]

TOTAL_CONFIGS = 16

# S11.4 — threshold is FIXED, not swept. Exactly +-0.5% = 0.005.
AP_A1_FIXED_THRESHOLD = 0.005

# S12.4 — positive filter is FIXED, not swept.
AP_A2_FIXED_POSITIVE_FILTER = "Score > 0"

_AP_A1_F = (14, 21)
_AP_A1_S = (56, 84)
_AP_A1_M = (28, 56)

_AP_A2_L = (14, 28)
_AP_A2_K = (5, 6)
_AP_A2_B = (1, 2)


class ConfigDomainError(Exception):
    """Base error for the canonical config domain."""


class ConfigCountError(ConfigDomainError):
    """The config domain is not EXACTLY 16 — a 17th slot (or fewer) exists."""


class DuplicateConfigError(ConfigDomainError):
    """Two configs in the domain share a config_id or a canonical_hash."""


class CrossFamilySupersedesError(ConfigDomainError):
    """A supersedes lineage tried to cross AP-A1 <-> AP-A2 (ROB-846
    ``SupersedesStrategyMismatch`` lineage, AC5): an AP-A1 config may never
    supersede an AP-A2 config or vice versa."""


@dataclass(frozen=True)
class ConfigSpec:
    """One sealed, immutable config identity.

    ``params`` is a plain dict of built-in Python types (int/float/str only)
    -- exactly what the typed canonical AST authority hashes. There is no
    setter; correcting a config means registering a NEW ``ConfigSpec`` under
    a new lineage (supersedes), never mutating this one.
    """

    config_id: str
    family: str
    params: dict[str, Any]
    canonical_hash: str

    def __post_init__(self) -> None:
        # ROB-1060 H2-lock item 7: `frozen=True` blocks `config.params = ...`
        # but NOT `config.params["threshold"] = ...`, which silently rots the
        # already-computed `canonical_hash` (nothing re-derives it). Wrap
        # `params` in a genuinely immutable mapping so in-place mutation
        # raises instead of drifting the seal out from under its own hash.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


def canonical_config_hash(config_id: str, family: str, params: dict[str, Any]) -> str:
    """The canonical SHA-256 identity of one config.

    Uses the SAME typed canonical AST authority
    (``research_contracts.canonical_hash``) H1's corpus manifest and ROB-846's
    experiment registry use: floats hash via ``float.hex()`` (1-ULP
    sensitive), dict keys sort canonically (container-permutation invariant).
    """
    payload = {
        "config_id": config_id,
        "family": family,
        # `dict(...)` so a caller re-hashing a `ConfigSpec.params` (a
        # `MappingProxyType` since ROB-1060 H2-lock item 7) still produces a
        # JSON-native payload the canonical AST authority accepts.
        "params": dict(params),
    }
    return canonical_hash.canonical_sha256(payload)


def _config(config_id: str, family: str, params: dict[str, Any]) -> ConfigSpec:
    return ConfigSpec(
        config_id=config_id,
        family=family,
        params=params,
        canonical_hash=canonical_config_hash(config_id, family, params),
    )


def build_ap_a1_configs() -> tuple[ConfigSpec, ...]:
    """The 8 AP-A1 DATS configs: full Cartesian product f x s x m, canonical
    nested-loop order (f outer, s middle, m inner), index = f_idx*4 + s_idx*2
    + m_idx."""
    configs = []
    idx = 0
    for f in _AP_A1_F:
        for s in _AP_A1_S:
            for m in _AP_A1_M:
                configs.append(
                    _config(
                        f"AP-A1-{idx:02d}",
                        "AP-A1",
                        {"f": f, "s": s, "m": m, "threshold": AP_A1_FIXED_THRESHOLD},
                    )
                )
                idx += 1
    assert idx == 8
    return tuple(configs)


def build_ap_a2_configs() -> tuple[ConfigSpec, ...]:
    """The 8 AP-A2 WCM-B configs: full Cartesian product L x k x b, canonical
    nested-loop order (L outer, k middle, b inner), index = L_idx*4 + k_idx*2
    + b_idx."""
    configs = []
    idx = 0
    for ell in _AP_A2_L:
        for k in _AP_A2_K:
            for b in _AP_A2_B:
                configs.append(
                    _config(
                        f"AP-A2-{idx:02d}",
                        "AP-A2",
                        {
                            "L": ell,
                            "k": k,
                            "b": b,
                            "positive_filter": AP_A2_FIXED_POSITIVE_FILTER,
                        },
                    )
                )
                idx += 1
    assert idx == 8
    return tuple(configs)


def build_all_configs() -> tuple[ConfigSpec, ...]:
    """The full, EXACTLY-16 sealed config domain. Takes no parameters -- no
    caller-reachable way to request a 17th slot or a 3rd family."""
    all_configs = build_ap_a1_configs() + build_ap_a2_configs()
    validate_config_domain(all_configs)
    return all_configs


def validate_config_domain(configs: tuple[ConfigSpec, ...]) -> None:
    """Fail closed unless ``configs`` is EXACTLY the 16 canonical rows: right
    count, unique config_id, unique canonical_hash. Used both by
    ``build_all_configs`` itself and by any downstream consumer (the sealed
    artifact, the registration CLI) re-validating an already-built domain
    before trusting it."""
    if len(configs) != TOTAL_CONFIGS:
        raise ConfigCountError(
            f"expected exactly {TOTAL_CONFIGS} configs, got {len(configs)}"
        )
    ids = [c.config_id for c in configs]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise DuplicateConfigError(f"duplicate config_id(s): {dupes}")
    hashes = [c.canonical_hash for c in configs]
    if len(set(hashes)) != len(hashes):
        raise DuplicateConfigError("duplicate canonical_hash across distinct configs")


def assert_valid_supersedes(*, child: ConfigSpec, parent: ConfigSpec) -> None:
    """ROB-846 ``SupersedesStrategyMismatch`` lineage rule (AC5): supersedes
    is only valid within the SAME family (AP-A1 configs may only supersede
    AP-A1 configs; AP-A2 likewise). Cross-family supersession is a terminal
    error, not a warning."""
    if child.family != parent.family:
        raise CrossFamilySupersedesError(
            f"{child.config_id} ({child.family}) cannot supersede "
            f"{parent.config_id} ({parent.family}) — supersedes lineage is "
            "confined to the same strategy family"
        )
