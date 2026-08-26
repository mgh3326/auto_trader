"""Safe, durable audit projections for auto-approve demotions.

The auto-approve classifier works with proposal free text so it can fail
closed.  That text must never become a reason payload: operators need the
reason code and a reproducible match location, not a copied thesis or other
unbounded proposal input.  This module owns the narrow JSONB shape shared by
the writer, Telegram card, and read-only MCP projection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

AUTO_APPROVE_REJECTIONS_KEY = "auto_approve_rejections"
AUTO_APPROVE_CAP_OBSERVATIONS_KEY = "cap_observations"

_MAX_ATTEMPTS = 8
_MAX_RUNGS_PER_ATTEMPT = 16
_MAX_TAG_MATCHES_PER_RUNG = 12
_MAX_CAP_OBSERVATIONS = 16
# This is an audit retention vocabulary, intentionally not the classifier's
# approval-blocking set. §156차 removed ``table_disagreement`` from eligibility
# only; retaining it here preserves safe projections of historical rows and
# externally supplied stored audit rows rather than silently dropping evidence.
# The classifier no longer creates fresh ``table_disagreement`` matches.
_ALLOWED_TAGS = frozenset({"policy_deviation", "table_disagreement"})
_ALLOWED_TAG_FIELDS = frozenset(
    {
        "rationale",
        "source_asof",
        "lot_context",
        "thesis",
        "strategy",
        "exit_reason",
        "void_reason",
    }
)
_ALLOWED_MATCH_KINDS = frozenset(
    {"text", "json_key", "json_value", "serialized", "scan_unavailable"}
)
_NUMERIC_INPUT_KEYS = frozenset(
    {
        "notional",
        "per_order_cap",
        "daily_notional_after",
        "daily_cap",
        "current_price",
        "limit_price",
        "quantity",
        "threshold",
        "distance_pct",
        "min_distance_pct",
        "avg_buy_price",
        "breakeven_band_pct",
        "round_trip_cost_bps",
        "distance_from_avg",
        "gross_pnl",
        "round_trip_cost",
        "net_pnl",
        "pending_rung_count",
    }
)
_ENUM_INPUT_VALUES = {
    "action": frozenset({"place", "replace", "cancel", "unrecognized"}),
    "order_type": frozenset({"limit", "market", "unrecognized"}),
    "account_mode": frozenset(
        {"kis_live", "kis_mock", "toss_live", "upbit", "db_simulated", "unrecognized"}
    ),
    "market": frozenset(
        {"equity_kr", "equity_us", "crypto", "forex", "index", "unrecognized"}
    ),
    "side": frozenset({"buy", "sell", "unrecognized"}),
    "preview_success": frozenset({"false", "invalid"}),
    # §141차 -- why a cancel/replace target was unusable to the classifier.
    "target_evidence": frozenset(
        {"order_id_missing", "snapshot_missing", "snapshot_mismatch"}
    ),
}
_BOOLEAN_INPUT_KEYS = frozenset(
    {
        "eligibility_error",
        "exit_intent_present",
        "thesis_present",
        "toss_auto_submission_frozen",
    }
)
_MISSING_INPUT_FIELDS = frozenset({"current_price", "limit_price", "quantity"})
_KNOWN_REASON_CODES = frozenset(
    {
        "account_not_veto_capable",
        # §141차 retired `action_not_place` from the classifier, but this
        # allowlist also decodes rows already durable in `source_asof` from
        # before that change. Dropping it would silently rewrite that history
        # to `invalid_reason_code`, so it stays -- as a read-only legacy code.
        "action_not_place",
        "action_not_supported",
        "approval_required_tag",
        "auto_veto_thesis_missing",
        "breakeven_band",
        "daily_cap_exceeded",
        "distance_below_minimum",
        "eligibility_error",
        "exit_intent_present",
        "expected_pnl_not_positive",
        "loss_cut_intent",
        "marketable_not_resting",
        "multi_rung_requires_approval",
        "order_type_not_limit",
        "per_order_cap_exceeded",
        "preview_guard_failed",
        "price_or_quantity_missing",
        "sell_classification_unavailable",
        "side_not_supported",
        "target_evidence_missing",
        "thesis_required_for_veto_card",
        "toss_auto_submission_frozen",
        "unknown_auto_approve_mode",
    }
)
_DECIMAL_TEXT_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_POLICY_VERSION_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:\.[0-9]{1,4})?")
_POLICY_CONTENT_HASH_PATTERN = re.compile(r"[0-9a-f]{12,64}")
_CAP_OBSERVATION_NUMERIC_KEYS = (
    "daily_cap",
    "daily_notional_before",
    "daily_notional_after",
    "per_order_cap",
    "notional",
)
_SAFE_PATH_SEGMENT = (
    r"(?:\.(?:context|decision|flags|labels|metadata|notes|reason|review|tag|tags)"
    r"|\[(?:0|[1-9][0-9]{0,3})\])"
)
_LOCATION_PATH_PATTERN = re.compile(rf"\$(?:{_SAFE_PATH_SEGMENT}){{0,32}}")


def _safe_reason_code(value: Any) -> str:
    if isinstance(value, str) and value in _KNOWN_REASON_CODES:
        return value
    return "invalid_reason_code"


def _safe_decimal_text(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    return value if _DECIMAL_TEXT_PATTERN.fullmatch(value) else None


def _safe_policy_version(value: Any) -> str | None:
    if not isinstance(value, str) or not _POLICY_VERSION_PATTERN.fullmatch(value):
        return None
    return value


def _safe_policy_content_hash(value: Any) -> str | None:
    if not isinstance(value, str) or not _POLICY_CONTENT_HASH_PATTERN.fullmatch(value):
        return None
    return value


def _safe_evaluated_at(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def _safe_mode(value: Any) -> str | None:
    if value in {"off", "expanded"}:
        return str(value)
    if value is not None:
        return "unrecognized"
    return None


def _safe_rung_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < 10000 else None


def _safe_enum_input(key: str, value: Any) -> str | None:
    return (
        value if isinstance(value, str) and value in _ENUM_INPUT_VALUES[key] else None
    )


def _safe_missing_inputs(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [field for field in value if field in _MISSING_INPUT_FIELDS]


def _safe_tag_matches(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    matches: list[dict[str, Any]] = []
    for raw in value[:_MAX_TAG_MATCHES_PER_RUNG]:
        if not isinstance(raw, Mapping):
            continue
        token = raw.get("token")
        field = raw.get("field")
        path = raw.get("path")
        kind = raw.get("kind")
        char_start = raw.get("char_start")
        if token not in _ALLOWED_TAGS or field not in _ALLOWED_TAG_FIELDS:
            continue
        if (
            not isinstance(path, str)
            or len(path) > 192
            or not _LOCATION_PATH_PATTERN.fullmatch(path)
        ):
            continue
        if kind not in _ALLOWED_MATCH_KINDS:
            continue
        if isinstance(char_start, bool) or not isinstance(char_start, int):
            continue
        if not 0 <= char_start < 1_000_000:
            continue
        matches.append(
            {
                "token": token,
                "field": field,
                "path": path,
                "kind": kind,
                "char_start": char_start,
            }
        )
    return matches


def _safe_tags(value: Any, matches: list[dict[str, Any]]) -> list[str]:
    tags = {match["token"] for match in matches}
    if isinstance(value, str):
        tags.update(token for token in value.split(",") if token in _ALLOWED_TAGS)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        tags.update(token for token in value if token in _ALLOWED_TAGS)
    return sorted(tags)


def _safe_inputs_from_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    policy_version = _safe_policy_version(decision.get("policy_version"))
    if policy_version is not None:
        inputs["policy_version"] = policy_version
    mode = _safe_mode(decision.get("mode"))
    if mode is not None:
        inputs["mode"] = mode
    for key in sorted(_ENUM_INPUT_VALUES):
        value = _safe_enum_input(key, decision.get(key))
        if value is not None:
            inputs[key] = value
    for key in sorted(_BOOLEAN_INPUT_KEYS):
        if decision.get(key) is True or decision.get(key) is False:
            inputs[key] = decision[key]
    missing_inputs = _safe_missing_inputs(decision.get("missing_inputs"))
    if missing_inputs:
        inputs["missing_inputs"] = missing_inputs
    for key in sorted(_NUMERIC_INPUT_KEYS):
        value = _safe_decimal_text(decision.get(key))
        if value is not None:
            inputs[key] = value
    matches = _safe_tag_matches(decision.get("tag_matches"))
    tags = _safe_tags(decision.get("tags"), matches)
    if tags:
        inputs["tags"] = tags
    if matches:
        inputs["tag_matches"] = matches
    return inputs


def _safe_inputs_from_stored(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    # Stored attempts use the same safe keys as a freshly flattened decision.
    return _safe_inputs_from_decision(value)


def _safe_rung_from_decision(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("eligible") is not False:
        return None
    rung_index = _safe_rung_index(value.get("rung_index"))
    if rung_index is None:
        return None
    return {
        "rung_index": rung_index,
        "reason_code": _safe_reason_code(value.get("reason")),
        "inputs": _safe_inputs_from_decision(value),
    }


def _safe_rung_from_stored(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    rung_index = _safe_rung_index(value.get("rung_index"))
    if rung_index is None:
        return None
    return {
        "rung_index": rung_index,
        "reason_code": _safe_reason_code(value.get("reason_code")),
        "inputs": _safe_inputs_from_stored(value.get("inputs")),
    }


def build_auto_approve_rejection_attempt(
    *, decisions: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, Any] | None:
    """Project only rejected decisions into a bounded, text-free audit event."""
    rungs = [
        safe
        for decision in decisions[:_MAX_RUNGS_PER_ATTEMPT]
        if (safe := _safe_rung_from_decision(decision)) is not None
    ]
    if not rungs:
        return None
    attempt: dict[str, Any] = {
        "evaluated_at": now.isoformat(),
        "rungs": rungs,
    }
    policy_version = rungs[0]["inputs"].get("policy_version")
    if policy_version is not None:
        attempt["policy_version"] = policy_version
    return attempt


def _safe_attempt(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    evaluated_at = _safe_evaluated_at(value.get("evaluated_at"))
    raw_rungs = value.get("rungs")
    if evaluated_at is None or not isinstance(raw_rungs, Sequence):
        return None
    rungs = [
        safe
        for raw in raw_rungs[:_MAX_RUNGS_PER_ATTEMPT]
        if (safe := _safe_rung_from_stored(raw)) is not None
    ]
    if not rungs:
        return None
    attempt: dict[str, Any] = {"evaluated_at": evaluated_at, "rungs": rungs}
    policy_version = _safe_policy_version(value.get("policy_version"))
    if policy_version is not None:
        attempt["policy_version"] = policy_version
    return attempt


def project_auto_approve_rejections(source_asof: Any) -> list[dict[str, Any]]:
    """Return the safe public projection; never return arbitrary JSONB keys."""
    if not isinstance(source_asof, Mapping):
        return []
    raw_attempts = source_asof.get(AUTO_APPROVE_REJECTIONS_KEY)
    if not isinstance(raw_attempts, Sequence) or isinstance(raw_attempts, (str, bytes)):
        return []
    return [
        safe
        for raw in raw_attempts[-_MAX_ATTEMPTS:]
        if (safe := _safe_attempt(raw)) is not None
    ]


def append_auto_approve_rejection_attempt(
    source_asof: Any, *, decisions: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, Any]:
    """Append one safe rejection attempt while preserving unrelated provenance."""
    attempt = build_auto_approve_rejection_attempt(decisions=decisions, now=now)
    source = dict(source_asof) if isinstance(source_asof, Mapping) else {}
    if attempt is None:
        return source
    source[AUTO_APPROVE_REJECTIONS_KEY] = [
        *project_auto_approve_rejections(source),
        attempt,
    ][-_MAX_ATTEMPTS:]
    return source


def _safe_cap_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    rung_index = _safe_rung_index(value.get("rung_index"))
    policy_version = _safe_policy_version(value.get("policy_version"))
    content_hash = _safe_policy_content_hash(value.get("content_hash"))
    evaluated_at = _safe_evaluated_at(value.get("evaluated_at"))
    numeric_values = {
        key: _safe_decimal_text(value.get(key)) for key in _CAP_OBSERVATION_NUMERIC_KEYS
    }
    if (
        rung_index is None
        or policy_version is None
        or content_hash is None
        or evaluated_at is None
        or any(item is None for item in numeric_values.values())
    ):
        return None
    return {
        "rung_index": rung_index,
        **numeric_values,
        "policy_version": policy_version,
        "content_hash": content_hash,
        "evaluated_at": evaluated_at,
    }


def build_auto_approve_cap_observations(
    *,
    decisions: Sequence[Mapping[str, Any]],
    policy_content_hash: str | None,
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    """Project complete, bounded cap evidence for eligible rungs only."""
    observations: list[dict[str, Any]] = []
    evaluated_at_text = evaluated_at.isoformat()
    for decision in decisions[:_MAX_CAP_OBSERVATIONS]:
        if decision.get("eligible") is not True:
            continue
        candidate = {
            "rung_index": decision.get("rung_index"),
            **{key: decision.get(key) for key in _CAP_OBSERVATION_NUMERIC_KEYS},
            "policy_version": decision.get("policy_version"),
            "content_hash": policy_content_hash,
            "evaluated_at": evaluated_at_text,
        }
        if (safe := _safe_cap_observation(candidate)) is not None:
            observations.append(safe)
    return observations


def project_auto_approve_cap_observations(source_asof: Any) -> list[dict[str, Any]]:
    """Return only complete, safe cap evidence from an auto-approved proposal."""
    if not isinstance(source_asof, Mapping):
        return []
    auto_approved = source_asof.get("auto_approved")
    if not isinstance(auto_approved, Mapping):
        return []
    raw_observations = auto_approved.get(AUTO_APPROVE_CAP_OBSERVATIONS_KEY)
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes)
    ):
        return []
    return [
        safe
        for raw in raw_observations[:_MAX_CAP_OBSERVATIONS]
        if (safe := _safe_cap_observation(raw)) is not None
    ]


def build_auto_approve_rejection_card_block(source_asof: Any) -> str | None:
    """Render a bounded safe summary for the existing manual approval card."""
    attempts = project_auto_approve_rejections(source_asof)
    if not attempts:
        return None
    latest = attempts[-1]
    lines = ["*자동 승인 제외*"]
    for rung in latest["rungs"][:3]:
        lines.append(f"- #{rung['rung_index'] + 1}: `{rung['reason_code']}`")
        matches = rung["inputs"].get("tag_matches", [])
        for match in matches[:2]:
            location = match["field"] + match["path"][1:]
            lines.append(
                f"  - 태그 `{match['token']}` / 위치 `{location}` "
                f"(문자 {match['char_start']})"
            )
    if len(latest["rungs"]) > 3:
        lines.append("- 추가 제외 사유는 제안 조회에서 확인")
    return "\n".join(lines)


__all__ = [
    "AUTO_APPROVE_CAP_OBSERVATIONS_KEY",
    "AUTO_APPROVE_REJECTIONS_KEY",
    "append_auto_approve_rejection_attempt",
    "build_auto_approve_cap_observations",
    "build_auto_approve_rejection_attempt",
    "build_auto_approve_rejection_card_block",
    "project_auto_approve_cap_observations",
    "project_auto_approve_rejections",
]
