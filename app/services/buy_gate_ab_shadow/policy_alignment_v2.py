"""Fail-closed ROB-1351 v2 seal-to-live-policy alignment.

The v2 projection is a sealed observation contract, while
``config/trading_policy.yaml`` remains the operator-owned live-policy source.
This module compares only the four live discovery inputs that the sealed v2
projection actually represents.  It deliberately does not assert that every
live-policy key appears in the projection: unrelated additive policy work must
not retroactively alter this observation contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.services import trading_policy_service
from app.services.buy_gate_ab_shadow.spec_v2 import POLICY_PROJECTION_V2


class PolicyAlignmentV2Error(ValueError):
    """The sealed v2 projection cannot be matched to the loaded live policy."""


# Direction is intentionally sealed projection -> live policy only.  These are
# the exact four discovery inputs represented by POLICY_PROJECTION_V2.
SEALED_TO_LIVE_POLICY_KEYS_V2: Final[dict[str, str]] = {
    "variant_a.support_strength_min": "screen.support_strength_min",
    "shared_gates.rsi.threshold": "screen.rsi_max",
    "shared_gates.support_distance_pct.maximum": "screen.support_within_pct",
    "shared_gates.honest_upside_pct.threshold": "screen.upside_min_pct",
}

# Every other leaf below the three checked projection branches is deliberately
# non-live.  Keeping the rationale adjacent to the mapping makes a new sealed
# leaf fail closed until a reviewer classifies it as a live policy input or an
# explicitly non-live experiment/evaluator rule.
SEALED_NO_LIVE_POLICY_REASON_V2: Final[dict[str, str]] = {
    "variant_a.label": "experiment arm structure, not a live policy threshold",
    "variant_a.role": "experiment arm structure, not a live policy threshold",
    "variant_a.executes": "experiment arm structure, not a live policy threshold",
    "variant_b.label": "shadow arm structure, not a live gate",
    "variant_b.role": "shadow arm structure, not a live gate",
    "variant_b.support_strength_min": "shadow arm threshold, not a live gate",
    "variant_b.executes": "shadow arm structure, not a live gate",
    "variant_b.register_as": "shadow stream label, not a live policy threshold",
    "shared_gates.rsi.operator": "sealed evaluator comparison rule",
    "shared_gates.rsi.missing": "sealed evaluator missing-input rule",
    "shared_gates.support_distance_pct.operator": "sealed evaluator comparison rule",
    "shared_gates.support_distance_pct.minimum": "sealed evaluator lower-bound rule",
    "shared_gates.support_distance_pct.missing": "sealed evaluator missing-input rule",
    "shared_gates.honest_upside_pct.operator": "sealed evaluator comparison rule",
    "shared_gates.honest_upside_pct.missing": "sealed evaluator missing-input rule",
    "shared_gates.other_gate_bits.keys": "session review-bit provenance, not a policy key",
    "shared_gates.other_gate_bits.required_value": "session review-bit rule, not a policy key",
    "shared_gates.other_gate_bits.missing_value": "session review-bit rule, not a policy key",
    "shared_gates.other_gate_bits.non_boolean": "session review-bit rule, not a policy key",
}

_PROJECTION_BRANCHES: Final[tuple[str, ...]] = (
    "variant_a",
    "variant_b",
    "shared_gates",
)
_NUMERIC_LIVE_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "screen.rsi_max",
        "screen.support_within_pct",
        "screen.upside_min_pct",
    }
)


def projection_leaf_paths_v2(
    payload: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """Return every leaf below the sealed v2 branches that need classification."""

    projection = POLICY_PROJECTION_V2 if payload is None else payload
    paths: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            if not value:
                paths.add(path)
                return
            for key, child in value.items():
                if not isinstance(key, str) or not key:
                    raise PolicyAlignmentV2Error(
                        "v2 policy projection contains an invalid mapping key"
                    )
                walk(child, f"{path}.{key}")
            return
        # Lists are intentional scalar leaves here.  Treating their element
        # indices as projection fields would make a changed review-bit list
        # look like a new live-policy mapping requirement.
        paths.add(path)

    for branch in _PROJECTION_BRANCHES:
        value = projection.get(branch)
        if not isinstance(value, Mapping):
            raise PolicyAlignmentV2Error(
                f"v2 policy projection requires mapping branch {branch}"
            )
        walk(value, branch)
    return frozenset(paths)


def assert_v2_policy_mapping_coverage() -> None:
    """Fail closed if a sealed leaf lacks a live mapping or stated rationale."""

    leaves = projection_leaf_paths_v2()
    classified = set(SEALED_TO_LIVE_POLICY_KEYS_V2) | set(
        SEALED_NO_LIVE_POLICY_REASON_V2
    )
    missing = sorted(leaves - classified)
    stale = sorted(classified - leaves)
    overlap = sorted(
        set(SEALED_TO_LIVE_POLICY_KEYS_V2).intersection(SEALED_NO_LIVE_POLICY_REASON_V2)
    )
    if missing or stale or overlap:
        detail: list[str] = []
        if missing:
            detail.append(f"unclassified sealed leaves: {', '.join(missing)}")
        if stale:
            detail.append(f"non-leaf classifications: {', '.join(stale)}")
        if overlap:
            detail.append(f"ambiguous classifications: {', '.join(overlap)}")
        raise PolicyAlignmentV2Error(
            "ROB-1351 v2 policy mapping coverage is invalid (" + "; ".join(detail) + ")"
        )


def _projection_value(path: str) -> Any:
    value: Any = POLICY_PROJECTION_V2
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise PolicyAlignmentV2Error(
                f"v2 policy projection is missing sealed field {path}"
            )
        value = value[segment]
    return value


def _loaded_effective_value(
    document: Any,
    *,
    market: str,
    policy_key: str,
) -> Any:
    """Mirror ``get_policy_for(..., 'discovery')`` for the loaded document.

    ``load_trading_policy`` is deliberately read here instead of relying only on
    ``get_policy_for``'s internal cache.  That makes the source boundary
    observable and ensures a loader failure or malformed loaded document is a
    fail-closed server rejection.
    """

    thresholds = getattr(document, "thresholds", None)
    if not isinstance(thresholds, Mapping):
        raise PolicyAlignmentV2Error("loaded trading policy has invalid thresholds")
    threshold = thresholds.get(policy_key)
    if threshold is None:
        raise PolicyAlignmentV2Error(f"loaded trading policy is missing {policy_key}")
    lanes = getattr(threshold, "lanes", None)
    if not isinstance(lanes, list) or "discovery" not in lanes:
        raise PolicyAlignmentV2Error(
            f"loaded trading policy {policy_key} is not a discovery threshold"
        )
    if not hasattr(threshold, "value"):
        raise PolicyAlignmentV2Error(f"loaded trading policy {policy_key} has no value")

    market_overrides = getattr(document, "market_overrides", None)
    if not isinstance(market_overrides, Mapping):
        raise PolicyAlignmentV2Error(
            "loaded trading policy has invalid market_overrides"
        )
    overrides = market_overrides.get(market)
    if not isinstance(overrides, Mapping):
        raise PolicyAlignmentV2Error(
            f"loaded trading policy has invalid {market} market override"
        )
    return overrides.get(policy_key, threshold.value)


def _get_policy_for_effective_value(*, market: str, policy_key: str) -> Any:
    """Read the public discovery projection, including market overrides."""

    try:
        policy = trading_policy_service.get_policy_for(market, "discovery")
    except Exception as exc:
        raise PolicyAlignmentV2Error(
            f"could not load effective {market} discovery policy"
        ) from exc
    if not isinstance(policy, Mapping):
        raise PolicyAlignmentV2Error(
            f"effective {market} discovery policy has invalid shape"
        )
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise PolicyAlignmentV2Error(
            f"effective {market} discovery policy has invalid thresholds"
        )
    threshold = thresholds.get(policy_key)
    if not isinstance(threshold, Mapping) or "value" not in threshold:
        raise PolicyAlignmentV2Error(
            f"effective {market} discovery policy is missing {policy_key}"
        )
    return threshold["value"]


def _normalized_number(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PolicyAlignmentV2Error(f"{field} must be a finite number") from exc
    if not number.is_finite():
        raise PolicyAlignmentV2Error(f"{field} must be a finite number")
    return number


def _normalized_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise PolicyAlignmentV2Error(f"{field} must be a non-empty string")
    normalized = value.strip().lower()
    if not normalized:
        raise PolicyAlignmentV2Error(f"{field} must be a non-empty string")
    return normalized


def _normalized_value(value: Any, *, policy_key: str, field: str) -> Decimal | str:
    if policy_key in _NUMERIC_LIVE_POLICY_KEYS:
        return _normalized_number(value, field=field)
    if policy_key == "screen.support_strength_min":
        return _normalized_text(value, field=field)
    raise PolicyAlignmentV2Error(f"unrecognized v2 live policy key {policy_key}")


def assert_v2_policy_alignment() -> None:
    """Compare sealed v2 gates with live KR/US discovery policy, fail closed.

    The loaded document and ``get_policy_for`` must agree on each effective
    value.  The first read proves the source is the actual policy loader; the
    second preserves the public market-override projection used by consumers.
    """

    assert_v2_policy_mapping_coverage()
    try:
        document = trading_policy_service.load_trading_policy()
    except Exception as exc:
        raise PolicyAlignmentV2Error("could not load trading policy") from exc

    markets = POLICY_PROJECTION_V2.get("markets")
    if not isinstance(markets, list) or tuple(markets) != ("kr", "us"):
        raise PolicyAlignmentV2Error("v2 policy projection markets must be kr and us")

    for market in markets:
        if not isinstance(market, str):
            raise PolicyAlignmentV2Error("v2 policy projection market is invalid")
        for projection_path, policy_key in SEALED_TO_LIVE_POLICY_KEYS_V2.items():
            sealed = _normalized_value(
                _projection_value(projection_path),
                policy_key=policy_key,
                field=f"sealed {projection_path}",
            )
            loaded = _normalized_value(
                _loaded_effective_value(
                    document,
                    market=market,
                    policy_key=policy_key,
                ),
                policy_key=policy_key,
                field=f"loaded {market} {policy_key}",
            )
            effective = _normalized_value(
                _get_policy_for_effective_value(
                    market=market,
                    policy_key=policy_key,
                ),
                policy_key=policy_key,
                field=f"effective {market} {policy_key}",
            )
            if loaded != effective:
                raise PolicyAlignmentV2Error(
                    f"loaded and effective {market} policy disagree for {policy_key}"
                )
            if sealed != loaded:
                raise PolicyAlignmentV2Error(
                    f"sealed {projection_path} does not match {market} {policy_key}"
                )


__all__ = [
    "PolicyAlignmentV2Error",
    "SEALED_NO_LIVE_POLICY_REASON_V2",
    "SEALED_TO_LIVE_POLICY_KEYS_V2",
    "assert_v2_policy_alignment",
    "assert_v2_policy_mapping_coverage",
    "projection_leaf_paths_v2",
]
