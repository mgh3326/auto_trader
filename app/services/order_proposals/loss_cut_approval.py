"""B0 evidence and B1 web ceremony for existing loss-cut proposals.

This module never creates proposals and never imports or calls an order-submit
function.  B1 ends at a durable ``validated_no_execution`` event.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.symbol import to_db_symbol
from app.mcp_server.caller_identity import caller_agent_id_var
from app.schemas.loss_cut_approval import (
    LossCutBeginResponse,
    LossCutConfirmResponse,
    LossCutEvidenceField,
    LossCutEvidenceResponse,
    LossCutPositionEvidence,
)
from app.services.analyst_consensus_snapshots.repository import (
    AnalystConsensusSnapshotsRepository,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    CallbackEnvelope,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.revalidation import preview_loss_cut_confirmation

PreviewFn = Callable[..., Awaitable[dict[str, Any]]]
Clock = Callable[[], datetime]
TokenFactory = Callable[[], str]

_CONFIRMATION_TTL_SECONDS = 90


class LossCutApprovalRejected(Exception):
    """A fail-closed result whose audit event is safe to commit."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _ProposalEvidenceBundle:
    response: LossCutEvidenceResponse
    group: Any
    scope_payload: dict[str, Any]
    scope_hash: str
    evidence_hash: str
    requested_quantity: Decimal
    total_quantity: Decimal
    sellable_quantity: Decimal
    average_price: Decimal
    account_ref: str
    observed_at: datetime
    evidence_valid_until: datetime
    fingerprint: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal_text(value: Any) -> str:
    decimal = Decimal(str(value))
    text = format(decimal.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _market_key(market: str) -> str:
    return {
        "equity_kr": "kr",
        "equity_us": "us",
        "crypto": "crypto",
    }.get(market, market.strip().lower())


def _infer_market(symbol: str) -> str:
    if symbol.upper().startswith("KRW-"):
        return "crypto"
    if len(symbol) == 6 and symbol.isdigit():
        return "kr"
    return "us"


def _account_mode_for_source(source: str) -> str:
    return {
        "kis": "kis_live",
        "toss_api": "toss_live",
        "upbit": "upbit",
    }.get(source, source)


def _role_text(role: Any) -> str:
    return str(getattr(role, "value", role) or "")


class LossCutApprovalService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        home_service: Any | None = None,
        preview_fn: PreviewFn = preview_loss_cut_confirmation,
        clock: Clock = _utc_now,
        nonce_factory: TokenFactory = _token,
        ceremony_factory: TokenFactory = _token,
    ) -> None:
        self._session = session
        self._proposals = OrderProposalsService(session)
        self._home = home_service
        self._preview = preview_fn
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._ceremony_factory = ceremony_factory

    async def get_symbol_evidence(
        self, *, symbol: str, user_id: int
    ) -> LossCutEvidenceResponse:
        """B0: assemble evidence without touching proposal approval state."""
        now = self._clock()
        canonical_symbol = to_db_symbol(symbol.strip()).upper()
        if not canonical_symbol:
            raise OrderProposalError("loss_cut_evidence_symbol_required")
        if self._home is None:
            raise OrderProposalError("loss_cut_evidence_home_reader_unavailable")
        home = await self._home.get_home(user_id=user_id, include_paper=False)
        positions: list[LossCutPositionEvidence] = []
        loss_rows: list[dict[str, str]] = []
        for holding in home.holdings:
            if to_db_symbol(str(holding.symbol)).upper() != canonical_symbol:
                continue
            total = Decimal(str(holding.quantity))
            average = (
                Decimal(str(holding.averageCost))
                if holding.averageCost is not None
                else None
            )
            current = None
            if holding.valueNative is not None and total > 0:
                current = Decimal(str(holding.valueNative)) / total
            sellable_unavailable = holding.source == "toss_api" and not bool(
                getattr(settings, "toss_live_order_mutations_enabled", False)
            )
            sellable = (
                None
                if sellable_unavailable or holding.sellableQuantity is None
                else Decimal(str(holding.sellableQuantity))
            )
            source_status = (
                "unavailable"
                if sellable_unavailable or not holding.sourceOfTruth
                else "filled"
            )
            source_reason = (
                "sellable quantity is unavailable while this source is reference-only"
                if sellable_unavailable
                else "reference-only holding"
                if not holding.sourceOfTruth
                else None
            )
            positions.append(
                LossCutPositionEvidence(
                    account_ref=str(holding.accountId),
                    account_mode=_account_mode_for_source(str(holding.source)),
                    market=str(holding.market),
                    symbol=canonical_symbol,
                    total_quantity=_decimal_text(total),
                    sellable_quantity=(
                        _decimal_text(sellable) if sellable is not None else None
                    ),
                    pending_sell_quantity=_decimal_text(holding.pendingSellQuantity),
                    average_price=(
                        _decimal_text(average) if average is not None else None
                    ),
                    current_price=(
                        _decimal_text(current) if current is not None else None
                    ),
                    source=str(holding.source),
                    source_status=source_status,
                    source_reason=source_reason,
                    observed_at=now.isoformat(),
                )
            )
            if average is not None and average > 0 and current is not None:
                loss_rows.append(
                    {
                        "account_ref": str(holding.accountId),
                        "loss_pct": _decimal_text(
                            (current - average) / average * Decimal("100")
                        ),
                    }
                )
        market = _infer_market(canonical_symbol)
        consensus = await self._consensus_field(market=market, symbol=canonical_symbol)
        watch = await self._watch_field(market=market, symbol=canonical_symbol, now=now)
        return LossCutEvidenceResponse(
            mode="symbol",
            symbol=canonical_symbol,
            generated_at=now.isoformat(),
            can_begin=False,
            positions=positions,
            loss=LossCutEvidenceField(
                status="filled" if loss_rows else "missing",
                label="손실률",
                value={"positions": loss_rows} if loss_rows else None,
                reason=None
                if loss_rows
                else "fresh price and average cost are required",
                source="invest_home",
                as_of=now.isoformat(),
            ),
            reason=LossCutEvidenceField(
                status="missing",
                label="사유 판정",
                reason="B0 has no proposal-bound loss-cut reason",
            ),
            r931=self._r931_field(),
            consensus=consensus,
            watch=watch,
            warnings=(
                ["same-symbol positions remain separated by account"]
                if len(positions) > 1
                else []
            ),
        )

    async def get_proposal_evidence(
        self, *, proposal_id: uuid.UUID
    ) -> LossCutEvidenceResponse:
        return (await self._build_proposal_bundle(proposal_id)).response

    async def begin(
        self,
        *,
        proposal_id: uuid.UUID,
        actor_user_id: int,
        actor_role: Any,
    ) -> LossCutBeginResponse:
        now = self._clock()
        callback = await self._proposals.current_callback_envelope(
            proposal_id, action="op"
        )
        await self._proposals.preflight_published_proposal_callback(
            proposal_id, callback=callback
        )
        bundle = await self._build_proposal_bundle(proposal_id, now=now)
        if not bundle.response.can_begin:
            raise OrderProposalError("loss_cut_web_begin_not_available")

        ceremony_id = self._ceremony_factory()
        ceremony_digest = _secret_digest(ceremony_id)
        confirmation_nonce = self._nonce_factory()
        actor_subject = str(actor_user_id)
        try:
            await self._proposals.consume_published_proposal_callback(
                proposal_id,
                callback=callback,
                now=now,
            )
        except OrderProposalError as exc:
            await self._proposals.append_approval_event(
                event_id=uuid.uuid4(),
                proposal_pk=bundle.group.id,
                ceremony_digest=ceremony_digest,
                channel="web",
                step="begin",
                outcome="rejected",
                actor_kind="user",
                actor_subject=actor_subject,
                actor_role=_role_text(actor_role),
                dispatch_attempt_id=callback.attempt_id,
                membership_revision=callback.membership_revision,
                membership_digest=callback.membership_digest,
                nonce_digest=_secret_digest(callback.nonce),
                proposal_payload_hash=bundle.group.payload_hash,
                scope_hash=bundle.scope_hash,
                evidence_hash=bundle.evidence_hash,
                evidence_snapshot=bundle.response.model_dump(mode="json"),
                reason_code=str(exc),
                observed_at=now,
            )
            raise LossCutApprovalRejected(str(exc)) from exc

        await self._proposals.issue_loss_cut_confirmation(
            proposal_id,
            first_nonce=callback.nonce,
            confirmation_nonce=confirmation_nonce,
            actor_channel="web",
            actor_subject=actor_subject,
            now=now,
            ttl_seconds=_CONFIRMATION_TTL_SECONDS,
        )
        group, _rungs = await self._proposals.get_proposal(proposal_id)
        attempt_id = uuid.uuid4()
        binding = build_proposal_dispatch_binding(
            proposal_id=proposal_id,
            nonce=confirmation_nonce,
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.LOSS_CUT_CONFIRMATION,
            current_membership_revision=group.approval_dispatch_membership_revision,
        )
        await self._proposals.start_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            binding=binding,
            now=now,
            payload_chars=0,
            context_message_count=0,
            channel="web",
            scope_hash=bundle.scope_hash,
            evidence_hash=bundle.evidence_hash,
            publication_ref_digest=ceremony_digest,
        )
        await self._proposals.finish_web_approval_dispatch(
            proposal_id, attempt_id=attempt_id, now=now
        )
        await self._proposals.upsert_loss_cut_scope(
            proposal_pk=group.id,
            schema_version=1,
            account_ref=bundle.account_ref,
            account_mode=group.account_mode,
            market=group.market,
            symbol=group.symbol,
            requested_quantity=bundle.requested_quantity,
            observed_total_quantity=bundle.total_quantity,
            observed_sellable_quantity=bundle.sellable_quantity,
            average_price=bundle.average_price,
            position_scope=bundle.scope_payload,
            source=f"order_preview:{group.account_mode}",
            observed_at=bundle.observed_at,
            decision_observed_at=group.created_at,
            evidence_valid_until=bundle.evidence_valid_until,
            scope_hash=bundle.scope_hash,
            evidence_hash=bundle.evidence_hash,
        )
        await self._proposals.append_approval_event(
            event_id=uuid.uuid4(),
            proposal_pk=group.id,
            ceremony_digest=ceremony_digest,
            channel="web",
            step="begin",
            outcome="accepted",
            actor_kind="user",
            actor_subject=actor_subject,
            actor_role=_role_text(actor_role),
            dispatch_attempt_id=attempt_id,
            membership_revision=binding.membership_revision,
            membership_digest=binding.membership_digest,
            nonce_digest=_secret_digest(confirmation_nonce),
            proposal_payload_hash=group.payload_hash,
            scope_hash=bundle.scope_hash,
            evidence_hash=bundle.evidence_hash,
            evidence_snapshot=bundle.response.model_dump(mode="json"),
            expires_at=bundle.evidence_valid_until,
            observed_at=now,
        )
        return LossCutBeginResponse(
            proposal_id=str(proposal_id),
            ceremony_id=ceremony_id,
            expires_at=bundle.evidence_valid_until.isoformat(),
            evidence=bundle.response,
            fingerprint=bundle.fingerprint,
        )

    async def confirm(
        self,
        *,
        proposal_id: uuid.UUID,
        ceremony_id: str,
        actor_user_id: int,
        actor_role: Any,
    ) -> LossCutConfirmResponse:
        now = self._clock()
        group, _rungs = await self._proposals.get_proposal(proposal_id)
        ceremony_digest = _secret_digest(ceremony_id)
        begin_event = await self._proposals.get_approval_event(
            proposal_pk=group.id,
            ceremony_digest=ceremony_digest,
            step="begin",
        )
        if begin_event is None or begin_event.outcome != "accepted":
            raise OrderProposalError("loss_cut_web_ceremony_not_found")
        confirm_event = await self._proposals.get_approval_event(
            proposal_pk=group.id,
            ceremony_digest=ceremony_digest,
            step="confirm",
        )
        if confirm_event is not None:
            raise OrderProposalError("nonce_replay")
        actor_subject = str(actor_user_id)
        if (
            begin_event.channel != "web"
            or begin_event.actor_kind != "user"
            or begin_event.actor_subject != actor_subject
        ):
            raise OrderProposalError("loss_cut_confirmation_principal_mismatch")
        if begin_event.expires_at is None or now >= begin_event.expires_at:
            await self._append_confirm_event(
                begin_event=begin_event,
                group=group,
                actor_subject=actor_subject,
                actor_role=actor_role,
                outcome="expired",
                reason_code="loss_cut_confirmation_expired",
                observed_at=now,
            )
            raise LossCutApprovalRejected("loss_cut_confirmation_expired")

        # This transaction-scoped target lock is deliberately acquired before
        # the proposal row lock taken by consume_published_proposal_callback.
        await self._proposals.acquire_target_mutation_lock(group)
        refreshed_group, _ = await self._proposals.get_proposal(proposal_id)
        nonce = str(refreshed_group.approval_nonce or "")
        if (
            not nonce
            or begin_event.dispatch_attempt_id is None
            or begin_event.membership_revision is None
            or begin_event.membership_digest is None
            or begin_event.nonce_digest != _secret_digest(nonce)
        ):
            raise OrderProposalError("loss_cut_web_binding_mismatch")
        callback = CallbackEnvelope(
            action="lc",
            subject_short=str(proposal_id)[:8],
            attempt_id=begin_event.dispatch_attempt_id,
            membership_revision=begin_event.membership_revision,
            membership_digest=begin_event.membership_digest,
            nonce=nonce,
        )
        await self._proposals.consume_published_proposal_callback(
            proposal_id,
            callback=callback,
            now=now,
            actor_channel="web",
            actor_subject=actor_subject,
        )
        try:
            fresh = await self._build_proposal_bundle(proposal_id, now=now)
        except OrderProposalError as exc:
            await self._append_confirm_event(
                begin_event=begin_event,
                group=group,
                actor_subject=actor_subject,
                actor_role=actor_role,
                outcome="needs_reconfirm",
                reason_code=(
                    f"loss_cut_confirmation_revalidation_failed:{str(exc)[:200]}"
                ),
                observed_at=now,
            )
            raise LossCutApprovalRejected(
                "loss_cut_confirmation_revalidation_failed"
            ) from exc
        scope = await self._proposals.get_loss_cut_scope(group.id)
        scope_matches = bool(
            scope is not None
            and scope.scope_hash == begin_event.scope_hash == fresh.scope_hash
            and scope.evidence_hash == begin_event.evidence_hash == fresh.evidence_hash
            and refreshed_group.approval_dispatch_scope_hash == fresh.scope_hash
            and refreshed_group.approval_dispatch_evidence_hash == fresh.evidence_hash
        )
        if not scope_matches:
            await self._append_confirm_event(
                begin_event=begin_event,
                group=group,
                actor_subject=actor_subject,
                actor_role=actor_role,
                outcome="needs_reconfirm",
                reason_code="loss_cut_confirmation_scope_or_evidence_changed",
                observed_at=now,
                fresh=fresh,
            )
            raise LossCutApprovalRejected(
                "loss_cut_confirmation_scope_or_evidence_changed"
            )

        await self._proposals.record_channel_approval(
            proposal_id,
            channel="web",
            actor_subject=actor_subject,
            now=now,
        )
        await self._append_confirm_event(
            begin_event=begin_event,
            group=group,
            actor_subject=actor_subject,
            actor_role=actor_role,
            outcome="accepted",
            observed_at=now,
            fresh=fresh,
        )
        return LossCutConfirmResponse(
            proposal_id=str(proposal_id),
            evidence=fresh.response,
            fingerprint=fresh.fingerprint,
        )

    async def _append_confirm_event(
        self,
        *,
        begin_event: Any,
        group: Any,
        actor_subject: str,
        actor_role: Any,
        outcome: str,
        observed_at: datetime,
        reason_code: str | None = None,
        fresh: _ProposalEvidenceBundle | None = None,
    ) -> None:
        await self._proposals.append_approval_event(
            event_id=uuid.uuid4(),
            proposal_pk=group.id,
            ceremony_digest=begin_event.ceremony_digest,
            channel="web",
            step="confirm",
            outcome=outcome,
            actor_kind="user",
            actor_subject=actor_subject,
            actor_role=_role_text(actor_role),
            dispatch_attempt_id=begin_event.dispatch_attempt_id,
            membership_revision=begin_event.membership_revision,
            membership_digest=begin_event.membership_digest,
            nonce_digest=begin_event.nonce_digest,
            proposal_payload_hash=group.payload_hash,
            scope_hash=fresh.scope_hash
            if fresh is not None
            else begin_event.scope_hash,
            evidence_hash=(
                fresh.evidence_hash if fresh is not None else begin_event.evidence_hash
            ),
            evidence_snapshot=(
                fresh.response.model_dump(mode="json")
                if fresh is not None
                else begin_event.evidence_snapshot
            ),
            reason_code=reason_code,
            expires_at=begin_event.expires_at,
            observed_at=observed_at,
        )

    async def _build_proposal_bundle(
        self, proposal_id: uuid.UUID, *, now: datetime | None = None
    ) -> _ProposalEvidenceBundle:
        observed_at = now or self._clock()
        group, rungs = await self._proposals.get_proposal(proposal_id)
        if group.exit_intent != "loss_cut" or group.retrospective_id is None:
            raise OrderProposalError("loss_cut_confirmation_requires_loss_cut")
        if group.side != "sell" or group.order_type != "limit":
            raise OrderProposalError("loss_cut_confirmation_order_shape_invalid")
        submit_agent_id = settings.ORDER_PROPOSALS_SUBMIT_AGENT_ID.strip() or None
        caller_agent_id_token = caller_agent_id_var.set(submit_agent_id)
        try:
            preview = await self._preview(
                service=self._proposals,
                proposal_id=proposal_id,
                now=observed_at,
            )
        finally:
            caller_agent_id_var.reset(caller_agent_id_token)
        preview_rungs = sorted(
            preview.get("rungs") or [], key=lambda item: int(item["rung_index"])
        )
        if not preview_rungs:
            raise OrderProposalError("loss_cut_confirmation_has_no_eligible_rungs")
        requested_quantity = sum(
            (Decimal(str(item["requested_quantity"])) for item in preview_rungs),
            Decimal("0"),
        )
        sellable_values = {
            Decimal(str(item["observed_sellable_qty"])) for item in preview_rungs
        }
        total_values = {
            Decimal(str(item["observed_total_qty"])) for item in preview_rungs
        }
        average_values = {Decimal(str(item["avg_buy_price"])) for item in preview_rungs}
        if (
            len(sellable_values) != 1
            or len(total_values) != 1
            or len(average_values) != 1
        ):
            raise OrderProposalError("loss_cut_confirmation_position_scope_ambiguous")
        sellable_quantity = sellable_values.pop()
        total_quantity = total_values.pop()
        average_price = average_values.pop()
        if requested_quantity > sellable_quantity:
            raise OrderProposalError("loss_cut_confirmation_quantity_exceeds_sellable")
        account_ref = str(group.broker_account_id or "").strip()
        if not account_ref:
            raise OrderProposalError("loss_cut_confirmation_account_scope_missing")
        evidence_valid_until = observed_at + timedelta(
            seconds=_CONFIRMATION_TTL_SECONDS
        )
        if group.valid_until is not None:
            evidence_valid_until = min(evidence_valid_until, group.valid_until)
        rung_revisions = {
            rung.rung_index: int(rung.approval_revision or 0) for rung in rungs
        }
        scope_rungs = [
            {
                "rung_index": int(item["rung_index"]),
                "approval_revision": rung_revisions[int(item["rung_index"])],
                "quantity": _decimal_text(item["requested_quantity"]),
                "limit_price": _decimal_text(item["limit_price"]),
            }
            for item in preview_rungs
        ]
        scope_payload = {
            "schema_version": 1,
            "proposal_id": str(group.proposal_id),
            "proposal_revision": group.revision,
            "proposal_payload_hash": group.payload_hash,
            "account_ref": account_ref,
            "account_mode": group.account_mode,
            "market": group.market,
            "symbol": group.symbol,
            "side": group.side,
            "order_type": group.order_type,
            "requested_quantity": _decimal_text(requested_quantity),
            "observed_total_quantity": _decimal_text(total_quantity),
            "observed_sellable_quantity": _decimal_text(sellable_quantity),
            "average_price": _decimal_text(average_price),
            "rungs": scope_rungs,
        }
        scope_hash = _digest(scope_payload)
        market = _market_key(group.market)
        consensus = await self._consensus_field(market=market, symbol=group.symbol)
        watch = await self._watch_field(
            market=market, symbol=group.symbol, now=observed_at
        )
        r931 = self._r931_field()
        loss = LossCutEvidenceField(
            status="filled",
            label="손실률",
            value={"rungs": preview_rungs},
            source=f"order_preview:{group.account_mode}",
            as_of=observed_at.isoformat(),
            valid_until=evidence_valid_until.isoformat(),
        )
        reason = LossCutEvidenceField(
            status="filled",
            label="사유 판정",
            value={
                "exit_reason": group.exit_reason,
                "retrospective_id": group.retrospective_id,
                "trigger_type": preview.get("retrospective_trigger_type"),
                "retrospective_created_at": preview.get("retrospective_created_at"),
                "lesson_excerpt": preview.get("lesson_excerpt"),
            },
            source="review.trade_retrospectives",
            as_of=preview.get("retrospective_created_at"),
        )
        stable_preview_rungs = [
            {key: value for key, value in item.items() if key != "quote_observed_at"}
            for item in preview_rungs
        ]
        evidence_binding = {
            "scope_hash": scope_hash,
            # Observation time advances between clicks by definition. Bind the
            # fresh values and warnings, while expiry is enforced separately.
            "loss": {"rungs": stable_preview_rungs},
            "reason": reason.value,
            "r931": {"status": r931.status, "value": r931.value},
            "consensus": {
                "status": consensus.status,
                "value": consensus.value,
                "as_of": consensus.as_of,
            },
            "watch": {
                "status": watch.status,
                "value": watch.value,
                "as_of": watch.as_of,
            },
        }
        evidence_hash = _digest(evidence_binding)
        proposal_age_seconds = max(
            int((observed_at - group.created_at).total_seconds()), 0
        )
        fingerprint = {
            "proposal_id": str(group.proposal_id),
            "proposal_revision": group.revision,
            "proposal_payload_hash": group.payload_hash,
            "position_scope_hash": scope_hash,
            "evidence_hash": evidence_hash,
            "account_ref": account_ref,
            "account_mode": group.account_mode,
            "market": group.market,
            "symbol": group.symbol,
            "side": group.side,
            "order_type": group.order_type,
            "rungs": scope_rungs,
            "current_prices": [item["current_price"] for item in preview_rungs],
            "fill_distances": [item.get("fill_distance") for item in preview_rungs],
            "quote_observed_at": observed_at.isoformat(),
            "proposal_age_seconds": proposal_age_seconds,
            "evidence_valid_until": evidence_valid_until.isoformat(),
            "execution": "disabled_b1",
        }
        position = LossCutPositionEvidence(
            account_ref=account_ref,
            account_mode=group.account_mode,
            market=group.market,
            symbol=group.symbol,
            total_quantity=_decimal_text(total_quantity),
            sellable_quantity=_decimal_text(sellable_quantity),
            pending_sell_quantity=_decimal_text(total_quantity - sellable_quantity),
            average_price=_decimal_text(average_price),
            current_price=str(preview_rungs[0]["current_price"]),
            source=f"order_preview:{group.account_mode}",
            source_status="filled",
            observed_at=observed_at.isoformat(),
        )
        can_begin = bool(
            group.approval_dispatch_state == ApprovalDispatchState.SENT_CURRENT.value
            and group.approval_dispatch_card_kind
            in {ApprovalCardKind.MANUAL.value, ApprovalCardKind.RECONFIRM.value}
            and group.approval_nonce
            and group.approval_nonce_used_at is None
            and observed_at < evidence_valid_until
        )
        response = LossCutEvidenceResponse(
            mode="proposal",
            symbol=group.symbol,
            proposal_id=str(group.proposal_id),
            generated_at=observed_at.isoformat(),
            can_begin=can_begin,
            positions=[position],
            loss=loss,
            reason=reason,
            r931=r931,
            consensus=consensus,
            watch=watch,
            fingerprint=fingerprint,
        )
        return _ProposalEvidenceBundle(
            response=response,
            group=group,
            scope_payload=scope_payload,
            scope_hash=scope_hash,
            evidence_hash=evidence_hash,
            requested_quantity=requested_quantity,
            total_quantity=total_quantity,
            sellable_quantity=sellable_quantity,
            average_price=average_price,
            account_ref=account_ref,
            observed_at=observed_at,
            evidence_valid_until=evidence_valid_until,
            fingerprint=fingerprint,
        )

    async def _consensus_field(
        self, *, market: str, symbol: str
    ) -> LossCutEvidenceField:
        if market not in {"kr", "us"}:
            return LossCutEvidenceField(
                status="unavailable",
                label="컨센서스",
                reason="durable consensus snapshots support kr/us equities only",
                source="analyst_consensus_snapshots",
            )
        try:
            row = await AnalystConsensusSnapshotsRepository(
                self._session
            ).latest_for_symbol(market=market, symbol=to_db_symbol(symbol).upper())
        except Exception as exc:  # noqa: BLE001 - read evidence degrades explicitly
            return LossCutEvidenceField(
                status="source_error",
                label="컨센서스",
                reason=type(exc).__name__,
                source="analyst_consensus_snapshots",
            )
        if row is None:
            return LossCutEvidenceField(
                status="missing",
                label="컨센서스",
                reason="no per-symbol durable snapshot",
                source="analyst_consensus_snapshots",
            )
        return LossCutEvidenceField(
            status="filled",
            label="컨센서스",
            value={
                "buy_count": row.buy_count,
                "hold_count": row.hold_count,
                "sell_count": row.sell_count,
                "total_count": row.total_count,
                "target_mean": (
                    _decimal_text(row.target_mean)
                    if row.target_mean is not None
                    else None
                ),
                "upside_pct": (
                    _decimal_text(row.upside_pct)
                    if row.upside_pct is not None
                    else None
                ),
                "analyst_count": row.analyst_count,
                "newest_opinion_date": (
                    row.newest_opinion_date.isoformat()
                    if row.newest_opinion_date is not None
                    else None
                ),
            },
            source=f"analyst_consensus_snapshots:{row.source}",
            as_of=row.snapshot_date.isoformat(),
        )

    async def _watch_field(
        self, *, market: str, symbol: str, now: datetime
    ) -> LossCutEvidenceField:
        if market not in {"kr", "us", "crypto"}:
            return LossCutEvidenceField(
                status="unavailable",
                label="워치 맥락",
                reason="unsupported market",
                source="review.investment_watch_alerts",
            )
        try:
            rows = await InvestmentReportsRepository(self._session).list_active_alerts(
                market=market,
                symbol=to_db_symbol(symbol).upper(),
                valid_at=now,
                limit=20,
            )
        except Exception as exc:  # noqa: BLE001 - read evidence degrades explicitly
            return LossCutEvidenceField(
                status="source_error",
                label="워치 맥락",
                reason=type(exc).__name__,
                source="review.investment_watch_alerts",
            )
        if not rows:
            return LossCutEvidenceField(
                status="missing",
                label="워치 맥락",
                reason="not-registered",
                source="review.investment_watch_alerts",
            )
        ordered_rows = sorted(
            rows,
            key=lambda row: (row.activated_at, row.id),
            reverse=True,
        )
        values = [
            {
                "alert_uuid": str(row.alert_uuid),
                "intent": row.intent,
                "rationale": row.rationale,
                "metric": row.metric,
                "operator": row.operator,
                "threshold": _decimal_text(row.threshold),
                "action_mode": row.action_mode,
                "valid_until": row.valid_until.isoformat(),
            }
            for row in ordered_rows
        ]
        return LossCutEvidenceField(
            status="filled",
            label="워치 맥락",
            value={"alerts": values},
            source="review.investment_watch_alerts",
            as_of=max(row.updated_at for row in rows).isoformat(),
            valid_until=max(row.valid_until for row in rows).isoformat(),
        )

    @staticmethod
    def _r931_field() -> LossCutEvidenceField:
        return LossCutEvidenceField(
            status="unavailable",
            label="R-931",
            reason="no durable typed producer is registered",
            source="not-recorded",
        )


__all__ = [
    "LossCutApprovalRejected",
    "LossCutApprovalService",
]
