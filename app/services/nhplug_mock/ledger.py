"""Service-owned durable NHPLUG mock order ledger transitions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.brokers.nhplug.order_evidence import (
    OrderListing,
    OrderRow,
    derive_order_status,
)
from app.services.nhplug_mock.intent import OrderIntent, body_digest
from app.services.nhplug_mock.readiness import Stage2Readiness
from app.services.nhplug_mock.transport import Stage2Timing

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class LedgerConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    machine_id: str
    boot_id: str
    pid_ns: str
    pid: int
    process_start: int

    def validate(self) -> None:
        if any(
            type(v) is not str or not v
            for v in (self.machine_id, self.boot_id, self.pid_ns)
        ):
            raise LedgerConflict("lease_identity_unavailable")
        if (
            type(self.pid) is not int
            or self.pid <= 0
            or type(self.process_start) is not int
            or self.process_start <= 0
        ):
            raise LedgerConflict("lease_identity_unavailable")


@dataclass(frozen=True, slots=True)
class Claim:
    token: UUID
    row_id: int
    client_request_id: UUID
    account_ref: UUID
    digest: str
    intent: OrderIntent


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    state: str
    reason: str | None = None
    evidence_order_id: str | None = None
    rsp_cd: str | None = None


def _lock_key(account_ref: UUID, idempotency_key: str) -> int:
    digest = hashlib.sha256(
        (str(account_ref) + ":" + idempotency_key).encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _intent_from_row(row: dict[str, Any]) -> OrderIntent:
    return OrderIntent(
        operation_kind=row["operation_kind"],
        side=row["side"],
        symbol=row["symbol"],
        quantity=row["quantity"],
        price=row["price"],
        original_order_id=row["original_order_id"],
        amend_scope=row["amend_scope"],
        account_ref=UUID(str(row["account_ref"])),
        body_schema_version=row["body_schema_version"],
    )


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    for name in ("client_request_id", "account_ref", "claim_token"):
        if result.get(name) is not None:
            result[name] = UUID(str(result[name]))
    return result


class NHPlugMockLedger:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def get(self, row_id: int) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger WHERE id=:id"
                        ),
                        {"id": row_id},
                    )
                )
                .mappings()
                .first()
            )
            return _row_dict(row) if row else None

    async def proof_codes(self, path: str) -> tuple[frozenset[str], frozenset[str]]:
        async with self.engine.connect() as conn:
            success = (
                (
                    await conn.execute(
                        text(
                            "SELECT rsp_cd FROM review.nhplug_success_proof_code WHERE path=:path"
                        ),
                        {"path": path},
                    )
                )
                .scalars()
                .all()
            )
            no_order = (
                (
                    await conn.execute(
                        text(
                            "SELECT rsp_cd FROM review.nhplug_no_order_proof_code WHERE path=:path"
                        ),
                        {"path": path},
                    )
                )
                .scalars()
                .all()
            )
            return frozenset(success), frozenset(no_order)

    async def create_intent(
        self,
        intent: OrderIntent,
        *,
        readiness: Stage2Readiness,
        idempotency_key: str,
        order_date: date,
        duplicate_of: int | None = None,
        second_order_authorization_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """T1/T15. Return (row, should_claim); never enqueue a claimed row again."""

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        intent.validate()
        if (
            type(idempotency_key) is not str
            or _KEY_RE.fullmatch(idempotency_key) is None
        ):
            raise LedgerConflict("invalid_idempotency_key")
        if type(order_date) is not date:
            raise LedgerConflict("invalid_order_date")
        if (duplicate_of is None) != (second_order_authorization_id is None):
            raise LedgerConflict("second_order_authorization_required")
        digest = body_digest(intent)
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _lock_key(intent.account_ref, idempotency_key)},
            )
            previous = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger "
                            "WHERE account_ref=:acct AND idempotency_key=:key ORDER BY attempt_no DESC LIMIT 1"
                        ),
                        {"acct": intent.account_ref, "key": idempotency_key},
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["body_digest"] != digest:
                    raise LedgerConflict("idempotency_key_conflict")
                if previous["state"] != "withdrawn":
                    return _row_dict(previous), previous["state"] == "intent"
                attempt_no = previous["attempt_no"] + 1
            else:
                attempt_no = 1
            row = (
                (
                    await conn.execute(
                        text(
                            "INSERT INTO review.nhplug_mock_order_ledger "
                            "(client_request_id,account_ref,idempotency_key,attempt_no,order_date,"
                            "operation_kind,side,symbol,quantity,price,original_order_id,amend_scope,"
                            "duplicate_of,second_order_authorization_id) VALUES "
                            "(:request,:acct,:key,:attempt,:day,:op,:side,:symbol,:qty,:price,:org,:scope,:dup,:auth) "
                            "ON CONFLICT DO NOTHING RETURNING *"
                        ),
                        {
                            "request": uuid4(),
                            "acct": intent.account_ref,
                            "key": idempotency_key,
                            "attempt": attempt_no,
                            "day": order_date,
                            "op": intent.operation_kind,
                            "side": intent.side,
                            "symbol": intent.symbol,
                            "qty": intent.quantity,
                            "price": intent.price,
                            "org": intent.original_order_id,
                            "scope": intent.amend_scope,
                            "dup": duplicate_of,
                            "auth": second_order_authorization_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["body_digest"] != digest:
                    raise LedgerConflict("digest_mismatch")
                return _row_dict(row), True
            blocking = (
                await conn.execute(
                    text(
                        "SELECT id FROM review.nhplug_mock_order_ledger WHERE account_ref=:acct "
                        "AND symbol=:symbol AND side=:side AND state IN ('intent','claimed','sending','uncertain') LIMIT 1"
                    ),
                    {
                        "acct": intent.account_ref,
                        "symbol": intent.symbol,
                        "side": intent.side,
                    },
                )
            ).scalar_one_or_none()
            if blocking is not None:
                raise LedgerConflict("in_flight_order_exists")
            raise LedgerConflict("duplicate_order_requires_authorization")

    async def claim(
        self,
        row_id: int,
        request_id: UUID,
        digest: str,
        account_ref: UUID,
        identity: LeaseIdentity,
        *,
        claim_window_seconds: int = 30,
    ) -> Claim:
        """T3 is one conditional UPDATE in a committed short transaction."""

        identity.validate()
        if (
            type(request_id) is not UUID
            or type(account_ref) is not UUID
            or type(digest) is not str
        ):
            raise LedgerConflict("claim_identity_invalid")
        token = uuid4()
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "UPDATE review.nhplug_mock_order_ledger SET state='claimed', claim_token=:token, "
                            "claimed_at=now(), claim_deadline=now()+(:window * interval '1 second'), "
                            "lease_machine_id=:machine, lease_boot_id=:boot, lease_pid_ns=:ns, "
                            "lease_pid=:pid, lease_process_start=:start "
                            "WHERE id=:id AND client_request_id=:request AND account_ref=:acct "
                            "AND body_digest=:digest AND state='intent' AND claim_token IS NULL "
                            "RETURNING *"
                        ),
                        {
                            "token": token,
                            "window": claim_window_seconds,
                            "machine": identity.machine_id,
                            "boot": identity.boot_id,
                            "ns": identity.pid_ns,
                            "pid": identity.pid,
                            "start": identity.process_start,
                            "id": row_id,
                            "request": request_id,
                            "acct": account_ref,
                            "digest": digest,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LedgerConflict("claim_rejected")
            claimed = _row_dict(row)
            intent = _intent_from_row(claimed)
            if body_digest(intent) != claimed["body_digest"]:
                raise LedgerConflict("digest_mismatch")
            return Claim(token, row_id, request_id, account_ref, digest, intent)

    async def withdraw(self, claim: Claim, reason: str) -> bool:
        """T4 only before the sending fence."""

        async with self.engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='withdrawn', withdraw_reason=:reason "
                    "WHERE id=:id AND claim_token=:token AND state='claimed' AND sending_at IS NULL"
                ),
                {"reason": reason, "id": claim.row_id, "token": claim.token},
            )
            return result.rowcount == 1

    async def fence(
        self, claim: Claim, *, lease_seconds: int = 120, lock_timeout_ms: int = 5000
    ) -> bool:
        """T5. Caller reads the local first-write clock before entering this method."""

        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('lock_timeout',:value,true)"),
                {"value": f"{lock_timeout_ms}ms"},
            )
            await conn.execute(
                text("SELECT set_config('statement_timeout',:value,true)"),
                {"value": f"{lock_timeout_ms}ms"},
            )
            result = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='sending', sending_at=now(), "
                    "lease_expires_at=now()+(:lease * interval '1 second') "
                    "WHERE id=:id AND claim_token=:token AND state='claimed' "
                    "AND sending_at IS NULL AND now() < claim_deadline RETURNING id"
                ),
                {"lease": lease_seconds, "id": claim.row_id, "token": claim.token},
            )
            return result.scalar_one_or_none() is not None

    async def record_final(self, claim: Claim, outcome: DispatchOutcome) -> bool:
        """T6/T7/T8, with T8e/T9a after recovery won the row lock."""

        if type(outcome) is not DispatchOutcome or outcome.state not in {
            "uncertain",
            "accepted",
            "rejected",
        }:
            raise LedgerConflict("invalid_result")
        if outcome.state == "accepted" and (
            outcome.evidence_order_id is None or outcome.rsp_cd is None
        ):
            raise LedgerConflict("acceptance_proof_missing")
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state=:state, lease_closed_at=now(), "
                        "dispatcher_done_at=now(), uncertain_reason=:reason, "
                        "ack_evidence_order_id=CASE WHEN :state='uncertain' THEN :number ELSE NULL END, "
                        "broker_order_id=CASE WHEN :state='accepted' THEN :number ELSE NULL END, "
                        "ack_order_id=CASE WHEN :state='accepted' THEN :number ELSE NULL END, "
                        "ack_source=CASE WHEN :state='accepted' THEN 'response' ELSE NULL END, "
                        "success_rsp_cd=CASE WHEN :state='accepted' THEN :code ELSE NULL END, "
                        "reject_rsp_cd=CASE WHEN :state='rejected' THEN :code ELSE NULL END "
                        "WHERE id=:id AND claim_token=:token AND state='sending' RETURNING id"
                    ),
                    {
                        "state": outcome.state,
                        "reason": outcome.reason,
                        "number": outcome.evidence_order_id,
                        "code": outcome.rsp_cd,
                        "id": claim.row_id,
                        "token": claim.token,
                    },
                )
            ).scalar_one_or_none()
            if row is not None:
                return True
            if outcome.state == "accepted":
                row = (
                    await conn.execute(
                        text(
                            "UPDATE review.nhplug_mock_order_ledger SET state='accepted', broker_order_id=:number, "
                            "ack_order_id=:number, ack_source='response', success_rsp_cd=:code, dispatcher_done_at=now() "
                            "WHERE id=:id AND claim_token=:token AND state='uncertain' AND dispatcher_done_at IS NULL "
                            "AND (ack_evidence_order_id IS NULL OR ack_evidence_order_id=:number) RETURNING id"
                        ),
                        {
                            "number": outcome.evidence_order_id,
                            "code": outcome.rsp_cd,
                            "id": claim.row_id,
                            "token": claim.token,
                        },
                    )
                ).scalar_one_or_none()
            else:
                row = (
                    await conn.execute(
                        text(
                            "UPDATE review.nhplug_mock_order_ledger SET ack_evidence_order_id=:number, "
                            "late_result_at=CASE WHEN CAST(:number AS text) IS NOT NULL THEN now() ELSE NULL END, "
                            "dispatcher_done_at=now() WHERE id=:id AND claim_token=:token "
                            "AND state='uncertain' AND dispatcher_done_at IS NULL "
                            "AND ack_evidence_order_id IS NULL RETURNING id"
                        ),
                        {
                            "number": outcome.evidence_order_id,
                            "id": claim.row_id,
                            "token": claim.token,
                        },
                    )
                ).scalar_one_or_none()
            return row is not None

    async def recover_expired(
        self, *, intent_stale_seconds: int = 600
    ) -> tuple[int, int, int]:
        """T10i/T10/T8v. Invoked only by manual reconciliation."""

        async with self.engine.begin() as conn:
            intent = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='withdrawn', withdraw_reason='stale_intent' "
                    "WHERE state='intent' AND claim_token IS NULL "
                    "AND created_at < now()-(:stale * interval '1 second')"
                ),
                {"stale": intent_stale_seconds},
            )
            claimed = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='withdrawn', "
                    "withdraw_reason='claim_deadline_passed' WHERE state='claimed' "
                    "AND sending_at IS NULL AND claim_deadline < now()"
                )
            )
            sending = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='uncertain', "
                    "uncertain_reason='lease_expired_without_result', lease_closed_at=now() "
                    "WHERE state='sending' AND lease_expires_at < now()"
                )
            )
            return intent.rowcount, claimed.rowcount, sending.rowcount

    async def verify_own_number(
        self, row_id: int, listing: OrderListing, *, readiness: Stage2Readiness
    ) -> bool:
        """T9b uses a complete, matching listing assembled by reconciliation."""

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if (
            type(listing) is not OrderListing
            or listing.scope != "all"
            or listing.complete is not True
        ):
            raise LedgerConflict("positive_listing_required")
        async with self.engine.begin() as conn:
            current = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
                        ),
                        {"id": row_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                current is None
                or current["state"] != "uncertain"
                or current["ack_evidence_order_id"] is None
            ):
                return False
            intent = _intent_from_row(_row_dict(current))
            target = listing.find(int(current["ack_evidence_order_id"]))
            if target is None:
                return False
            original = (
                None
                if intent.original_order_id is None
                else int(intent.original_order_id)
            )
            attributes_match = (
                target.symbol == intent.symbol
                and target.side == intent.side
                and (intent.quantity is None or target.order_qty == intent.quantity)
                and (
                    intent.price is None or target.order_price == Decimal(intent.price)
                )
                and target.original_order_no == original
            )
            evidence = {
                "listing_order_id": current["ack_evidence_order_id"],
                "listing_complete": True,
                "listing_scope": "all",
                "attributes_match": attributes_match,
                "listing_row": target.evidence(),
            }
            if not attributes_match:
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state='anomaly', "
                        "requires_manual_review=true, manual_review_reason='own_number_attribute_mismatch', "
                        "evidence=CAST(:evidence AS jsonb), last_reconcile=CAST(:evidence AS jsonb) "
                        "WHERE id=:id AND state='uncertain'"
                    ),
                    {"id": row_id, "evidence": json.dumps(evidence)},
                )
                return False
            row = (
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state='accepted', "
                        "broker_order_id=ack_evidence_order_id, ack_order_id=ack_evidence_order_id, "
                        "ack_source='own_evidence', reconcile_state='verified', "
                        "evidence=CAST(:evidence AS jsonb), last_reconcile=CAST(:evidence AS jsonb) "
                        "WHERE id=:id AND state='uncertain' AND ack_evidence_order_id=:number "
                        "RETURNING id"
                    ),
                    {
                        "id": row_id,
                        "number": evidence.get("listing_order_id"),
                        "evidence": json.dumps(evidence),
                    },
                )
            ).scalar_one_or_none()
            return row is not None

    async def record_uncertain_candidates(
        self,
        row_id: int,
        all_listing: OrderListing,
        *,
        readiness: Stage2Readiness,
        time_epsilon_seconds: int = 2,
    ) -> tuple[str, ...]:
        """Record plausible numbers only; a candidate never becomes an acknowledgement."""

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if (
            type(all_listing) is not OrderListing
            or all_listing.scope != "all"
            or not all_listing.complete
        ):
            raise LedgerConflict("complete_all_listing_required")
        if type(time_epsilon_seconds) is not int or not 0 <= time_epsilon_seconds <= 60:
            raise LedgerConflict("invalid_time_epsilon")
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
                        ),
                        {"id": row_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["state"] != "uncertain":
                return ()
            if row["ack_evidence_order_id"] is not None:
                return ()
            intent = _intent_from_row(_row_dict(row))
            start = row["sending_at"] - timedelta(seconds=time_epsilon_seconds)
            end = row["sending_at"] + timedelta(
                seconds=time_epsilon_seconds
                + Stage2Timing.from_env().first_write_seconds
            )
            candidates: dict[str, dict[str, Any]] = {}
            for candidate in all_listing.rows:
                if not _matches_intent(candidate, intent):
                    continue
                observed = _broker_order_time(row["order_date"], candidate.order_time)
                if observed is not None and start <= observed <= end:
                    candidates[str(candidate.order_no)] = candidate.evidence()
            note = {
                "reason": "candidate_only",
                "listing_scope": "all",
                "listing_complete": True,
                "candidate_count": len(candidates),
            }
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET candidate_order_ids=CAST(:candidates AS jsonb), "
                    "requires_manual_review=true, manual_review_reason='unbound_uncertain_order', "
                    "last_reconcile=CAST(:note AS jsonb), reconcile_state='unknown' "
                    "WHERE id=:id AND state='uncertain'"
                ),
                {
                    "id": row_id,
                    "candidates": json.dumps(candidates),
                    "note": json.dumps(note),
                },
            )
            return tuple(candidates)

    async def bind_operator_candidate(
        self,
        row_id: int,
        candidate_order_id: str,
        authorization_id: UUID,
        *,
        readiness: Stage2Readiness,
    ) -> bool:
        """T9h consumes a matching operator authorization in the DB trigger."""

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if (
            type(candidate_order_id) is not str
            or re.fullmatch(r"[1-9][0-9]{0,9}", candidate_order_id) is None
        ):
            raise LedgerConflict("candidate_number_invalid")
        if type(authorization_id) is not UUID:
            raise LedgerConflict("authorization_invalid")
        async with self.engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='accepted', "
                    "broker_order_id=:number, ack_order_id=:number, ack_source='operator', "
                    "resolution_authorization_id=:auth WHERE id=:id AND state='uncertain' "
                    "AND candidate_order_ids ? :number RETURNING id"
                ),
                {"id": row_id, "number": candidate_order_id, "auth": authorization_id},
            )
            return result.scalar_one_or_none() is not None

    async def abandon_with_authorization(
        self,
        row_id: int,
        authorization_id: UUID,
        all_listing: OrderListing,
        *,
        readiness: Stage2Readiness,
    ) -> bool:
        """T14 accepts documented unresolved risk only after local host death proof."""

        from app.services.nhplug_mock.lease_host import (
            lease_identity_from_row,
            process_gone_on_lease_host,
        )

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if type(authorization_id) is not UUID:
            raise LedgerConflict("authorization_invalid")
        if (
            type(all_listing) is not OrderListing
            or all_listing.scope != "all"
            or not all_listing.complete
        ):
            raise LedgerConflict("complete_all_listing_required")
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
                        ),
                        {"id": row_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or row["state"] != "uncertain"
                or row["lease_closed_at"] is None
            ):
                return False
            own = row["ack_evidence_order_id"]
            if own is not None and all_listing.find(int(own)) is not None:
                raise LedgerConflict("own_number_present")
            if row["candidate_order_ids"]:
                raise LedgerConflict("candidate_risk_unresolved")
            if not process_gone_on_lease_host(lease_identity_from_row(dict(row))):
                raise LedgerConflict("lease_process_not_proven_gone")
            result = await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='abandoned', "
                    "resolution_authorization_id=:auth, manual_review_reason='operator_accepted_unresolved_risk' "
                    "WHERE id=:id AND state='uncertain' RETURNING id"
                ),
                {"id": row_id, "auth": authorization_id},
            )
            return result.scalar_one_or_none() is not None

    async def reconcile_bound(
        self,
        row_id: int,
        all_listing: OrderListing,
        open_listing: OrderListing,
        filled_listing: OrderListing | None,
        *,
        readiness: Stage2Readiness,
    ) -> str:
        """T11/T12: close only with complete scopes and independent positive sources."""

        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if (
            type(all_listing) is not OrderListing
            or all_listing.scope != "all"
            or not all_listing.complete
            or type(open_listing) is not OrderListing
            or open_listing.scope != "open"
            or not open_listing.complete
            or (
                filled_listing is not None
                and (
                    type(filled_listing) is not OrderListing
                    or filled_listing.scope != "filled"
                )
            )
        ):
            raise LedgerConflict("complete_listing_required")
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
                        ),
                        {"id": row_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["state"] not in {
                "accepted",
                "open",
                "partially_filled",
            }:
                raise LedgerConflict("not_reconcilable")
            number = int(row["broker_order_id"])
            own = all_listing.find(number)
            intent = _intent_from_row(_row_dict(row))
            if intent.operation_kind == "cancel" and row["state"] != "accepted":
                raise LedgerConflict("not_reconcilable")
            reason: str | None = None
            target: str | None = None
            successor: str | None = None
            applied: int | None = None
            original: OrderRow | None = None
            if intent.operation_kind == "cancel":
                original = all_listing.find(int(intent.original_order_id or "0"))
                if (
                    own is None
                    or original is None
                    or own.original_order_no != original.order_no
                    or not _matches_intent(own, intent)
                ):
                    reason = "cancel_ack_or_original_missing"
                elif original.cancelled_qty is None or original.cancelled_qty <= 0:
                    reason = "cancel_quantity_not_reflected"
                elif (
                    open_listing.find(original.order_no) is not None
                    or original.open_qty != 0
                ):
                    reason = "original_still_open"
                else:
                    target = "confirmed"
                    applied = original.cancelled_qty
            elif own is None or not _matches_intent(own, intent):
                reason = "own_order_missing_or_mismatched"
            elif own.order_qty != intent.quantity:
                reason = "order_quantity_drift"
            else:
                derived = derive_order_status(own)
                in_open = open_listing.find(number) is not None
                if derived not in {
                    "open",
                    "partially_filled",
                    "filled",
                    "cancelled",
                    "modified",
                }:
                    reason = "broker_quantities_inconsistent"
                elif derived in {"open", "partially_filled"} and not in_open:
                    reason = "open_scope_disagreement"
                elif derived in {"filled", "cancelled", "modified"} and in_open:
                    reason = "terminal_open_scope_disagreement"
                elif own.filled_qty > 0 and (
                    filled_listing is None
                    or not filled_listing.complete
                    or (filled := filled_listing.find(number)) is None
                    or filled.filled_qty != own.filled_qty
                ):
                    reason = "fill_not_confirmed"
                elif derived == "cancelled" and not await _own_ack_exists(
                    conn, row, "cancel", number
                ):
                    reason = "own_cancel_ack_missing"
                elif derived == "modified":
                    successor = await _own_successor(conn, row, number, all_listing)
                    if successor is None:
                        reason = "own_modify_ack_or_successor_missing"
                    else:
                        target = derived
                else:
                    target = derived
            note = {
                "listing_order_id": str(number),
                "listing_scope": "all",
                "listing_complete": True,
                "reason": reason or "positive_sources_verified",
            }
            if target is None:
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET reconcile_state='unknown', "
                        "last_reconcile=CAST(:note AS jsonb) WHERE id=:id"
                    ),
                    {"id": row_id, "note": json.dumps(note)},
                )
                return "unknown"
            evidence = {
                "all_order": own.evidence() if own else None,
                "open_order": open_listing.find(number).evidence()
                if open_listing.find(number)
                else None,
            }
            if filled_listing is not None and filled_listing.find(number) is not None:
                evidence["filled_order"] = filled_listing.find(number).evidence()
            if intent.operation_kind == "cancel":
                assert original is not None
                evidence["original_order"] = original.evidence()
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state='confirmed', reconcile_state='verified', "
                        "applied_qty=:applied, evidence=CAST(:evidence AS jsonb), last_reconcile=CAST(:note AS jsonb) "
                        "WHERE id=:id AND state='accepted'"
                    ),
                    {
                        "id": row_id,
                        "applied": applied,
                        "evidence": json.dumps(evidence),
                        "note": json.dumps(note),
                    },
                )
            else:
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state=:state, reconcile_state='verified', "
                        "filled_qty=:filled, open_qty=:open, cancelled_qty=:cancelled, modified_qty=:modified, "
                        "avg_fill_price=:avg, successor_order_id=:successor, "
                        "evidence=CAST(:evidence AS jsonb), last_reconcile=CAST(:note AS jsonb) "
                        "WHERE id=:id AND state IN ('accepted','open','partially_filled')"
                    ),
                    {
                        "id": row_id,
                        "state": target,
                        "filled": own.filled_qty,
                        "open": own.open_qty,
                        "cancelled": own.cancelled_qty or 0,
                        "modified": own.modified_qty or 0,
                        "avg": own.avg_fill_price,
                        "successor": successor,
                        "evidence": json.dumps(evidence),
                        "note": json.dumps(note),
                    },
                )
            return target


def _matches_intent(candidate: OrderRow, intent: OrderIntent) -> bool:
    return (
        candidate.symbol == intent.symbol
        and candidate.side == intent.side
        and (intent.quantity is None or candidate.order_qty == intent.quantity)
        and (intent.price is None or candidate.order_price == Decimal(intent.price))
        and candidate.original_order_no
        == (None if intent.original_order_id is None else int(intent.original_order_id))
    )


def _broker_order_time(day: date, raw: str | None) -> datetime | None:
    if raw is None or re.fullmatch(r"[0-9]{6}(?:[0-9]{3})?", raw) is None:
        return None
    try:
        moment = time(
            int(raw[:2]), int(raw[2:4]), int(raw[4:6]), int(raw[6:9] or "0") * 1000
        )
    except ValueError:
        return None
    return datetime.combine(day, moment, tzinfo=ZoneInfo("Asia/Seoul"))


async def _own_ack_exists(conn: Any, row: Any, kind: str, original_no: int) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM review.nhplug_mock_order_ledger WHERE account_ref=:acct AND order_date=:day "
                    "AND operation_kind=:kind AND original_order_id=:number AND ack_order_id IS NOT NULL "
                    "AND ack_source IN ('response','own_evidence','operator') "
                    "AND state IN ('accepted','confirmed','open','partially_filled','filled','cancelled','modified') LIMIT 1"
                ),
                {
                    "acct": row["account_ref"],
                    "day": row["order_date"],
                    "kind": kind,
                    "number": str(original_no),
                },
            )
        ).scalar_one_or_none()
    )


async def _own_successor(
    conn: Any, row: Any, original_no: int, listing: OrderListing
) -> str | None:
    numbers = (
        (
            await conn.execute(
                text(
                    "SELECT ack_order_id FROM review.nhplug_mock_order_ledger WHERE account_ref=:acct AND order_date=:day "
                    "AND operation_kind='modify' AND original_order_id=:number AND ack_order_id IS NOT NULL "
                    "AND ack_source IN ('response','own_evidence','operator') "
                    "AND state IN ('accepted','confirmed','open','partially_filled','filled','cancelled','modified')"
                ),
                {
                    "acct": row["account_ref"],
                    "day": row["order_date"],
                    "number": str(original_no),
                },
            )
        )
        .scalars()
        .all()
    )
    for number in numbers:
        successor = listing.find(int(number))
        if successor is not None and successor.original_order_no == original_no:
            return str(number)
    return None
