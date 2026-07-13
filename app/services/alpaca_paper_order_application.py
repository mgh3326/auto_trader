"""Guarded Alpaca Crypto Paper application boundary for ROB-845.

The common paper façade and MCP handlers enter Alpaca through this service. It
builds and persists approval packets in the existing Alpaca native ledger and
delegates every real POST to :class:`AlpacaPaperSubmitCoordinator`. It owns no
additional lifecycle state and never imports MCP tooling or a live endpoint.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import (
    KNOWN_OPEN_BROKER_STATUSES,
    AlpacaPaperLedgerService,
    normalize_known_broker_order_status,
)
from app.services.alpaca_paper_market_evidence import (
    MarketEvidenceError,
    hard_notional_cap,
    load_market_evidence,
)
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    SubmitOutcome,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.crypto_execution_mapping import (
    map_alpaca_paper_to_binance_public_spot,
)
from app.services.paper_approval_packet import (
    PaperApprovalPacket,
    PaperApprovalPacketError,
    verify_order_within_packet,
    verify_packet_market_data,
    verify_sell_packet_source,
)

ALPACA_CRYPTO_PAPER_MAX_NOTIONAL = Decimal("50")
DEFAULT_PACKET_TTL = timedelta(minutes=5)
DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=5)

SessionFactory = Callable[[], async_sessionmaker[AsyncSession] | AsyncSession]
BrokerFactory = Callable[[], AlpacaPaperBrokerService]


def _default_broker_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


@dataclass(frozen=True)
class AlpacaPaperOrderSpec:
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit"]
    qty: Decimal | None
    notional: Decimal | None
    time_in_force: Literal["gtc", "ioc"]
    limit_price: Decimal
    asset_class: Literal["crypto"] = "crypto"

    def __post_init__(self) -> None:
        if self.symbol not in {"BTC/USD", "ETH/USD"}:
            raise ValueError("Alpaca Crypto Paper V1 supports BTC/USD and ETH/USD")
        if self.order_type != "limit":
            raise ValueError("Alpaca Crypto Paper V1 is limit-only")
        if self.time_in_force not in {"gtc", "ioc"}:
            raise ValueError("Alpaca Crypto Paper V1 supports gtc/ioc")
        if self.qty is None or self.notional is not None:
            raise ValueError("Alpaca Crypto Paper V1 requires qty sizing")
        if not self.qty.is_finite() or self.qty <= 0:
            raise ValueError("qty must be finite and positive")
        if not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("limit_price must be finite and positive")

    def canonical(self) -> dict[str, Any]:
        return build_canonical_payload(
            symbol=self.symbol,
            side=self.side,
            type=self.order_type,
            time_in_force=self.time_in_force,
            qty=self.qty,
            notional=self.notional,
            limit_price=self.limit_price,
            asset_class=self.asset_class,
        )


@dataclass(frozen=True)
class AlpacaVerifiedDecision:
    order: AlpacaPaperOrderSpec
    decision_id: str
    signal_symbol: str
    signal_venue: Literal["binance_public_spot"]
    snapshot_id: str
    snapshot_hash: str
    snapshot_as_of: datetime
    snapshot_source: str
    reference_price: Decimal
    source_buy_client_order_id: str | None
    decision_identity_hash: str

    def __post_init__(self) -> None:
        required = {
            "decision_id": self.decision_id,
            "signal_symbol": self.signal_symbol,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_source": self.snapshot_source,
            "decision_identity_hash": self.decision_identity_hash,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.snapshot_as_of.tzinfo is None:
            raise ValueError("snapshot_as_of must be timezone-aware")
        if not self.reference_price.is_finite() or self.reference_price <= 0:
            raise ValueError("reference_price must be finite and positive")
        expected_signal = map_alpaca_paper_to_binance_public_spot(self.order.symbol)
        if self.signal_symbol != expected_signal:
            raise ValueError("signal symbol does not match execution symbol")
        if (
            self.order.side == "sell"
            and not (self.source_buy_client_order_id or "").strip()
        ):
            raise ValueError("sell requires source_buy_client_order_id")


@dataclass(frozen=True)
class AlpacaPaperApplicationOutcome:
    status: str
    reason_code: str | None = None
    native_client_order_id: str | None = None
    native_order_id: str | None = None
    submitted: bool = False
    broker_called: bool = False
    replayed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    message: str | None = None


class AlpacaPaperOrderApplication:
    """Packet, native-ledger, and coordinator boundary for verified decisions."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = AsyncSessionLocal,  # type: ignore[assignment]
        broker_factory: BrokerFactory = _default_broker_factory,
        now_fn: Callable[[], datetime] | None = None,
        quote_max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
    ) -> None:
        self._session_factory = session_factory
        self._broker_factory = broker_factory
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._quote_max_age = quote_max_age

    async def preview_trusted_snapshot(
        self,
        *,
        canonical: dict[str, Any],
        quote_snapshot_id: int,
        valid_for_seconds: int,
    ) -> dict[str, Any]:
        """Compatibility entry for the existing automated MCP preview.

        Identity and quote evidence are reloaded from the opaque native snapshot;
        callers still cannot provide correlation, origin, ceiling, or client ID.
        """
        if str(canonical.get("side") or "") == "sell":
            return self._legacy_rejected(
                None,
                "automated_sell_disabled",
                "legacy automated sell has no verified native buy authority",
            )
        now = self._now_fn()
        asset_class = str(canonical.get("asset_class") or "")
        symbol = str(canonical.get("symbol") or "")
        async with self._session_factory() as db:
            try:
                evidence = await load_market_evidence(
                    db,
                    quote_snapshot_id,
                    execution_symbol=symbol,
                    asset_class=asset_class,
                    now=now,
                    max_age=self._quote_max_age,
                )
            except MarketEvidenceError as exc:
                return self._legacy_rejected(None, exc.code, str(exc))

            max_notional = hard_notional_cap(asset_class)
            client_order_id = derive_automated_key(
                correlation_id=evidence.correlation_id,
                snapshot_id=evidence.snapshot_id,
                canonical=canonical,
            )
            try:
                packet = PaperApprovalPacket(
                    signal_source="automated_preview",
                    artifact_id=uuid.uuid4(),
                    signal_symbol=evidence.signal_symbol,
                    signal_venue="upbit",
                    execution_symbol=symbol,
                    execution_venue="alpaca_paper",
                    execution_asset_class=asset_class,
                    side=str(canonical.get("side") or ""),
                    max_notional=max_notional,
                    max_qty=None,
                    qty_source="notional_estimate",
                    expected_lifecycle_step="previewed",
                    lifecycle_correlation_id=evidence.correlation_id,
                    client_order_id=client_order_id,
                    expires_at=now + timedelta(seconds=max(1, int(valid_for_seconds))),
                    account_mode="alpaca_paper",
                    origin="automated",
                    market_data_asof=evidence.market_data_asof,
                    market_data_source=evidence.market_data_source,
                    preview_payload_hash=canonical_hash(canonical),
                    snapshot_id=evidence.snapshot_id,
                    execution_order_type=str(canonical.get("type") or ""),
                    execution_time_in_force=canonical.get("time_in_force"),
                    reference_price=evidence.price,
                )
                verify_packet_market_data(packet, now=now, max_age=self._quote_max_age)
                verify_order_within_packet(packet, canonical)
            except (PaperApprovalPacketError, ValueError) as exc:
                code = getattr(exc, "code", "invalid_packet")
                return self._legacy_rejected(client_order_id, code, str(exc))

            packet_dict = packet.model_dump(mode="json")
            provenance = {
                "quote_snapshot_id": evidence.quote_snapshot_id,
                "snapshot_content_hash": evidence.content_hash,
                "market_data_source": evidence.market_data_source,
                "reference_price": str(evidence.price),
                "policy_max_notional": str(max_notional),
                "packet_hash": self._packet_hash(packet_dict)[:16],
            }
            ledger = AlpacaPaperLedgerService(db)
            await ledger.record_preview(
                client_order_id=client_order_id,
                lifecycle_correlation_id=evidence.correlation_id,
                execution_symbol=symbol,
                execution_venue="alpaca_paper",
                execution_asset_class=asset_class,
                instrument_type=(
                    InstrumentType.crypto
                    if asset_class == "crypto"
                    else InstrumentType.equity_us
                ),
                side=str(canonical.get("side") or ""),
                order_type=str(canonical.get("type") or ""),
                time_in_force=canonical.get("time_in_force"),
                requested_qty=self._decimal_or_none(canonical.get("qty")),
                requested_notional=self._decimal_or_none(canonical.get("notional")),
                requested_price=self._decimal_or_none(canonical.get("limit_price")),
                preview_payload={
                    "canonical": canonical,
                    "approval_packet": packet_dict,
                    "provenance": provenance,
                },
            )
            db.expire_all()
            persisted = await ledger.get_preview_by_client_order_id(client_order_id)
            stored = getattr(persisted, "preview_payload", None) or {}
            stored_packet = stored.get("approval_packet", {})
            stored_provenance = stored.get("provenance", provenance)
            stored_canonical = stored.get("canonical", canonical)
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": False,
            "preview": True,
            "approval_token": client_order_id,
            "client_order_id": client_order_id,
            "expires_at": stored_packet.get(
                "expires_at", packet.expires_at.isoformat()
            ),
            "order_request": stored_canonical,
            "provenance": stored_provenance,
        }

    async def submit_token(
        self, *, approval_token: str, confirm: bool
    ) -> dict[str, Any]:
        """Compatibility entry that submits only a persisted opaque token."""
        token = (approval_token or "").strip()
        if not token:
            raise ValueError("approval_token is required")
        async with self._session_factory() as db:
            ledger = AlpacaPaperLedgerService(db)
            preview = await ledger.get_preview_by_client_order_id(token)
            if preview is None:
                return self._legacy_rejected(
                    token,
                    "no_preview_for_token",
                    "no persisted preview for approval_token",
                )
            payload = preview.preview_payload or {}
            canonical = payload.get("canonical")
            packet_dict = payload.get("approval_packet")
            if not isinstance(canonical, dict) or not isinstance(packet_dict, dict):
                return self._legacy_rejected(
                    token, "malformed_preview", "preview payload is malformed"
                )
            try:
                packet = PaperApprovalPacket(**packet_dict)
            except ValueError as exc:
                return self._legacy_rejected(token, "malformed_preview", str(exc))
            if confirm is not True:
                reason: str | None = None
                try:
                    now = self._now_fn()
                    from app.services.paper_approval_packet import (
                        verify_packet_freshness,
                    )

                    verify_packet_freshness(packet, now=now)
                    verify_packet_market_data(
                        packet, now=now, max_age=self._quote_max_age
                    )
                except PaperApprovalPacketError as exc:
                    reason = exc.code
                return {
                    "success": True,
                    "account_mode": "alpaca_paper",
                    "source": "alpaca_paper",
                    "submitted": False,
                    "blocked_reason": "confirmation_required",
                    "client_order_id": token,
                    "would_reject_reason": reason,
                    "order_request": canonical,
                }
            outcome = await AlpacaPaperSubmitCoordinator(
                ledger,
                self._broker_factory,
                now_fn=self._now_fn,
                quote_max_age=self._quote_max_age,
            ).submit(packet, submit_canonical=canonical)
            return self._legacy_submit_result(outcome)

    async def preview(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome:
        async with self._session_factory() as db:
            prepared = await self._prepare(db, decision, enforce_dynamic_checks=True)
            if isinstance(prepared, AlpacaPaperApplicationOutcome):
                return prepared
            packet, canonical = prepared
            return AlpacaPaperApplicationOutcome(
                status="previewed",
                native_client_order_id=packet.client_order_id,
                evidence={
                    "order_request": canonical,
                    "snapshot_hash": decision.snapshot_hash,
                    "decision_identity_hash": decision.decision_identity_hash,
                    "approval_token": packet.client_order_id,
                    "expires_at": packet.expires_at.isoformat(),
                },
            )

    async def submit(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome:
        async with self._session_factory() as db:
            # The coordinator owns replay-before-freshness/source ordering. Do
            # not reject a previously terminal native result merely because its
            # original snapshot or source evidence is no longer fresh/readable.
            prepared = await self._prepare(db, decision, enforce_dynamic_checks=False)
            if isinstance(prepared, AlpacaPaperApplicationOutcome):
                return prepared
            packet, canonical = prepared
            coordinator = AlpacaPaperSubmitCoordinator(
                AlpacaPaperLedgerService(db),
                self._broker_factory,
                now_fn=self._now_fn,
                quote_max_age=self._quote_max_age,
            )
            outcome = await coordinator.submit(packet, submit_canonical=canonical)
            return self._from_submit_outcome(outcome)

    async def get_order(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome:
        canonical = decision.order.canonical()
        client_order_id = self._client_order_id(decision, canonical)
        async with self._session_factory() as db:
            row = await AlpacaPaperLedgerService(db).get_execution_by_client_order_id(
                client_order_id
            )
            if row is None:
                return self._rejected(
                    client_order_id,
                    "native_order_not_found",
                    "native Alpaca paper execution was not found",
                )
            return self._row_outcome(row)

    async def cancel(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome:
        canonical = decision.order.canonical()
        client_order_id = self._client_order_id(decision, canonical)
        async with self._session_factory() as db:
            ledger = AlpacaPaperLedgerService(db)
            row = await ledger.get_execution_by_client_order_id(client_order_id)
            broker_order_id = getattr(row, "broker_order_id", None) if row else None
            if not broker_order_id:
                return self._rejected(
                    client_order_id,
                    "native_order_not_found",
                    "cancel requires a submitted native Alpaca paper order",
                )
            persisted_status = normalize_known_broker_order_status(
                getattr(row, "order_status", None)
            )
            if persisted_status == "canceled":
                if str(getattr(row, "cancel_status", "") or "").lower() != "canceled":
                    await ledger.record_cancel(
                        client_order_id, cancel_status="canceled"
                    )
                return AlpacaPaperApplicationOutcome(
                    status="canceled",
                    native_client_order_id=client_order_id,
                    native_order_id=str(broker_order_id),
                    broker_called=False,
                    replayed=True,
                    evidence={"order_status": "canceled"},
                )
            broker = self._broker_factory()
            await broker.cancel_order(str(broker_order_id))
            try:
                order = await broker.get_order(str(broker_order_id))
            except Exception:  # noqa: BLE001 - accepted cancel remains reserved
                return AlpacaPaperApplicationOutcome(
                    status="cancel_requested",
                    native_client_order_id=client_order_id,
                    native_order_id=str(broker_order_id),
                    broker_called=True,
                    reason_code="cancel_status_unavailable",
                )
            payload = order.model_dump(mode="json")
            status = normalize_known_broker_order_status(payload.get("status"))
            if status is None:
                return AlpacaPaperApplicationOutcome(
                    status="cancel_requested",
                    reason_code="cancel_status_unknown",
                    native_client_order_id=client_order_id,
                    native_order_id=str(broker_order_id),
                    broker_called=True,
                )
            await ledger.record_status(
                client_order_id,
                payload,
                lifecycle_state_override=(
                    "submitted"
                    if status in KNOWN_OPEN_BROKER_STATUSES or status == "filled"
                    else None
                ),
            )
            if status == "canceled":
                await ledger.record_cancel(
                    client_order_id,
                    cancel_status="canceled",
                    raw_response=payload,
                )
                result_status = "canceled"
            else:
                result_status = "cancel_requested"
            return AlpacaPaperApplicationOutcome(
                status=result_status,
                reason_code=None if status == "canceled" else "cancel_pending",
                native_client_order_id=client_order_id,
                native_order_id=str(broker_order_id),
                broker_called=True,
                evidence={"order_status": status},
            )

    async def _prepare(
        self,
        db: AsyncSession,
        decision: AlpacaVerifiedDecision,
        *,
        enforce_dynamic_checks: bool,
    ) -> tuple[PaperApprovalPacket, dict[str, Any]] | AlpacaPaperApplicationOutcome:
        canonical = decision.order.canonical()
        packet = self._packet(decision, canonical)
        try:
            verify_order_within_packet(packet, canonical)
            if enforce_dynamic_checks:
                now = self._now_fn()
                verify_packet_market_data(packet, now=now, max_age=self._quote_max_age)
            if enforce_dynamic_checks and packet.side == "sell":
                await verify_sell_packet_source(
                    packet,
                    ledger=AlpacaPaperLedgerService(db),
                    requested_qty=decision.order.qty,
                )
        except PaperApprovalPacketError as exc:
            return self._rejected(packet.client_order_id, exc.code, str(exc))

        packet_dict = packet.model_dump(mode="json")
        ledger = AlpacaPaperLedgerService(db)
        await ledger.record_preview(
            client_order_id=packet.client_order_id,
            lifecycle_correlation_id=packet.lifecycle_correlation_id,
            execution_symbol=packet.execution_symbol,
            execution_venue=packet.execution_venue,
            execution_asset_class=packet.execution_asset_class,
            instrument_type=InstrumentType.crypto,
            side=packet.side,
            order_type=decision.order.order_type,
            time_in_force=decision.order.time_in_force,
            requested_qty=decision.order.qty,
            requested_notional=decision.order.notional,
            requested_price=decision.order.limit_price,
            preview_payload={
                "canonical": canonical,
                "approval_packet": packet_dict,
                "provenance": {
                    "snapshot_hash": decision.snapshot_hash,
                    "decision_id": decision.decision_id,
                    "decision_identity_hash": decision.decision_identity_hash,
                    "packet_hash": self._packet_hash(packet_dict),
                    "source_buy_client_order_id": decision.source_buy_client_order_id,
                },
            },
        )
        db.expire_all()
        persisted = await ledger.get_preview_by_client_order_id(packet.client_order_id)
        payload = getattr(persisted, "preview_payload", None) or {}
        stored_packet = (
            payload.get("approval_packet") if isinstance(payload, dict) else None
        )
        stored_canonical = (
            payload.get("canonical") if isinstance(payload, dict) else None
        )
        if not isinstance(stored_packet, dict) or not isinstance(
            stored_canonical, dict
        ):
            return self._rejected(
                packet.client_order_id,
                "malformed_preview",
                "persisted native preview is missing its immutable packet",
            )
        try:
            return PaperApprovalPacket(**stored_packet), stored_canonical
        except ValueError as exc:
            return self._rejected(packet.client_order_id, "malformed_preview", str(exc))

    def _packet(
        self, decision: AlpacaVerifiedDecision, canonical: dict[str, Any]
    ) -> PaperApprovalPacket:
        client_order_id = self._client_order_id(decision, canonical)
        return PaperApprovalPacket(
            signal_source="canonical_experiment",
            artifact_id=uuid.uuid5(uuid.NAMESPACE_URL, decision.decision_identity_hash),
            signal_symbol=decision.signal_symbol,
            signal_venue=decision.signal_venue,
            execution_symbol=decision.order.symbol,
            execution_venue="alpaca_paper",
            execution_asset_class="crypto",
            side=decision.order.side,
            max_notional=ALPACA_CRYPTO_PAPER_MAX_NOTIONAL,
            max_qty=None,
            qty_source=(
                "verified_native_buy"
                if decision.order.side == "sell"
                else "notional_estimate"
            ),
            expected_lifecycle_step="previewed",
            lifecycle_correlation_id=decision.decision_identity_hash,
            client_order_id=client_order_id,
            expires_at=self._now_fn() + DEFAULT_PACKET_TTL,
            account_mode="alpaca_paper",
            origin="automated",
            market_data_asof=decision.snapshot_as_of,
            market_data_source=decision.snapshot_source,
            preview_payload_hash=canonical_hash(canonical),
            snapshot_id=decision.snapshot_id,
            execution_order_type=decision.order.order_type,
            execution_time_in_force=decision.order.time_in_force,
            reference_price=decision.reference_price,
            source_client_order_id=decision.source_buy_client_order_id,
            decision_identity_hash=decision.decision_identity_hash,
        )

    @staticmethod
    def _client_order_id(
        decision: AlpacaVerifiedDecision, canonical: dict[str, Any]
    ) -> str:
        return derive_automated_key(
            correlation_id=decision.decision_identity_hash,
            snapshot_id=decision.snapshot_id,
            canonical=canonical,
        )

    @staticmethod
    def _packet_hash(packet: dict[str, Any]) -> str:
        blob = json.dumps(
            packet, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        return hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))

    @staticmethod
    def _legacy_rejected(
        client_order_id: str | None, code: str, message: str
    ) -> dict[str, Any]:
        return {
            "success": False,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": False,
            "status": "rejected",
            "reason_code": code,
            "client_order_id": client_order_id,
            "message": message,
        }

    @staticmethod
    def _legacy_submit_result(outcome: SubmitOutcome) -> dict[str, Any]:
        return {
            "success": outcome.success,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": outcome.submitted,
            "status": outcome.status,
            "reason_code": outcome.reason_code,
            "client_order_id": outcome.client_order_id,
            "broker_called": outcome.broker_called,
            "order": outcome.order,
            "message": outcome.message,
        }

    @staticmethod
    def _rejected(
        client_order_id: str | None, code: str, message: str
    ) -> AlpacaPaperApplicationOutcome:
        return AlpacaPaperApplicationOutcome(
            status="rejected",
            reason_code=code,
            native_client_order_id=client_order_id,
            message=message,
        )

    @staticmethod
    def _from_submit_outcome(outcome: SubmitOutcome) -> AlpacaPaperApplicationOutcome:
        order = outcome.order or {}
        return AlpacaPaperApplicationOutcome(
            status=outcome.status,
            reason_code=outcome.reason_code,
            native_client_order_id=outcome.client_order_id,
            native_order_id=str(order.get("id")) if order.get("id") else None,
            submitted=outcome.submitted,
            broker_called=outcome.broker_called,
            replayed=outcome.status == "replayed",
            evidence={"order": order} if order else {},
            message=outcome.message,
        )

    @staticmethod
    def _row_outcome(row: Any) -> AlpacaPaperApplicationOutcome:
        lifecycle = str(getattr(row, "lifecycle_state", "") or "")
        return AlpacaPaperApplicationOutcome(
            status="found",
            native_client_order_id=getattr(row, "client_order_id", None),
            native_order_id=getattr(row, "broker_order_id", None),
            replayed=True,
            evidence={
                "lifecycle_state": lifecycle,
                "order_status": getattr(row, "order_status", None),
                "filled_qty": str(getattr(row, "filled_qty", "") or "") or None,
            },
        )


__all__ = [
    "ALPACA_CRYPTO_PAPER_MAX_NOTIONAL",
    "AlpacaPaperApplicationOutcome",
    "AlpacaPaperOrderApplication",
    "AlpacaPaperOrderSpec",
    "AlpacaVerifiedDecision",
]
