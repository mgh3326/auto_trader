"""Operator-run recovery of KR mock dispatch evidence, without broker writes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    KISMockRecoveryLineage,
    NativeOrderEvidence,
    RediscoveredKISMockClaim,
)
from app.services.mock_integration.coordination import (
    DurableSendClaimAdapter,
    TerminalClaimEvidence,
)

KST = timezone(timedelta(hours=9))
RecoveryReader = Callable[[str, datetime], Awaitable[dict[str, Any]]]


def _quantity(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is str and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _classify_readback(
    row: dict[str, Any], lineage: KISMockRecoveryLineage, *, now: datetime
) -> tuple[str, str] | None:
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
        return "terminal_fill", digest
    if filled > 0 and filled + remaining == ordered and remaining > 0:
        return "partial_fill", digest
    if filled == 0 and remaining == ordered:
        return "accepted", digest
    return None


async def recover_kis_mock_dispatch_claims(
    *,
    store: KISMockEvidenceStore,
    claims: DurableSendClaimAdapter,
    reader: RecoveryReader,
    dry_run: bool = True,
    confirm: bool = False,
    record_dispatch_evidence: bool = False,
) -> dict[str, Any]:
    """Read surviving claims; no source in this version proves account position.

    Each reported claim stays blocked. Exact terminal readback may be recorded
    with the position bit explicitly false; hk 2690 owns a real position proof.
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
        )
        results.append(result)
    return {
        "status": "blocked" if results else "no_unreleased_claims",
        "account_blocked": bool(results),
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
        classified = _classify_readback(row, lineage, now=datetime.now(UTC))
        if classified is None:
            result["readback_reason"] = "readback_incomplete_or_inconsistent"
            return result
        state, digest = classified
        result["readback"] = state
        if not record_dispatch_evidence:
            result["evidence"] = "not_recorded"
            return result

        kind = {
            "terminal_fill": "terminal_reconciliation",
            "partial_fill": "partial_fill",
            "accepted": "ack",
        }[state]
        terminal = (
            TerminalClaimEvidence(
                lane_native_terminal_evidence=True,
                account_position_reconciled=False,
                remainder_known=True,
            )
            if state == "terminal_fill"
            else TerminalClaimEvidence()
        )
        key = f"operator-recovery-v1:{digest}"
        await store.record_native_evidence(
            lineage.envelope,
            NativeOrderEvidence(kind, key, item.broker_order_id, row["odno"], terminal),
        )
        # This version has no position-proof source, so this condition is false
        # even for a fully filled order. The store independently rechecks the
        # committed event and the common terminal-evidence AND gate.
        if terminal.authorizes_release:
            await store.release_with_recorded_terminal_evidence(
                lineage.envelope,
                broker_evidence_key=key,
                claim=item.claim,
                claims=claims,
            )
        result["evidence"] = "recorded"
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
