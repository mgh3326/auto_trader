"""ROB-981 (ROB-974 R2 H6-A) CP2 -- canonical campaign payload, plan purity,
and immutable seals.

Builds ONE canonical envelope over the 48 row specs (``rob974_h6a_identity``)
plus campaign-wide policy (folds/embargo/horizons/selection authority/
base13-primary17-upward22 path membership/funding policy/gates+bins/
full-window PBO contract/S4 pair order+tri-state), and derives a
deterministic ``full_campaign_hash`` + primary ``campaign_run_id`` that are
independent of path, wall clock, env order, diagnostics, and DB/registration
IDs.

Production mode (``mode="production_plan"``) additionally requires a CLOSED,
non-placeholder source-pin object (H1 feature source, H2 engine source, H4
runner source, PBO implementation source) before an envelope can even be
constructed -- this module never derives a "production" full-campaign hash
from missing/zero/placeholder pins, but it also never claims the pins it IS
given are real production lineage; that determination belongs entirely to
the caller (CP8's H2/H3 adapter, and eventually H4's own source pins).

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
shared ``research_contracts.canonical_hash`` authority and the sibling
``rob974_h6a_identity`` module.
"""

from __future__ import annotations

import base64
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import rob974_h6a_identity as identity

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "EMPTY_SOURCE_PINS",
    "RUN_ID_PREFIX",
    "CampaignPolicy",
    "H6ACampaignEnvelope",
    "H6APayloadError",
    "MissingSourcePinError",
    "PlanMode",
    "RequiredSourcePins",
    "RunIdDerivationError",
    "build_campaign_envelope",
    "derive_primary_run_id",
    "verify_primary_run_id",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_HEX = "0" * 64
RUN_ID_PREFIX = "rob974h6a-"

PlanMode = Literal["fixture_plan", "production_plan"]


class H6APayloadError(ValueError):
    """Base error for the ROB-981 H6-A campaign payload builder."""


class MissingSourcePinError(H6APayloadError):
    """``mode="production_plan"`` was requested but a required source pin is
    missing/None, the all-zero placeholder, or not a well-formed lowercase
    64-hex digest -- refused BEFORE any identity derivation."""


class RunIdDerivationError(H6APayloadError):
    """A caller-supplied ``campaign_run_id`` is not the value canonically
    derived from ``full_campaign_hash`` -- an arbitrary UUID/timestamp/
    operator typo is refused."""


def _freeze(obj: Any) -> Any:
    """Recursively converts dict/list into an immutable structure
    (``types.MappingProxyType`` + ``tuple``); mirrors
    ``rob944_frozen_campaign._freeze``/``rob945_accounting_seal._deep_freeze``.
    Performs a full deep, non-aliasing copy -- a caller's own
    pre-construction dict can never leak into (or later mutate) the sealed
    envelope, and the returned structure shares no mutable node with it."""
    if isinstance(obj, Mapping):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list | tuple):
        return tuple(_freeze(v) for v in obj)
    return obj


def _unfreeze(obj: Any) -> Any:
    if isinstance(obj, types.MappingProxyType):
        return {k: _unfreeze(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_unfreeze(v) for v in obj]
    return obj


@dataclass(frozen=True)
class CampaignPolicy:
    """Campaign-wide policy shared by all 48 rows -- every field here is a
    distinct identity component; callers exercise real content (CP8's H3
    adapter) or fixture content (this checkpoint), never a hardcoded
    real-looking literal invented by this module itself."""

    folds: tuple[Any, ...]
    embargo_hours: int
    horizons: Mapping[str, Any]
    selection_authority: str
    path_membership: Mapping[str, Any]  # keys: base13/primary_stress17/upward_stress22
    funding_policy: Mapping[str, Any]
    gates_bins: Mapping[str, Any]
    pbo_contract: Mapping[
        str, Any
    ]  # full-window PBO primary_stress@17, 24x365, slices=4
    pair_order: tuple[str, ...]  # exactly ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
    s4_tri_state_policy: str  # S4 historical-only / PAIR_EXEC tri-state disclosure

    def as_dict(self) -> dict[str, Any]:
        return {
            "folds": list(self.folds),
            "embargo_hours": self.embargo_hours,
            "horizons": dict(self.horizons),
            "selection_authority": self.selection_authority,
            "path_membership": dict(self.path_membership),
            "funding_policy": dict(self.funding_policy),
            "gates_bins": dict(self.gates_bins),
            "pbo_contract": dict(self.pbo_contract),
            "pair_order": list(self.pair_order),
            "s4_tri_state_policy": self.s4_tri_state_policy,
        }


@dataclass(frozen=True)
class RequiredSourcePins:
    """Closed required source-pin object. ``None`` in every field is a valid
    (non-production) value -- only ``require_production_ready`` enforces
    non-placeholder presence, and only when ``mode="production_plan"``."""

    feature_source_sha256: str | None
    engine_source_sha256: str | None
    runner_source_sha256: str | None
    pbo_implementation_sha256: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "feature_source_sha256": self.feature_source_sha256,
            "engine_source_sha256": self.engine_source_sha256,
            "runner_source_sha256": self.runner_source_sha256,
            "pbo_implementation_sha256": self.pbo_implementation_sha256,
        }

    def require_production_ready(self) -> None:
        for name, value in self.as_dict().items():
            if value is None:
                raise MissingSourcePinError(
                    f"{name} is missing (None) -- required for mode='production_plan'"
                )
            if type(value) is not str or not _HEX64_RE.match(value):
                raise MissingSourcePinError(
                    f"{name} is not a well-formed lowercase 64-hex digest"
                )
            if value == _PLACEHOLDER_HEX:
                raise MissingSourcePinError(
                    f"{name} is the all-zero placeholder -- not a real source pin"
                )


