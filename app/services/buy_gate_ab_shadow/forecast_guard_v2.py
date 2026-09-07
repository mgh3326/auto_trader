"""Server-side integrity guard for ROB-1351 v2 forecast targets.

The evaluator/tag builder is intentionally pure and observation-only.  This
module protects the separate ``forecast_save`` persistence boundary so callers
cannot bypass those builder-side constraints with a hand-crafted payload.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Final

from app.services.buy_gate_ab_shadow.epoch_v2 import assert_v2_seal
from app.services.buy_gate_ab_shadow.policy_alignment_v2 import (
    PolicyAlignmentV2Error,
    assert_v2_policy_alignment,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
)


class ForecastGuardV2Error(ValueError):
    """A purported ROB-1351 v2 forecast violates its sealed contract."""


_REQUIRED_STRING_FIELDS: Final[tuple[str, ...]] = (
    "evaluation_as_of",
    "session_date",
    "entry_price",
    "scoring_authority",
    "input_snapshot_sha256",
)
_EXACT_HASH_FIELDS: Final[dict[str, str]] = {
    "spec_sha256": PINNED_SPEC_SHA256_V2,
    "policy_projection_sha256": PINNED_POLICY_PROJECTION_SHA256_V2,
}
_ALLOWED_SHARED_GATE_BITS: Final[frozenset[str]] = frozenset(
    {"supplied", "unavailable_at_this_call_site"}
)
_ALLOWED_INSTRUMENT_TYPES: Final[frozenset[str]] = frozenset({"equity_kr", "equity_us"})


def is_rob1351_v2_target(forecast_target: dict[str, Any]) -> bool:
    """Identify v2 solely by its experiment ID, never by a cohort/fact bit."""

    return forecast_target.get("experiment_id") == EXPERIMENT_ID_V2


def _required_nonempty_string(forecast_target: dict[str, Any], field: str) -> str:
    value = forecast_target.get(field)
    if not isinstance(value, str) or not value:
        raise ForecastGuardV2Error(f"ROB-1351 v2 forecast requires {field}")
    return value


def _canonical_input_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Use the exact v2 evaluator serialization for the input snapshot digest."""

    try:
        payload = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast input_snapshot is not canonically serializable"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _validate_v2_seal_and_live_policy() -> None:
    try:
        assert_v2_seal()
    except Exception as exc:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 sealed registration does not match its pin"
        ) from exc
    try:
        assert_v2_policy_alignment()
    except PolicyAlignmentV2Error as exc:
        raise ForecastGuardV2Error(str(exc)) from exc
    except Exception as exc:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 live policy alignment could not be verified"
        ) from exc


def validate_v2_forecast_target(
    forecast_target: dict[str, Any], *, instrument_type: str
) -> None:
    """Validate a v2 target at the server boundary; ignore non-v2 targets."""

    if not is_rob1351_v2_target(forecast_target):
        return

    _validate_v2_seal_and_live_policy()

    if forecast_target.get("promote") is not False:
        raise ForecastGuardV2Error("ROB-1351 v2 forecast must set promote=false")
    if forecast_target.get("calibration_eligibility") != "calibration_exclude":
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast must set calibration_eligibility=calibration_exclude"
        )
    if (
        forecast_target.get("trade_performance_eligibility")
        != "trade_performance_exclude"
    ):
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast must set "
            "trade_performance_eligibility=trade_performance_exclude"
        )
    if forecast_target.get("cohort") != "shadow_buy":
        raise ForecastGuardV2Error("ROB-1351 v2 forecast must set cohort=shadow_buy")
    if forecast_target.get("live_gate_impact") is not False:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast must set live_gate_impact=false"
        )
    if forecast_target.get("variant") != "B":
        raise ForecastGuardV2Error("ROB-1351 v2 forecast must set variant=B")

    for field, expected in _EXACT_HASH_FIELDS.items():
        if forecast_target.get(field) != expected:
            raise ForecastGuardV2Error(f"ROB-1351 v2 forecast has mismatched {field}")

    for field in _REQUIRED_STRING_FIELDS:
        _required_nonempty_string(forecast_target, field)

    snapshot = forecast_target.get("input_snapshot")
    if not isinstance(snapshot, dict):
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast requires input_snapshot as an object"
        )
    if (
        _canonical_input_snapshot_sha256(snapshot)
        != forecast_target["input_snapshot_sha256"]
    ):
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast has mismatched input_snapshot_sha256"
        )

    try:
        evaluation_as_of = datetime.fromisoformat(forecast_target["evaluation_as_of"])
    except (TypeError, ValueError) as exc:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast evaluation_as_of must be ISO-8601"
        ) from exc
    if evaluation_as_of.tzinfo is None or evaluation_as_of.utcoffset() is None:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast evaluation_as_of must be timezone-aware"
        )
    if forecast_target["session_date"] != evaluation_as_of.date().isoformat():
        raise ForecastGuardV2Error("ROB-1351 v2 forecast has mismatched session_date")

    if forecast_target.get("collection_epoch_id") is not None:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast must not set collection_epoch_id before arm"
        )
    if forecast_target.get("pre_arming_witness") is not True:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast must set pre_arming_witness=true"
        )

    experiment_sample = forecast_target.get("experiment_sample")
    if type(experiment_sample) is not bool:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast experiment_sample must be an exact bool"
        )
    shared_gate_bits = forecast_target.get("shared_gate_bits")
    if (
        type(shared_gate_bits) is not str
        or shared_gate_bits not in _ALLOWED_SHARED_GATE_BITS
    ):
        raise ForecastGuardV2Error("ROB-1351 v2 forecast shared_gate_bits is invalid")
    if (experiment_sample is True) != (shared_gate_bits == "supplied"):
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast experiment_sample and shared_gate_bits disagree"
        )

    if instrument_type not in _ALLOWED_INSTRUMENT_TYPES:
        raise ForecastGuardV2Error(
            "ROB-1351 v2 forecast requires instrument_type equity_kr or equity_us"
        )


__all__ = [
    "ForecastGuardV2Error",
    "is_rob1351_v2_target",
    "validate_v2_forecast_target",
]
