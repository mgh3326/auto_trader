"""Operator-run recovery of KR mock dispatch evidence, without broker writes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.services.brokers.kis.live_order_expiry import kr_day_order_expiry
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    KISMockRecoveryLineage,
    NativeOrderEvidence,
    RediscoveredKISMockClaim,
)
from app.services.kis_mock_runner.position_proof import (
    BalanceSnapshot,
    PositionProof,
    PositionUnproven,
    reconcile_position,
)
from app.services.mock_integration.coordination import (
    DurableSendClaimAdapter,
    TerminalClaimEvidence,
)

KST = timezone(timedelta(hours=9))
RecoveryReader = Callable[[str, datetime], Awaitable[dict[str, Any]]]
PositionReader = Callable[[str], Awaitable[BalanceSnapshot]]
Clock = Callable[[], datetime]
_NON_FILL_KINDS = {
    "terminal_cancel": "cancel",
    "terminal_expiry": "expiry",
    "zero_fill_terminal": "reject",
}
_TERMINAL_STATES = frozenset({"terminal_fill", *_NON_FILL_KINDS})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quantity(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is str and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _classify_readback(
    row: dict[str, Any], lineage: KISMockRecoveryLineage, *, now: datetime
) -> tuple[str, str, int] | None:
    """Classify a single exact-order row, rejecting ambiguous quantities and dates."""
    envelope = lineage.envelope
    plan = envelope.execution_plan
    if plan is None:
        return None
    order_date, order_time = row.get("ord_dt"), row.get("ord_tmd")
    if (
        type(order_date) is not str
        or re.fullmatch(r"[0-9]{8}", order_date) is None
        or type(order_time) is not str
        or re.fullmatch(r"[0-9]{6}", order_time) is None
    ):
        return None
    try:
        ordered_at = datetime.strptime(order_date + order_time, "%Y%m%d%H%M%S").replace(
            tzinfo=KST
        )
    except ValueError:
        return None
    if not (
        ordered_at.date() == lineage.created_at.astimezone(KST).date()
        and lineage.created_at.astimezone(KST).replace(microsecond=0)
        <= ordered_at
        <= min(lineage.acknowledged_at.astimezone(KST), now.astimezone(KST))
    ):
        return None
    ordered = _quantity(row.get("ord_qty"))
    filled = _quantity(row.get("tot_ccld_qty"))
    remaining = _quantity(row.get("rmn_qty"))
    if (
        ordered is None
        or filled is None
        or remaining is None
        or ordered <= 0
        or Decimal(ordered) != plan.quantity
        or filled > ordered
        or remaining > ordered
        or filled + remaining > ordered
        or row.get("pdno") != plan.normalized_symbol
        or row.get("sll_buy_dvsn_cd")
        != ("02" if envelope.decision_intent.side == "buy" else "01")
    ):
        return None

    projection = {
        field: row[field]
        for field in ("odno", "ord_dt", "ord_tmd", "pdno", "sll_buy_dvsn_cd")
    }
    projection.update(ord_qty=ordered, tot_ccld_qty=filled, rmn_qty=remaining)
    digest = hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if filled == ordered and remaining == 0:
        return "terminal_fill", digest, filled
    if filled > 0 and filled + remaining == ordered and remaining > 0:
        return "partial_fill", digest, filled
    if filled == 0 and remaining == ordered:
        return "accepted", digest, filled
    if remaining != 0:
        return None
    return _classify_non_fill(
        row,
        projection,
        ordered_at=ordered_at,
        side=envelope.decision_intent.side,
        filled=filled,
        unfilled=ordered - filled,
        now=now,
    )


def _classify_non_fill(
    row: dict[str, Any],
    projection: dict[str, Any],
    *,
    ordered_at: datetime,
    side: str,
    filled: int,
    unfilled: int,
    now: datetime,
) -> tuple[str, str, int] | None:
    """Name a non-fill ending only from the attributed row's own positive fields."""
    cancel_flag = row.get("cncl_yn")
    confirmed = _quantity(row.get("cncl_cfrm_qty"))
    rejected = _quantity(row.get("rjct_qty"))
    state: str | None = None
    if cancel_flag == "Y":
        if confirmed == unfilled and rejected in (0, None):
            state = "terminal_cancel"
    elif cancel_flag == "N" and rejected == unfilled and confirmed in (0, None):
        expiry_iso, _ = kr_day_order_expiry(accepted_at=ordered_at, side=side)
        if expiry_iso is not None and now >= datetime.fromisoformat(expiry_iso):
            state = "terminal_expiry"
        elif filled == 0:
            state = "zero_fill_terminal"
    if state is None:
        return None
    digest = hashlib.sha256(
        json.dumps(
            {
                **projection,
                "cncl_yn": cancel_flag,
                "cncl_cfrm_qty": confirmed,
                "rjct_qty": rejected,
                "state": state,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return state, digest, filled


async def recover_kis_mock_dispatch_claims(
    *,
    store: KISMockEvidenceStore,
    claims: DurableSendClaimAdapter,
    reader: RecoveryReader,
    dry_run: bool = True,
    confirm: bool = False,
    record_dispatch_evidence: bool = False,
    position_reader: PositionReader | None = None,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Release a claim only on terminal order evidence AND exact position proof.

    Everything else keeps the claim and reports why. Preview never releases.
    """
    if any(
        type(value) is not bool
        for value in (dry_run, confirm, record_dispatch_evidence)
    ):
        raise ValueError("recovery_confirmation_invalid")
    if not dry_run and confirm is not True:
        raise ValueError("confirm_required")
    if record_dispatch_evidence and (dry_run or confirm is not True):
        raise ValueError("record_dispatch_evidence_requires_confirmed_apply")
    try:
        discovered = await store.rediscover_unreleased_claims()
    except Exception:  # noqa: BLE001 - account uncertainty is fail-closed
        return {
            "status": "uncertain",
            "account_blocked": True,
            "reason": "claim_rediscovery_unavailable",
            "claims": [],
            "dry_run": dry_run,
            "record_dispatch_evidence": record_dispatch_evidence,
        }

    results = []
    for item in discovered:
        result = await _recover_one(
            store=store,
            claims=claims,
            item=item,
            reader=reader,
            record_dispatch_evidence=record_dispatch_evidence,
            position_reader=position_reader,
            sole_claim=len(discovered) == 1,
            clock=clock,
        )
        results.append(result)
    blocked = any(item["status"] != "released" for item in results)
    return {
        "status": "blocked"
        if blocked
        else ("released" if results else "no_unreleased_claims"),
        "account_blocked": blocked,
        "claims": results,
        "dry_run": dry_run,
        "record_dispatch_evidence": record_dispatch_evidence,
    }


async def _recover_one(
    *,
    store: KISMockEvidenceStore,
    claims: DurableSendClaimAdapter,
    item: RediscoveredKISMockClaim,
    reader: RecoveryReader,
    record_dispatch_evidence: bool,
    position_reader: PositionReader | None,
    sole_claim: bool,
    clock: Clock,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "claim_id": item.claim.row_id,
        "status": "retained",
        "reason": "position proof unavailable",
        "disposition": "retained: position proof unavailable",
        "readback": "uncertain",
    }
    if (
        type(item.broker_order_id) is not str
        or re.fullmatch(r"[0-9]+", item.broker_order_id) is None
    ):
        result["readback_reason"] = (
            item.attribution_error or "attributed_odno_unavailable"
        )
        return result
    try:
        lineage = await store.load_recovery_lineage(item)

        async def read_exact(odno: str) -> dict[str, Any]:
            return await reader(odno, lineage.created_at)

        row = await store.readback_by_exact_odno(item, read_exact)
        classified = _classify_readback(row, lineage, now=clock())
        if classified is None:
            result["readback_reason"] = "readback_incomplete_or_inconsistent"
            return result
        state, digest, filled = classified
        result["readback"] = state
        if not record_dispatch_evidence:
            result["evidence"] = "not_recorded"
            return result

        kind = (
            _NON_FILL_KINDS.get(state)
            or {
                "terminal_fill": "terminal_reconciliation",
                "partial_fill": "partial_fill",
                "accepted": "ack",
            }[state]
        )
        terminal_order = state in _TERMINAL_STATES
        key = f"operator-recovery-v1:{digest}"
        await store.record_native_evidence(
            lineage.envelope,
            NativeOrderEvidence(
                kind,
                key,
                item.broker_order_id,
                row["odno"],
                TerminalClaimEvidence(
                    lane_native_terminal_evidence=True,
                    account_position_reconciled=False,
                    remainder_known=True,
                )
                if state == "terminal_fill"
                else TerminalClaimEvidence(),
                filled if terminal_order else None,
            ),
        )
        result["evidence"] = "recorded"
        if not terminal_order:
            return result
        proof = await _prove_position(
            store=store,
            item=item,
            lineage=lineage,
            position_reader=position_reader,
            sole_claim=sole_claim,
            result=result,
            clock=clock,
        )
        if proof is None:
            return result
        # A new event: the unproven one above is never upgraded in place.
        proven_key = f"operator-recovery-position-v1:{digest}:{proof.read_digest}"
        terminal = TerminalClaimEvidence(
            lane_native_terminal_evidence=True,
            account_position_reconciled=True,
            remainder_known=True,
        )
        await store.record_native_evidence(
            lineage.envelope,
            NativeOrderEvidence(
                "terminal_reconciliation",
                proven_key,
                item.broker_order_id,
                row["odno"],
                terminal,
                proof.filled_quantity,
                proof,
            ),
        )
        released = await store.release_with_recorded_terminal_evidence(
            lineage.envelope,
            broker_evidence_key=proven_key,
            claim=item.claim,
            claims=claims,
        )
        result["position"] = "proven"
        if released != 1:
            result["position_reason"] = "claim_owner_changed"
            return result
        result.update(
            status="released",
            reason="terminal evidence and position proven",
            disposition="released",
        )
    except KISMockEvidenceConflict as exc:
        result["readback"] = "uncertain"
        result["readback_reason"] = (
            str(exc)
            if str(exc)
            in {"recovery_dispatch_day_ambiguous", "recovery_readback_not_unique"}
            else "exact_readback_or_claim_unavailable"
        )
    except Exception:  # noqa: BLE001 - read/write failure is never release authority
        result["readback"] = "uncertain"
        result["readback_reason"] = "recovery_error"
    return result


async def _prove_position(
    *,
    store: KISMockEvidenceStore,
    item: RediscoveredKISMockClaim,
    lineage: KISMockRecoveryLineage,
    position_reader: PositionReader | None,
    sole_claim: bool,
    result: dict[str, Any],
    clock: Clock,
) -> PositionProof | None:
    """Return a proof, or record a closed reason. Never treats a doubt as zero."""
    plan = lineage.envelope.execution_plan
    try:
        if not sole_claim:
            raise PositionUnproven("position_other_claim_unresolved")
        if position_reader is None or plan is None:
            raise PositionUnproven("position_reader_unavailable")
        baseline = await store.load_position_baseline(lineage)
        filled = await store.attributed_filled_quantity(item)
        snapshot = await position_reader(plan.normalized_symbol)
        return reconcile_position(
            side=lineage.envelope.decision_intent.side,
            baseline=baseline,
            filled=filled,
            snapshot=snapshot,
            symbol=plan.normalized_symbol,
            acknowledged_at=lineage.acknowledged_at,
            now=clock(),
        )
    except PositionUnproven as exc:
        result["position_reason"] = exc.reason
    except Exception:  # noqa: BLE001 - a failed read is unproven, never zero
        result["position_reason"] = "position_read_failed"
    return None