EMPTY_SOURCE_PINS = RequiredSourcePins(
    feature_source_sha256=None,
    engine_source_sha256=None,
    runner_source_sha256=None,
    pbo_implementation_sha256=None,
)


@dataclass(frozen=True)
class H6ACampaignEnvelope:
    """The one top-level canonical envelope. ``mode`` and ``source_pins`` are
    NOT hashed into ``full_campaign_hash`` -- they are call-context metadata
    (a fixture-vs-production distinguisher and an optional readiness
    assertion), never semantic campaign content; two envelopes built from
    identical row/policy/corpus content hash identically regardless of mode.
    """

    schema_version: str
    row_specs: tuple[identity.H6ARowSpec, ...]
    parent_corpus: Mapping[str, Any]
    campaign_policy: CampaignPolicy
    source_pins: RequiredSourcePins
    mode: PlanMode
    campaign_policy_frozen: Mapping[str, Any]
    parent_corpus_frozen: Mapping[str, Any]

    def full_campaign_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "rows": [
                {
                    "row_id": spec.row_id,
                    "experiment_id": spec.experiment_id,
                    "components": spec.components,
                }
                for spec in self.row_specs
            ],
            "parent_corpus": _unfreeze(self.parent_corpus_frozen),
            "campaign_policy": _unfreeze(self.campaign_policy_frozen),
        }
        return canonical_sha256(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rows": [
                {
                    "row_id": spec.row_id,
                    "experiment_id": spec.experiment_id,
                    "components": _unfreeze(_freeze(spec.components)),
                }
                for spec in self.row_specs
            ],
            "parent_corpus": _unfreeze(self.parent_corpus_frozen),
            "campaign_policy": _unfreeze(self.campaign_policy_frozen),
            "mode": self.mode,
        }


SCHEMA_VERSION = "rob974_h6a_campaign_envelope.v1"


def build_campaign_envelope(
    *,
    row_specs: tuple[identity.H6ARowSpec, ...],
    parent_corpus: Mapping[str, Any],
    campaign_policy: CampaignPolicy,
    source_pins: RequiredSourcePins,
    mode: PlanMode,
) -> H6ACampaignEnvelope:
    """Pure builder: no DB/session/query, corpus load, process, network, or
    env/current-time read. Validates row shape/order/component-identity
    (reusing CP1's own kernel, never re-implementing it) BEFORE hashing, and
    -- in ``production_plan`` mode -- validates every source pin is present
    and non-placeholder BEFORE the envelope (and therefore
    ``full_campaign_hash``) can even be constructed.
    """
    identity.assert_specs_in_canonical_order(tuple(row_specs))
    identity.validate_same_strategy_components_identical(row_specs)

    if mode == "production_plan":
        source_pins.require_production_ready()
    elif mode != "fixture_plan":
        raise H6APayloadError(f"unknown plan mode {mode!r}")

    return H6ACampaignEnvelope(
        schema_version=SCHEMA_VERSION,
        row_specs=tuple(row_specs),
        parent_corpus=parent_corpus,
        campaign_policy=campaign_policy,
        source_pins=source_pins,
        mode=mode,
        campaign_policy_frozen=_freeze(campaign_policy.as_dict()),
        parent_corpus_frozen=_freeze(dict(parent_corpus)),
    )


def derive_primary_run_id(full_campaign_hash: str) -> str:
    """Deterministic typed-canonical derivation, mirroring
    ``rob945_accounting_seal.derive_campaign_run_id``'s recipe (SHA-256 of
    ``{full_campaign_hash, kind}`` -> raw 32 bytes -> unpadded URL-safe
    base64 -> fixed prefix) with a distinct ROB-981 prefix/kind so a ROB-974
    run ID can never collide with (or be confused for) a ROB-944 one."""
    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "rob974_h6a_primary_run"}
    )
    raw = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{RUN_ID_PREFIX}{suffix}"


def verify_primary_run_id(campaign_run_id: str, *, full_campaign_hash: str) -> None:
    """Reject a caller-supplied ``campaign_run_id`` (arbitrary UUID/
    timestamp/operator typo) that is not the value canonically derived from
    ``full_campaign_hash`` -- never trust the caller's own claim."""
    expected = derive_primary_run_id(full_campaign_hash)
    if campaign_run_id != expected:
        raise RunIdDerivationError(
            "campaign_run_id is not the value canonically derived from the frozen "
            "full_campaign_hash -- an arbitrary UUID/timestamp/operator typo is refused"
        )
