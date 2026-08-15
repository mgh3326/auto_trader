"""Funding advisory evaluation, revision, and delivery-claim orchestration."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.funding_advisory import canonical_decimal
from app.services.funding_advisory._repository import FundingAdvisoryRepository
from app.services.funding_advisory.contracts import (
    FundingCandidateEvent,
    FundingRoute,
)
from app.services.funding_advisory.external_cash import (
    ExternalCashDeclarationService,
)
from app.services.funding_advisory.ranking import (
    build_reference_combination,
    compare_routes,
)

KST = ZoneInfo("Asia/Seoul")


class FundingAdvisoryError(Exception):
    """Base domain error."""


class FundingAdvisoryNotFound(FundingAdvisoryError):
    """No owner-scoped advisory exists for the requested id."""


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FundingAdvisoryError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _thread_key(event: FundingCandidateEvent) -> str:
    evidence = event.evidence
    identity = {
        "owner_user_id": evidence.owner_user_id,
        "source_kind": evidence.source_kind,
        "source_candidate_id": evidence.source_candidate_id,
        "gate_name": evidence.gate_name,
        # Contract/schema version only. Per-evaluation evidence_hash is excluded
        # so repeated evaluation cannot escape the one-thread/day delivery cap.
        "gate_version": evidence.gate_version,
        "target_account_mode": evidence.target_account_mode,
        "broker_account_id": evidence.broker_account_id,
        "currency": evidence.currency,
    }
    return f"funding:{_canonical_hash(identity)}"


def _unknown_route(
    route_id: str,
    label: str,
    *,
    amount_status: str = "unknown",
    confidence: str = "unknown",
    eligibility: str = "comparison_unavailable",
    reason_codes: list[str],
) -> FundingRoute:
    return FundingRoute.model_validate(
        {
            "route_id": route_id,
            "label": label,
            "amount_status": amount_status,
            "route_fundable_amount": None,
            "counted_fundable_amount": Decimal("0"),
            "confidence": confidence,
            "source_as_of": None,
            "deadline_status": "unknown",
            "explicit_cost": None,
            "eta_minutes": None,
            "realized_impact": None,
            "reversibility": "unknown",
            "eligibility": eligibility,
            "reason_codes": reason_codes,
        }
    )


class FundingAdvisoryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        _repository: FundingAdvisoryRepository | None = None,
        _external_cash_service: ExternalCashDeclarationService | None = None,
    ) -> None:
        self._session = session
        self._repository = _repository or FundingAdvisoryRepository(session)
        self._external_cash = _external_cash_service or ExternalCashDeclarationService(
            session
        )

    async def _routes(
        self,
        event: FundingCandidateEvent,
        *,
        shortfall: Decimal,
        now: datetime,
    ) -> list[FundingRoute]:
        external_views = await self._external_cash.list_current(
            owner_user_id=event.evidence.owner_user_id,
            now=now,
        )
        external_rows = [
            view.current
            for view in external_views
            if view.status == "fresh"
            and view.current is not None
            and view.current.currency == event.evidence.currency
        ]
        declared_total = sum((row.amount for row in external_rows), start=Decimal("0"))
        if declared_total > 0:
            external = FundingRoute(
                route_id="EXTERNAL_PARKING_KRW",
                label="외부 파킹 현금",
                amount_status="known",
                route_fundable_amount=min(declared_total, shortfall),
                counted_fundable_amount=Decimal("0"),
                confidence="operator_declared",
                source_as_of=max(row.as_of for row in external_rows),
                deadline_status="unknown",
                explicit_cost=None,
                eta_minutes=None,
                realized_impact=Decimal("0"),
                reversibility="reversible",
                eligibility="comparison_unavailable",
                reason_codes=[
                    "operator_declared_unverified",
                    "transfer_eta_unknown",
                    "target_balance_not_yet_confirmed",
                ],
            )
        else:
            external = _unknown_route(
                "EXTERNAL_PARKING_KRW",
                "외부 파킹 현금",
                reason_codes=["fresh_external_cash_unavailable"],
            )

        routes = [
            external,
            _unknown_route(
                "USD_CONVERSION",
                "USD 환전",
                reason_codes=["executable_sell_fx_or_fee_unavailable"],
            ),
            _unknown_route(
                "CREDIT_LINE_SHORT_TERM",
                "단기 신용한도",
                reason_codes=["verified_limit_apr_and_eta_unavailable"],
            ),
            _unknown_route(
                "PROFITABLE_TRIM",
                "수익권 트림",
                amount_status="conditional",
                confidence="conditional",
                eligibility="locked",
                reason_codes=["strict_sellable_quote_fee_tax_output_unavailable"],
            ),
            _unknown_route(
                "LOSS_CUT_ROTATION",
                "독립 손절 전환",
                amount_status="conditional",
                confidence="conditional",
                eligibility="locked",
                reason_codes=[
                    "independent_loss_cut_intent_required",
                    "existing_two_click_confirmation_required",
                ],
            ),
        ]
        return compare_routes(routes)

    async def evaluate_candidate_event(
        self, event: FundingCandidateEvent, *, now: datetime
    ) -> dict[str, Any]:
        """Evaluate an upstream candidate event and optionally claim Telegram."""

        return await self._evaluate(event, now=now, event_kind="candidate_event")

    async def _evaluate(
        self,
        event: FundingCandidateEvent,
        *,
        now: datetime,
        event_kind: Literal["candidate_event", "page_refresh"],
    ) -> dict[str, Any]:
        current_now = _aware(now, "now")
        evidence = event.evidence
        assessment = event.assessment
        if current_now >= _aware(evidence.valid_until, "evidence.valid_until"):
            return {"status": "not_triggered", "reason": "evidence_expired"}
        if current_now >= _aware(assessment.valid_until, "assessment.valid_until"):
            return {"status": "not_triggered", "reason": "funding_snapshot_expired"}

        required = assessment.required_cash
        target = assessment.target_buying_power
        shortfall = max(required - target, Decimal("0"))
        operational_gap = max(
            required
            + assessment.other_pending_required
            + assessment.reserved_cash
            - target,
            Decimal("0"),
        )
        thread_key = _thread_key(event)

        try:
            await self._repository.acquire_lock(thread_key)
            advisory = await self._repository.get_advisory_by_thread(
                thread_key, for_update=True
            )
            if shortfall <= 0:
                if advisory is not None and advisory.state == "active":
                    await self._repository.update_advisory(
                        advisory, state="resolved", updated_at=current_now
                    )
                await self._session.commit()
                return {
                    "status": "not_triggered",
                    "reason": "no_candidate_shortfall",
                    "shortfall": "0",
                    "other_pending_required": canonical_decimal(
                        assessment.other_pending_required
                    ),
                    "reserved_cash": canonical_decimal(assessment.reserved_cash),
                    "operational_gap": canonical_decimal(operational_gap),
                }

            routes = await self._routes(event, shortfall=shortfall, now=current_now)
            combination = build_reference_combination(routes, shortfall=shortfall)
            evidence_payload = {
                "gate": evidence.model_dump(mode="json"),
                "assessment": assessment.canonical_payload(),
            }
            route_payloads = [route.json_payload() for route in routes]
            fingerprint = _canonical_hash(
                {
                    "evidence_hash": evidence.evidence_hash,
                    "assessment": assessment.canonical_payload(),
                    "routes": route_payloads,
                    "combination": combination,
                }
            )

            if advisory is None:
                advisory = await self._repository.insert_advisory(
                    advisory_id=uuid.uuid4(),
                    owner_user_id=evidence.owner_user_id,
                    thread_key=thread_key,
                    source_kind=evidence.source_kind,
                    source_candidate_id=evidence.source_candidate_id,
                    gate_name=evidence.gate_name,
                    gate_version=evidence.gate_version,
                    market=evidence.market,
                    target_account_mode=evidence.target_account_mode,
                    broker_account_id=evidence.broker_account_id,
                    currency=evidence.currency,
                    symbol=evidence.symbol,
                    side="buy",
                    state="active",
                    valid_until=evidence.valid_until,
                    updated_at=current_now,
                )
            else:
                await self._repository.update_advisory(
                    advisory,
                    state="active",
                    valid_until=evidence.valid_until,
                    updated_at=current_now,
                )

            revision = await self._repository.get_revision_by_fingerprint(
                advisory_id=advisory.advisory_id,
                fingerprint=fingerprint,
            )
            if revision is None:
                previous = await self._repository.latest_revision(advisory.advisory_id)
                revision = await self._repository.insert_revision(
                    revision_id=uuid.uuid4(),
                    advisory_id=advisory.advisory_id,
                    revision_no=(previous.revision_no + 1) if previous else 1,
                    fingerprint=fingerprint,
                    evidence=evidence_payload,
                    required_cash=required,
                    target_buying_power=target,
                    other_pending_required=assessment.other_pending_required,
                    reserved_cash=assessment.reserved_cash,
                    shortfall=shortfall,
                    operational_gap=operational_gap,
                    routes=route_payloads,
                    combination=combination,
                    evaluated_at=current_now,
                    expires_at=min(evidence.valid_until, assessment.valid_until),
                )

            delivery = {"action": "none", "reason": "page_refresh_no_delivery"}
            if event_kind == "candidate_event":
                delivery = await self._claim_delivery(
                    advisory_id=advisory.advisory_id,
                    revision_id=revision.revision_id,
                    now=current_now,
                )
            await self._session.commit()
            return self._projection(advisory, revision, delivery=delivery)
        except Exception:
            await self._session.rollback()
            raise

    async def _claim_delivery(
        self, *, advisory_id: UUID, revision_id: UUID, now: datetime
    ) -> dict[str, Any]:
        kst_date = now.astimezone(KST).date()
        row = await self._repository.get_delivery(
            advisory_id=advisory_id,
            channel="telegram",
            kst_date=kst_date,
            for_update=True,
        )
        if row is None:
            row = await self._repository.insert_delivery(
                delivery_id=uuid.uuid4(),
                advisory_id=advisory_id,
                channel="telegram",
                kst_date=kst_date,
                state="claimed",
                revision_id=revision_id,
                claimed_at=now,
                updated_at=now,
            )
            return {
                "action": "send",
                "delivery_id": str(row.delivery_id),
                "chat_id": None,
                "message_id": None,
            }
        if row.state == "sent" and row.revision_id != revision_id:
            return {
                "action": "edit",
                "delivery_id": str(row.delivery_id),
                "chat_id": row.chat_id,
                "message_id": row.message_id,
            }
        return {
            "action": "none",
            "delivery_id": str(row.delivery_id),
            "reason": "same_day_delivery_already_claimed",
        }

    async def record_delivery_result(
        self,
        *,
        delivery_id: UUID,
        revision_id: UUID,
        action: Literal["send", "edit"],
        state: Literal["sent", "send_failed", "edit_failed", "delivery_unknown"],
        now: datetime,
        chat_id: str | None = None,
        message_id: int | None = None,
        failure_code: str | None = None,
    ) -> None:
        current_now = _aware(now, "now")
        try:
            await self._repository.acquire_lock(f"funding-delivery:{delivery_id}")
            row = await self._repository.get_delivery_by_id(
                delivery_id, for_update=True
            )
            if row is None:
                raise FundingAdvisoryNotFound(str(delivery_id))
            if action == "edit" and row.state != "sent":
                raise FundingAdvisoryError("only a sent delivery can be edited")
            await self._repository.update_delivery(
                row,
                state=state,
                revision_id=revision_id if state == "sent" else row.revision_id,
                chat_id=chat_id if chat_id is not None else row.chat_id,
                message_id=message_id if message_id is not None else row.message_id,
                failure_code=failure_code,
                delivered_at=current_now if state == "sent" else row.delivered_at,
                updated_at=current_now,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    def _projection(
        self, advisory: Any, revision: Any, *, delivery: dict[str, Any]
    ) -> dict[str, Any]:
        gate = revision.evidence["gate"]
        return {
            "status": "triggered",
            "advisory_id": str(advisory.advisory_id),
            "thread_key": advisory.thread_key,
            "state": advisory.state,
            "revision_id": str(revision.revision_id),
            "revision_no": revision.revision_no,
            "fingerprint": revision.fingerprint,
            "trigger": {
                "source_kind": advisory.source_kind,
                "source_candidate_id": advisory.source_candidate_id,
                "gate_name": advisory.gate_name,
                "gate_version": advisory.gate_version,
                "gate_version_kind": "contract_schema_version",
                "gate_verdict": "passed",
                "gate_evaluated_at": gate["gate_evaluated_at"],
                "valid_until": gate["valid_until"],
                "upstream_priority": gate.get("upstream_priority"),
            },
            "target": {
                "market": advisory.market,
                "account_mode": advisory.target_account_mode,
                "broker_account_id": advisory.broker_account_id,
                "currency": advisory.currency,
                "symbol": advisory.symbol,
                "side": "buy",
            },
            "need": {
                "required_cash": canonical_decimal(Decimal(revision.required_cash)),
                "target_buying_power": canonical_decimal(
                    Decimal(revision.target_buying_power)
                ),
                "shortfall": canonical_decimal(Decimal(revision.shortfall)),
                "funding_needed": canonical_decimal(Decimal(revision.shortfall)),
                "other_pending_required": canonical_decimal(
                    Decimal(revision.other_pending_required)
                ),
                "reserved_cash": canonical_decimal(Decimal(revision.reserved_cash)),
                "operational_gap_including_other_pending": canonical_decimal(
                    Decimal(revision.operational_gap)
                ),
                "shortfall_scope": "this_candidate_only",
            },
            "routes": revision.routes,
            "combination": revision.combination,
            "safety": {
                "advisory_only": True,
                "executes_money_movement": False,
                "creates_proposal": False,
                "authoritative_for_order_gate": False,
            },
            "proposal_handoff": {
                "source_funding_advisory_id": str(advisory.advisory_id),
                "provenance_only": True,
                "classifier_input": False,
                "sizing_input": False,
                "eligibility_input": False,
                "action_label": "경로 설명 · 이 화면에서 주문 안 만듦",
                "ordinary_trim": "별도 create 확인 뒤 기존 dispatch 분류와 승인/veto 경로 적용",
                "loss_cut": "기존 loss_cut_intent 거절 규칙과 2-click nonce 유지",
            },
            "evaluated_at": revision.evaluated_at.isoformat(),
            "expires_at": revision.expires_at.isoformat(),
            "delivery": delivery,
        }

    async def get_detail(
        self, *, advisory_id: UUID, owner_user_id: int
    ) -> dict[str, Any]:
        advisory = await self._repository.get_advisory(
            advisory_id, owner_user_id=owner_user_id
        )
        if advisory is None:
            raise FundingAdvisoryNotFound(str(advisory_id))
        revision = await self._repository.latest_revision(advisory.advisory_id)
        if revision is None:
            raise FundingAdvisoryNotFound(f"{advisory_id}:revision")
        delivery_row = await self._repository.get_delivery(
            advisory_id=advisory.advisory_id,
            channel="telegram",
            kst_date=datetime.now(KST).date(),
        )
        delivery = (
            {
                "action": "none",
                "delivery_id": str(delivery_row.delivery_id),
                "state": delivery_row.state,
                "message_id": delivery_row.message_id,
            }
            if delivery_row is not None
            else {"action": "none", "state": "not_claimed"}
        )
        return self._projection(advisory, revision, delivery=delivery)

    async def refresh_detail(
        self, *, advisory_id: UUID, owner_user_id: int, now: datetime
    ) -> dict[str, Any]:
        advisory = await self._repository.get_advisory(
            advisory_id, owner_user_id=owner_user_id
        )
        if advisory is None:
            raise FundingAdvisoryNotFound(str(advisory_id))
        revision = await self._repository.latest_revision(advisory.advisory_id)
        if revision is None:
            raise FundingAdvisoryNotFound(f"{advisory_id}:revision")
        event = FundingCandidateEvent.model_validate(
            {
                "evidence": revision.evidence["gate"],
                "assessment": revision.evidence["assessment"],
            }
        )
        return await self._evaluate(event, now=now, event_kind="page_refresh")

    async def list_details(
        self, *, owner_user_id: int, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        advisories = await self._repository.list_advisories(
            owner_user_id=owner_user_id, state=state, limit=limit
        )
        rows: list[dict[str, Any]] = []
        for advisory in advisories:
            revision = await self._repository.latest_revision(advisory.advisory_id)
            if revision is not None:
                rows.append(
                    self._projection(
                        advisory,
                        revision,
                        delivery={"action": "none", "state": "not_evaluated"},
                    )
                )
        return rows

    async def cross_market_allocation(
        self, *, owner_user_id: int, now: datetime
    ) -> dict[str, Any]:
        details = await self.list_details(owner_user_id=owner_user_id, state="active")
        broker_by_account: dict[tuple[str, str, str], Decimal] = {}
        buckets: dict[str, dict[str, Any]] = {}
        for detail in details:
            target = detail["target"]
            need = detail["need"]
            currency = target["currency"]
            broker_by_account[
                (target["account_mode"], target["broker_account_id"], currency)
            ] = Decimal(need["target_buying_power"])
            bucket = buckets.setdefault(
                currency,
                {
                    "currency": currency,
                    "broker_confirmed_total_native": Decimal("0"),
                    "declared_total_native": Decimal("0"),
                    "conditional_total_native": Decimal("0"),
                    "display_total_native_including_declared": Decimal("0"),
                    "demands": [],
                    "contention": False,
                    "krw_equivalent": None,
                    "krw_equivalent_status": (
                        "native" if currency == "KRW" else "conversion_unavailable"
                    ),
                },
            )
            bucket["demands"].append(
                {
                    "advisory_id": detail["advisory_id"],
                    "market": target["market"],
                    "symbol": target["symbol"],
                    "shortfall": need["shortfall"],
                    "upstream_priority": detail["trigger"]["upstream_priority"],
                }
            )

        for (
            account_mode,
            broker_account_id,
            currency,
        ), amount in broker_by_account.items():
            _ = (account_mode, broker_account_id)
            buckets[currency]["broker_confirmed_total_native"] += amount

        external = await self._external_cash.list_current(
            owner_user_id=owner_user_id, now=now
        )
        for view in external:
            if view.status != "fresh" or view.current is None:
                continue
            currency = view.current.currency
            bucket = buckets.setdefault(
                currency,
                {
                    "currency": currency,
                    "broker_confirmed_total_native": Decimal("0"),
                    "declared_total_native": Decimal("0"),
                    "conditional_total_native": Decimal("0"),
                    "display_total_native_including_declared": Decimal("0"),
                    "demands": [],
                    "contention": False,
                    "krw_equivalent": None,
                    "krw_equivalent_status": (
                        "native" if currency == "KRW" else "conversion_unavailable"
                    ),
                },
            )
            bucket["declared_total_native"] += view.current.amount

        serialized: list[dict[str, Any]] = []
        for bucket in buckets.values():
            bucket["display_total_native_including_declared"] = (
                bucket["broker_confirmed_total_native"]
                + bucket["declared_total_native"]
            )
            bucket["contention"] = (
                bucket["declared_total_native"] > 0 and len(bucket["demands"]) > 1
            )
            for field in (
                "broker_confirmed_total_native",
                "declared_total_native",
                "conditional_total_native",
                "display_total_native_including_declared",
            ):
                bucket[field] = canonical_decimal(bucket[field])
            serialized.append(bucket)
        serialized.sort(key=lambda bucket: bucket["currency"])
        return {
            "buckets": serialized,
            "cross_currency_total": None,
            "cross_currency_total_status": "not_summed_without_executable_fx",
            "investment_priority_recomputed": False,
            "generated_at": _aware(now, "now").isoformat(),
        }


__all__ = [
    "FundingAdvisoryError",
    "FundingAdvisoryNotFound",
    "FundingAdvisoryService",
]
