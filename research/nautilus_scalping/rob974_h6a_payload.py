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
    "D13_A_CAMPAIGN_DECISION_POLICY",
    "EMPTY_SOURCE_PINS",
    "RUN_ID_PREFIX",
    "CampaignDecisionPolicy",
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


_CAMPAIGN_DECISION_BRANCH_ORDER: tuple[str, str, str, str, str] = (
    "accounting_or_strategy_incomplete",
    "both_fail",
    "s3_only_pass",
    "s4_only_pass",
    "both_pass",
)
_DIRECT_VERDICT_DOMAIN: tuple[str, str, str] = (
    "historical_pass",
    "historical_fail",
    "incomplete",
)
_S4_PAIR_EXECUTOR_GATE_STATES: tuple[str, str, str] = ("not_evaluated", "pass", "fail")


@dataclass(frozen=True)
class CampaignDecisionPolicy:
    """R1 blocker #6 -- typed, closed commitment to the approved D13=A
    campaign-decision table (``06-h5.md`` AC40-46). H6-A never COMPUTES a
    campaign verdict (that is H5's own job) -- but the campaign identity
    must commit to the DECISION CONTRACT H5 is required to follow, so a
    future semantics change (a different orch-approved D-ruling
    superseding D13=A) is detectable as a ``full_campaign_hash`` drift
    rather than silently invisible to H6-A's identity.

    The four S4 safety-invariant fields (``s4_pair_executor_gate_states``/
    ``s4_historical_lineage_permitted_gate_state``/
    ``s4_demo_eligible_default``/``s4_full_promotion_conjunction``) are
    hard-pinned in ``__post_init__`` -- they mirror
    ``rob974_h6a_evidence.HistoricalExecutorState``'s per-attempt
    invariants at the CAMPAIGN-policy level and are never caller-relaxable.
    The remaining fields (branch labels/ranking order) are shape/type
    validated but injectable, so a genuine future policy revision remains
    representable (and its drift detectable) without a code change here.
    """

    policy_version: str
    direct_verdict_domain: tuple[str, str, str]
    branch_order: tuple[str, str, str, str, str]
    incomplete_result: str
    both_fail_result: str
    s3_only_pass_result: str
    s4_only_pass_result: str
    both_pass_result: str
    both_pass_ranking_order: tuple[str, ...]
    s4_pair_executor_gate_states: tuple[str, str, str]
    s4_historical_lineage_permitted_gate_state: str
    s4_demo_eligible_default: bool
    s4_promotion_blocked_reason: str
    s4_full_promotion_conjunction: str

    def __post_init__(self) -> None:
        if type(self.policy_version) is not str or not self.policy_version:
            raise H6APayloadError("policy_version must be a non-empty str")
        if set(self.direct_verdict_domain) != set(_DIRECT_VERDICT_DOMAIN):
            raise H6APayloadError(
                f"direct_verdict_domain must be exactly {set(_DIRECT_VERDICT_DOMAIN)}"
            )
        if tuple(self.branch_order) != _CAMPAIGN_DECISION_BRANCH_ORDER:
            raise H6APayloadError(
                f"branch_order must be exactly {_CAMPAIGN_DECISION_BRANCH_ORDER}"
            )
        for name in (
            "incomplete_result",
            "both_fail_result",
            "s3_only_pass_result",
            "s4_only_pass_result",
            "both_pass_result",
            "s4_promotion_blocked_reason",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise H6APayloadError(f"{name} must be a non-empty str")
        if type(self.both_pass_ranking_order) is not tuple or not all(
            type(item) is str for item in self.both_pass_ranking_order
        ):
            raise H6APayloadError("both_pass_ranking_order must be a tuple of str")
        # Hard-pinned S4 safety invariants -- never caller-relaxable (mirrors
        # HistoricalExecutorState's per-attempt posture at the policy level).
        if set(self.s4_pair_executor_gate_states) != set(_S4_PAIR_EXECUTOR_GATE_STATES):
            raise H6APayloadError(
                f"s4_pair_executor_gate_states must be exactly "
                f"{set(_S4_PAIR_EXECUTOR_GATE_STATES)}"
            )
        if self.s4_historical_lineage_permitted_gate_state != "not_evaluated":
            raise H6APayloadError(
                "s4_historical_lineage_permitted_gate_state must be exactly "
                "'not_evaluated' -- historical S4 never observes pass/fail"
            )
        if (
            type(self.s4_demo_eligible_default) is not bool
            or self.s4_demo_eligible_default
        ):
            raise H6APayloadError(
                "s4_demo_eligible_default must be exactly False -- even a historical "
                "PASS remains promotion_blocked_pending_pair_executor"
            )
        if self.s4_full_promotion_conjunction != "not_evaluated":
            raise H6APayloadError(
                "s4_full_promotion_conjunction must be exactly 'not_evaluated' -- "
                "PAIR_EXEC_FAIL=0 is never observed, so full promotion is never true"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "direct_verdict_domain": list(self.direct_verdict_domain),
            "branch_order": list(self.branch_order),
            "incomplete_result": self.incomplete_result,
            "both_fail_result": self.both_fail_result,
            "s3_only_pass_result": self.s3_only_pass_result,
            "s4_only_pass_result": self.s4_only_pass_result,
            "both_pass_result": self.both_pass_result,
            "both_pass_ranking_order": list(self.both_pass_ranking_order),
            "s4_pair_executor_gate_states": list(self.s4_pair_executor_gate_states),
            "s4_historical_lineage_permitted_gate_state": (
                self.s4_historical_lineage_permitted_gate_state
            ),
            "s4_demo_eligible_default": self.s4_demo_eligible_default,
            "s4_promotion_blocked_reason": self.s4_promotion_blocked_reason,
            "s4_full_promotion_conjunction": self.s4_full_promotion_conjunction,
        }


D13_A_CAMPAIGN_DECISION_POLICY = CampaignDecisionPolicy(
    policy_version="d13_a.v1",
    direct_verdict_domain=_DIRECT_VERDICT_DOMAIN,
    branch_order=_CAMPAIGN_DECISION_BRANCH_ORDER,
    incomplete_result="campaign_incomplete",
    both_fail_result="historical_fail_no_candidate",
    s3_only_pass_result="historical_pass_s3_preferred_demo_handoff",
    s4_only_pass_result="historical_pass_s4_preferred_no_demo_candidate",
    both_pass_result="historical_pass_s3_demo_candidate_s4_comparison_report_only",
    both_pass_ranking_order=(
        "higher_min_fold_e17",
        "higher_pooled_e17",
        "lower_monthly_concentration",
        "lower_timeout",
        "lower_operational_complexity",
    ),
    s4_pair_executor_gate_states=_S4_PAIR_EXECUTOR_GATE_STATES,
    s4_historical_lineage_permitted_gate_state="not_evaluated",
    s4_demo_eligible_default=False,
    s4_promotion_blocked_reason="promotion_blocked_pending_pair_executor",
    s4_full_promotion_conjunction="not_evaluated",
)


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
    campaign_decision_policy: CampaignDecisionPolicy = D13_A_CAMPAIGN_DECISION_POLICY

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
            "campaign_decision_policy": self.campaign_decision_policy.as_dict(),
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
    """The one top-level canonical envelope. ``mode`` alone is call-context
    metadata (a fixture-vs-production distinguisher) and is NOT hashed --
    two envelopes built from identical row/policy/corpus/source-pin content
    hash identically regardless of mode. ``source_pins`` VALUES, however,
    ARE semantic identity content and ARE committed to
    ``full_campaign_hash`` (R1 blocker #1) -- a different H1 feature/H2
    engine/H4 runner/PBO implementation source must never collide on the
    same campaign/run identity.
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
                    # spec.components is itself a frozen (MappingProxyType/
                    # tuple) snapshot since rob974_h6a_identity's own R1
                    # immutable-seal fix -- unfreeze to canonical-hash-safe
                    # built-ins here, at the one point that needs them.
                    "components": _unfreeze(spec.components),
                }
                for spec in self.row_specs
            ],
            "parent_corpus": _unfreeze(self.parent_corpus_frozen),
            "campaign_policy": _unfreeze(self.campaign_policy_frozen),
            # R1 blocker #1: required source pins (H1 feature/H2 engine/H4
            # runner/PBO implementation) MUST be committed to identity --
            # two envelopes with different runner/PBO source but otherwise
            # identical content must never collide on full_campaign_hash.
            # `mode` alone remains metadata (see TestModeIsMetadataNotSemantic
            # Identity); the PIN VALUES are semantic content.
            "source_pins": self.source_pins.as_dict(),
        }
        return canonical_sha256(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rows": [
                {
                    "row_id": spec.row_id,
                    "experiment_id": spec.experiment_id,
                    "components": _unfreeze(spec.components),
                }
                for spec in self.row_specs
            ],
            "parent_corpus": _unfreeze(self.parent_corpus_frozen),
            "campaign_policy": _unfreeze(self.campaign_policy_frozen),
            "source_pins": self.source_pins.as_dict(),
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
