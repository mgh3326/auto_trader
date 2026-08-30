"""ROB-402 — watch auto_execute_mock service.

Records an intent (audit) and, when all gates pass, places a kis_mock order.
The executor is hard-pinned is_mock=True; the live-block guard rejects explicit
live/non-mock accounts before any insert. Default off via gate flag.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.review import WatchOrderIntentLedger
from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)

logger = logging.getLogger(__name__)

# Strategy label carried into review.kis_mock_signal_ledger / kis_mock_order_ledger
# so a watch-sourced order is attributable without joining back through the
# watch intent row.
WATCH_AUTO_EXECUTE_STRATEGY = "watch_auto_execute_mock"

_SYNTHETIC_CANARY_CONTRACT = "synthetic-canary-guard/v1"
_SYNTHETIC_CANARY_REQUIRED_FIELDS = frozenset(
    {
        "canary_run_id",
        "pre_registration_digest",
        "mutation_disabled",
        "policy",
        "j7_manifest_ref",
    }
)
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _to_decimal(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _PlaceOutcome:
    executed: bool
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _SyntheticCanaryLayer:
    marker_present: bool
    marker: Any = None
    evaluation: dict[str, Any] | None = None


def _canonical_json_digest(value: Any) -> str | None:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _evaluate_synthetic_canary_layer(
    metadata: Any,
    *,
    marker_matches_source: bool = True,
) -> _SyntheticCanaryLayer:
    """Evaluate the marker-present-only durable data guard.

    ``pre_registration_digest`` is the canonical SHA-256 of the marker with
    that digest field omitted.  This lets the executor detect a marker that
    changed after pre-registration without consulting mutable runtime state.
    """

    if not isinstance(metadata, dict) or "synthetic_canary" not in metadata:
        return _SyntheticCanaryLayer(marker_present=False)

    marker = copy.deepcopy(metadata["synthetic_canary"])
    marker_dict = marker if isinstance(marker, dict) else None
    expected_digest = (
        marker_dict.get("pre_registration_digest") if marker_dict is not None else None
    )
    digest_payload = (
        {
            key: value
            for key, value in marker_dict.items()
            if key != "pre_registration_digest"
        }
        if marker_dict is not None
        else None
    )
    actual_digest = (
        _canonical_json_digest(digest_payload) if digest_payload is not None else None
    )
    digest_match = bool(
        marker_matches_source
        and isinstance(expected_digest, str)
        and _SHA256_HEX_RE.fullmatch(expected_digest)
        and actual_digest is not None
        and hmac.compare_digest(expected_digest, actual_digest)
    )

    policy = marker_dict.get("policy") if marker_dict is not None else None
    identity_valid = bool(
        marker_dict is not None
        and _SYNTHETIC_CANARY_REQUIRED_FIELDS.issubset(marker_dict)
        and isinstance(marker_dict.get("canary_run_id"), str)
        and marker_dict["canary_run_id"].strip()
        and isinstance(marker_dict.get("j7_manifest_ref"), str)
        and marker_dict["j7_manifest_ref"].strip()
        and isinstance(policy, dict)
        and isinstance(policy.get("version"), str)
        and policy["version"].strip()
        and isinstance(policy.get("content_hash"), str)
        and bool(_SHA256_HEX_RE.fullmatch(policy["content_hash"]))
        and marker_dict.get("mutation_disabled") is True
        and digest_match
    )
    code = (
        "synthetic_canary_mutation_disabled"
        if identity_valid
        else "synthetic_canary_identity_invalid"
    )
    return _SyntheticCanaryLayer(
        marker_present=True,
        marker=marker,
        evaluation={
            "layer": "synthetic_data_guard",
            "evaluated": True,
            "result": "block",
            "code": code,
            "marker_digest_match": digest_match,
        },
    )


def _evaluate_env_gate_layer() -> dict[str, Any]:
    enabled = settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED
    return {
        "layer": "env_gate",
        "evaluated": True,
        "result": "allow" if enabled else "block",
        "code": (
            "auto_execute_globally_enabled"
            if enabled
            else "auto_execute_globally_disabled"
        ),
    }


def _normalize_place_result(result: Any) -> _PlaceOutcome:
    """Interpret the order function's normalized result truthfully (ROB-843).

    Executed requires broker acceptance, a non-preview response, a broker order
    id, and explicit durable-tracking availability. ``ledger_id=None`` alone is
    not a failure: ROB-843's benign on-conflict path re-checks the existing
    native row and returns ``ledger_tracking_unavailable=False`` without the id.
    """
    if not isinstance(result, dict):
        return _PlaceOutcome(False, "malformed_result", str(result)[:200])
    detail = (
        result.get("detail") or result.get("response_message") or result.get("message")
    )
    normalized_detail = str(detail) if detail else None

    if result.get("success") is not True:
        reason = result.get("reason") or result.get("status") or "order_failed"
        return _PlaceOutcome(False, str(reason), normalized_detail)
    if result.get("dry_run") is not False:
        reason = (
            "dry_run_result"
            if result.get("dry_run") is True
            else "invalid_dry_run_flag"
        )
        return _PlaceOutcome(False, reason, normalized_detail)

    order_no = result.get("order_no")
    if not isinstance(order_no, str) or not order_no.strip():
        return _PlaceOutcome(False, "missing_broker_order_id", normalized_detail)

    tracking_unavailable = result.get("ledger_tracking_unavailable")
    if tracking_unavailable is not False:
        reason = (
            "ledger_tracking_unavailable"
            if tracking_unavailable is True
            else "invalid_ledger_tracking_flag"
        )
        return _PlaceOutcome(False, reason, normalized_detail)

    # ROB-1140 R1: explicit None is the ROB-843 benign-conflict signal; a
    # missing key is a malformed contract and must not collapse into that case.
    if "ledger_id" not in result:
        return _PlaceOutcome(False, "missing_ledger_id", normalized_detail)

    ledger_id = result["ledger_id"]
    if ledger_id is not None and (
        not isinstance(ledger_id, int) or isinstance(ledger_id, bool) or ledger_id <= 0
    ):
        return _PlaceOutcome(False, "invalid_ledger_id", normalized_detail)

    return _PlaceOutcome(True)


async def _default_place_order_fn(**kwargs):
    # Lazy import to avoid heavy import at module load.
    from app.mcp_server.tooling.order_execution import _place_order_impl

    return await _place_order_impl(**kwargs)


async def maybe_auto_execute(
    db,
    *,
    alert,
    correlation_id: str,
    kst_date: str,
    scanner_snapshot: dict[str, Any] | None = None,
    place_order_fn: Callable[..., Any] = _default_place_order_fn,
) -> dict[str, Any]:
    """Evaluate gates and (if all pass) place a kis_mock order for the alert."""
    if alert.action_mode != "auto_execute_mock":
        return {"executed": False, "skipped": "not_auto_execute_mock"}

    max_action: dict = alert.max_action or {}
    account_mode = max_action.get("account_mode") or "kis_mock"

    # 1) live-block guard (hard reject before any insert).
    try:
        assert_auto_execute_account_allowed("auto_execute_mock", account_mode)
    except AutoExecuteLiveBlocked:
        logger.warning(
            "auto_execute_mock blocked for live account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "live_account"}
    except AutoExecuteUnsupported:
        logger.warning(
            "auto_execute_mock unsupported account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "unsupported_account"}

    # 2) precondition checks (account is kis_mock from here on).  The two
    # policy layers are deliberately called before either result is consumed:
    # a blocking synthetic layer must not short-circuit the env discriminator,
    # nor may an off env gate hide whether the synthetic marker was evaluated.
    alert_metadata = getattr(alert, "alert_metadata", None)
    marker_source = alert_metadata
    marker_matches_source = True
    if (
        scanner_snapshot is not None
        and isinstance(alert_metadata, dict)
        and "synthetic_canary" in alert_metadata
    ):
        if (
            isinstance(scanner_snapshot, dict)
            and "synthetic_canary" in scanner_snapshot
        ):
            marker_source = scanner_snapshot
            marker_matches_source = (
                scanner_snapshot["synthetic_canary"]
                == alert_metadata["synthetic_canary"]
            )
        else:
            # Preserve marker-present semantics even if the preceding event
            # hop lost the marker: this is invalid identity, never a normal
            # markerless production alert.
            marker_source = {"synthetic_canary": None}
            marker_matches_source = False
    synthetic_layer = _evaluate_synthetic_canary_layer(
        marker_source,
        marker_matches_source=marker_matches_source,
    )
    env_layer = _evaluate_env_gate_layer()

    reasons: list[str] = []
    synthetic_evaluation = synthetic_layer.evaluation
    if synthetic_layer.marker_present:
        assert synthetic_evaluation is not None
        reasons.append(synthetic_evaluation["code"])
    if env_layer["result"] == "block":
        reasons.append(env_layer["code"])
    side = max_action.get("side")
    quantity = _to_decimal(max_action.get("quantity"))
    limit_price = _to_decimal(max_action.get("limit_price"))
    if side not in ("buy", "sell"):
        reasons.append("missing_or_invalid_side")
    if quantity is None or quantity <= 0:
        reasons.append("missing_quantity")
    if limit_price is None or limit_price <= 0:
        reasons.append("missing_limit_price")

    allowed = not reasons
    lifecycle = "previewed" if allowed else "failed"
    preview_line = {
        "symbol": alert.symbol,
        "side": side,
        "quantity": str(quantity) if quantity is not None else None,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "account_mode": "kis_mock",
        "action_mode": "auto_execute_mock",
    }
    detail: dict[str, Any] = {}
    if synthetic_layer.marker_present:
        guard = {
            "contract": _SYNTHETIC_CANARY_CONTRACT,
            "canary_run_id": (
                synthetic_layer.marker.get("canary_run_id")
                if isinstance(synthetic_layer.marker, dict)
                else None
            ),
            "pre_registration_digest": (
                synthetic_layer.marker.get("pre_registration_digest")
                if isinstance(synthetic_layer.marker, dict)
                else None
            ),
            "decision": "blocked" if reasons else "allowed",
            "evaluations": [synthetic_evaluation, env_layer],
        }
        guard_digest = _canonical_json_digest(guard)
        if guard_digest is None:  # pragma: no cover - guard is JSON-native
            raise RuntimeError("synthetic canary guard is not JSON serializable")
        detail = {
            "synthetic_canary": copy.deepcopy(synthetic_layer.marker),
            "synthetic_canary_guard": guard,
        }
        preview_line.update(
            {
                "synthetic_canary": copy.deepcopy(synthetic_layer.marker),
                "synthetic_canary_guard_digest": guard_digest,
            }
        )

    # 3) write intent row (ON CONFLICT correlation_id → idempotent skip).
    stmt = (
        pg_insert(WatchOrderIntentLedger)
        .values(
            correlation_id=correlation_id,
            idempotency_key=f"intent:{alert.alert_uuid}:{kst_date}:{alert.threshold_key}",
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            condition_type=alert.operator,
            threshold=_to_decimal(alert.threshold),
            threshold_key=alert.threshold_key,
            action="auto_execute_mock",
            side=side if side in ("buy", "sell") else "buy",
            account_mode="kis_mock",
            execution_source="watch",
            lifecycle_state=lifecycle,
            quantity=quantity,
            limit_price=limit_price,
            execution_allowed=allowed,
            approval_required=False,
            blocking_reasons=reasons,
            blocked_by=(reasons[0] if reasons else None),
            detail=detail,
            preview_line=preview_line,
            kst_date=kst_date,
        )
        .on_conflict_do_nothing(constraint="uq_watch_intent_correlation_id")
        .returning(WatchOrderIntentLedger.id)
    )
    result = await db.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await db.commit()

    if inserted_id is None:
        return {"executed": False, "skipped": "duplicate"}
    if not allowed:
        return {"executed": False, "blocking_reasons": reasons}

    # 4) place the kis_mock order (executor hard-pinned is_mock=True). A raised
    # exception is a failure too — never leave the intent 'previewed' (ROB-843).
    try:
        place_result: Any = await place_order_fn(
            symbol=alert.symbol,
            side=side,
            order_type="limit",
            quantity=float(quantity),
            price=float(limit_price),
            dry_run=False,
            reason="watch auto_execute_mock",
            is_mock=True,
            correlation_id=correlation_id,
            # Names the lane that owns the order. The kis_mock pre-submit
            # attribution gate refuses to send without it; this path is still
            # gated off by WATCH_AUTO_EXECUTE_MOCK_ENABLED either way.
            strategy=WATCH_AUTO_EXECUTE_STRATEGY,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a truthful failed outcome
        place_result = {
            "success": False,
            "reason": "order_exception",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    # 5) validate + persist the broker outcome truthfully (ROB-843). The result
    # is never discarded: a failure flips the intent row to 'failed' with the
    # stable reason/detail preserved, and returns executed=False.
    outcome = _normalize_place_result(place_result)
    if not outcome.executed:
        logger.warning(
            "auto_execute_mock order failed alert=%s reason=%s",
            alert.alert_uuid,
            outcome.reason,
        )
        await _mark_intent_failed(
            db,
            correlation_id=correlation_id,
            reason=outcome.reason,
            detail=outcome.detail,
            preview_line=preview_line,
        )
        return {
            "executed": False,
            "reason": outcome.reason,
            "detail": outcome.detail,
            "correlation_id": correlation_id,
        }

    return {"executed": True, "correlation_id": correlation_id}


async def _mark_intent_failed(
    db,
    *,
    correlation_id: str,
    reason: str | None,
    detail: str | None,
    preview_line: dict[str, Any],
) -> None:
    """Flip the previewed intent row to 'failed', preserving reason/detail.

    Reuses existing columns only (no schema migration): ``blocked_by`` /
    ``blocking_reasons`` carry the reason and ``preview_line.failure_detail``
    carries the redacted broker detail.
    """
    reason = reason or "order_failed"
    failed_preview = {**preview_line, "failure_detail": detail}
    await db.execute(
        update(WatchOrderIntentLedger)
        .where(WatchOrderIntentLedger.correlation_id == correlation_id)
        .values(
            lifecycle_state="failed",
            execution_allowed=False,
            blocked_by=reason,
            blocking_reasons=[reason],
            preview_line=failed_preview,
        )
    )
    await db.commit()
