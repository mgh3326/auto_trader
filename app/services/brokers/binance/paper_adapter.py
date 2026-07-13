"""Canonical Binance Spot Demo paper adapter for ROB-845.

The adapter composes only the guarded Demo market-data, executor, and native
ledger services.  It never signs an order itself and never imports a live or
MCP-tooling mutation surface.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.market_data import (
    DemoScalpingMarketData,
    MarketConditionsUnavailable,
    build_market_conditions,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoExecutionIdentity,
    DemoScalpingExecutor,
    ExecutionResult,
)
from app.services.brokers.binance.demo_scalping_exec.reference import DemoReferenceData
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperReasonCode,
    PaperRiskSnapshot,
    VerifiedPaperOrderIntent,
)

_ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
_LIMITS = ScalpingRiskLimits(
    allowlist=_ALLOWED_SYMBOLS,
    excluded=frozenset(),
)
_POLICY_VERSION = "rob845-binance-spot-demo-v1"
_POLICY_CANONICAL = json.dumps(
    {
        "allowlist": sorted(_LIMITS.allowlist),
        "excluded": sorted(_LIMITS.excluded),
        "max_notional_usdt": str(_LIMITS.max_notional_usdt),
        "global_open_lifecycle_cap": _LIMITS.global_open_lifecycle_cap,
        "daily_order_count_cap": _LIMITS.daily_order_count_cap,
        "daily_loss_budget_usdt": str(_LIMITS.daily_loss_budget_usdt),
        "cooldown_seconds": _LIMITS.cooldown_seconds,
        "max_spread_bps": str(_LIMITS.max_spread_bps),
        "max_data_age_seconds": _LIMITS.max_data_age_seconds,
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
)
_POLICY_HASH = hashlib.sha256(_POLICY_CANONICAL.encode("utf-8")).hexdigest()

_SessionFactory = Callable[[], Any]
_ObjectFactory = Callable[[], Any]
_MarketBuilder = Callable[..., Awaitable[Any]]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class BinanceSpotDemoPaperAdapter:
    """Spot Demo BUY/MARKET/notional round-trip adapter."""

    broker = Broker.BINANCE

    def __init__(
        self,
        *,
        session_factory: _SessionFactory = AsyncSessionLocal,
        client_factory: _ObjectFactory = BinanceSpotDemoExecutionClient.from_env,
        reference_factory: _ObjectFactory = DemoReferenceData,
        market_data_factory: _ObjectFactory = DemoScalpingMarketData,
        market_conditions_builder: _MarketBuilder = build_market_conditions,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._reference_factory = reference_factory
        self._market_data_factory = market_data_factory
        self._market_conditions_builder = market_conditions_builder
        self._clock = clock

    async def preview(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        unsupported = self._unsupported(PaperOperation.PREVIEW, intent)
        if unsupported is not None:
            return unsupported
        reference: Any = None
        market_data: Any = None
        try:
            reference = self._reference_factory()
            market_data = self._market_data_factory()
            market = await self._market_conditions_builder(
                market_data, product="spot", symbol=intent.symbol
            )
            async with self._session_factory() as session:
                executor = DemoScalpingExecutor(
                    product="spot",
                    client=None,
                    session=session,
                    reference=reference,
                    now=self._clock(),
                    limits=_LIMITS,
                    execution_identity=self._execution_identity(intent),
                )
                result = await executor.execute(
                    self._native_intent(intent),
                    confirm=False,
                    market=market,
                    session_tag="paper_execution",
                    signal_snapshot=self._signal_snapshot(intent),
                )
            return self._map_execution_result(PaperOperation.PREVIEW, intent, result)
        except MarketConditionsUnavailable as exc:
            return self._blocked(
                PaperOperation.PREVIEW,
                "market_conditions_unavailable",
                evidence={"reason": exc.reason},
            )
        except Exception as exc:  # noqa: BLE001 — typed adapter failure boundary
            return self._failed(PaperOperation.PREVIEW, exc)
        finally:
            await self._close(reference)
            await self._close(market_data)

    async def submit(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        unsupported = self._unsupported(PaperOperation.SUBMIT, intent)
        if unsupported is not None:
            return unsupported
        native_intent = self._native_intent(intent)
        identity = self._execution_identity(intent)
        reference: Any = None
        market_data: Any = None
        client: Any = None
        try:
            async with self._session_factory() as session:
                # Idempotency truth precedes even unsigned market I/O and signed
                # client construction.  New orders continue to the guarded path.
                replay_reader = DemoScalpingExecutor(
                    product="spot",
                    client=None,
                    session=session,
                    reference=None,
                    now=self._clock(),
                    limits=_LIMITS,
                    execution_identity=identity,
                )
                existing = await replay_reader.resolve_existing_execution(native_intent)
                if existing is not None:
                    return await self._map_submit_with_native_truth(
                        session, intent, existing
                    )

                reference = self._reference_factory()
                market_data = self._market_data_factory()
                market = await self._market_conditions_builder(
                    market_data, product="spot", symbol=intent.symbol
                )
                client = self._client_factory()
                executor = DemoScalpingExecutor(
                    product="spot",
                    client=client,
                    session=session,
                    reference=reference,
                    now=self._clock(),
                    limits=_LIMITS,
                    execution_identity=identity,
                )
                result = await executor.execute(
                    native_intent,
                    confirm=True,
                    market=market,
                    session_tag="paper_execution",
                    signal_snapshot=self._signal_snapshot(intent),
                )
                await session.commit()
                return await self._map_submit_with_native_truth(session, intent, result)
        except MarketConditionsUnavailable as exc:
            return self._blocked(
                PaperOperation.SUBMIT,
                "market_conditions_unavailable",
                evidence={"reason": exc.reason},
            )
        except Exception as exc:  # noqa: BLE001 — typed adapter failure boundary
            return self._failed(PaperOperation.SUBMIT, exc)
        finally:
            await self._close(client)
            await self._close(reference)
            await self._close(market_data)

    async def cancel(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return self._blocked(
            PaperOperation.CANCEL, PaperReasonCode.UNSUPPORTED_CAPABILITY
        )

    async def reconcile(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return self._blocked(
            PaperOperation.RECONCILE, PaperReasonCode.UNSUPPORTED_CAPABILITY
        )

    async def get_order(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        unsupported = self._unsupported(PaperOperation.GET_ORDER, intent)
        if unsupported is not None:
            return unsupported
        return await self._read_native(PaperOperation.GET_ORDER, intent)

    async def link_native_order(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult:
        unsupported = self._unsupported(PaperOperation.LINK_NATIVE_ORDER, intent)
        if unsupported is not None:
            return unsupported
        return await self._read_native(PaperOperation.LINK_NATIVE_ORDER, intent)

    async def _read_native(
        self, operation: PaperOperation, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult:
        identity = self._execution_identity(intent)
        async with self._session_factory() as session:
            ledger = BinanceDemoLedgerService(session)
            root = await ledger.get_by_client_order_id(identity.root_client_order_id)
            if root is None:
                return self._blocked(operation, "native_order_not_found")
            close = await ledger.get_by_client_order_id(identity.close_client_order_id)
            return PaperOperationResult(
                operation=operation,
                status=PaperOperationStatus.SUCCEEDED,
                reason_code=PaperReasonCode.OK,
                venue=self.broker,
                native_order_id=root.broker_order_id,
                native_client_order_id=root.client_order_id,
                evidence={
                    "root": self._row_evidence(root),
                    "close": None if close is None else self._row_evidence(close),
                },
            )

    async def _map_submit_with_native_truth(
        self,
        session: Any,
        intent: VerifiedPaperOrderIntent,
        result: ExecutionResult,
    ) -> PaperOperationResult:
        mapped = self._map_execution_result(PaperOperation.SUBMIT, intent, result)
        if result.open_client_order_id is None:
            return mapped
        ledger = BinanceDemoLedgerService(session)
        root = await ledger.get_by_client_order_id(result.open_client_order_id)
        return mapped.model_copy(
            update={"native_order_id": None if root is None else root.broker_order_id}
        )

    def _map_execution_result(
        self,
        operation: PaperOperation,
        intent: VerifiedPaperOrderIntent,
        result: ExecutionResult,
    ) -> PaperOperationResult:
        if result.status in {"dry_run", "reconciled"}:
            status = PaperOperationStatus.SUCCEEDED
            reason: str = PaperReasonCode.OK
        elif result.status == "blocked":
            status = PaperOperationStatus.BLOCKED
            reason = result.reason_codes[0] if result.reason_codes else "blocked"
        else:
            status = PaperOperationStatus.FAILED
            reason = result.anomaly_reason or result.status
        risk_snapshot = self._risk_snapshot(intent, result)
        return PaperOperationResult(
            operation=operation,
            status=status,
            reason_code=reason,
            venue=self.broker,
            native_client_order_id=result.open_client_order_id,
            evidence={
                **result.to_evidence_dict(),
                "close_client_order_id": result.close_client_order_id,
                "canonical_market_snapshot": {
                    "price": str(intent.reference_price),
                    "source": intent.market_snapshot_source,
                    "as_of": intent.market_snapshot_as_of.isoformat(),
                    "snapshot_id": intent.market_snapshot_id,
                    "snapshot_hash": intent.market_snapshot_hash,
                    "experiment_policy_hash": intent.policy_hash,
                },
                "native_demo_risk": {
                    "reference_price": (
                        None
                        if result.reference_price is None
                        else str(result.reference_price)
                    ),
                    "reference_source": "binance_demo_ticker_price",
                    "policy_version": _POLICY_VERSION,
                    "policy_hash": _POLICY_HASH,
                },
            },
            risk_snapshot=risk_snapshot,
            replayed=result.replayed,
        )

    def _risk_snapshot(
        self, intent: VerifiedPaperOrderIntent, result: ExecutionResult
    ) -> PaperRiskSnapshot | None:
        ledger = result.ledger_snapshot
        market = result.market_conditions
        quote_price = result.reference_price
        if ledger is None or market is None or quote_price is None:
            return None
        return PaperRiskSnapshot(
            open_exposure=None,
            reserved_notional=None,
            daily_realized_loss=ledger.realized_loss_today_usdt,
            quote_price=intent.reference_price,
            spread_bps=market.spread_bps,
            data_age_seconds=Decimal(str(market.data_age_seconds)),
            quote_source=intent.market_snapshot_source,
            quote_as_of=intent.market_snapshot_as_of,
            policy_version=_POLICY_VERSION,
            policy_hash=_POLICY_HASH,
        )

    def _unsupported(
        self, operation: PaperOperation, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult | None:
        if (
            intent.venue is not self.broker
            or intent.account_mode != "demo"
            or intent.product != "spot"
            or intent.symbol not in _ALLOWED_SYMBOLS
            or intent.side != "buy"
            or intent.order_type != "market"
            or intent.time_in_force is not None
            or intent.qty is not None
            or intent.notional is None
            or intent.price is not None
            or intent.market_snapshot_source != "binance_public_spot"
        ):
            return self._blocked(operation, PaperReasonCode.UNSUPPORTED_CAPABILITY)
        return None

    def _native_intent(self, intent: VerifiedPaperOrderIntent) -> OrderIntent:
        snapshot_ms = int(intent.market_snapshot_as_of.timestamp() * 1000)
        return OrderIntent(
            product="spot",
            symbol=intent.symbol,
            side="BUY",
            order_type="MARKET",
            target_notional_usdt=intent.notional or Decimal("0"),
            entry_reference_price=intent.reference_price,
            tp_price=None,
            sl_price=None,
            confidence=Decimal("0"),
            reason_codes=("paper_execution",),
            source_candle_close_time_ms=snapshot_ms,
            # Native immutable intent metadata must remain identical on retry;
            # wall-clock time would turn an exact replay into a collision.
            evaluated_at_ms=snapshot_ms,
        )

    def _execution_identity(
        self, intent: VerifiedPaperOrderIntent
    ) -> DemoExecutionIdentity:
        metadata = intent.model_dump(mode="json")
        return DemoExecutionIdentity.from_verified_metadata(
            decision_id=intent.decision_id,
            idempotency_key=intent.idempotency_key,
            immutable_metadata=metadata,
        )

    @staticmethod
    def _signal_snapshot(intent: VerifiedPaperOrderIntent) -> dict[str, Any]:
        return {
            "source": "paper_execution",
            "experiment_id": intent.experiment_id,
            "run_id": intent.run_id,
            "cohort_id": intent.cohort_id,
            "decision_id": intent.decision_id,
            "market_snapshot_id": intent.market_snapshot_id,
            "market_snapshot_hash": intent.market_snapshot_hash,
        }

    @staticmethod
    def _row_evidence(row: Any) -> dict[str, object]:
        return {
            "product": row.product,
            "venue_host": row.venue_host,
            "client_order_id": row.client_order_id,
            "parent_client_order_id": row.parent_client_order_id,
            "broker_order_id": row.broker_order_id,
            "side": row.side,
            "order_type": row.order_type,
            "qty": str(row.qty),
            "notional_usdt": (
                None if row.notional_usdt is None else str(row.notional_usdt)
            ),
            "lifecycle_state": row.lifecycle_state,
            "metadata": row.extra_metadata or {},
        }

    def _blocked(
        self,
        operation: PaperOperation,
        reason: str,
        *,
        evidence: dict[str, object] | None = None,
    ) -> PaperOperationResult:
        return PaperOperationResult.blocked(
            operation=operation,
            venue=self.broker,
            reason_code=reason,
            evidence=evidence,
        )

    def _failed(
        self, operation: PaperOperation, exc: Exception
    ) -> PaperOperationResult:
        return PaperOperationResult(
            operation=operation,
            status=PaperOperationStatus.FAILED,
            reason_code=PaperReasonCode.ADAPTER_UNAVAILABLE,
            venue=self.broker,
            evidence={"error_type": type(exc).__name__},
        )

    @staticmethod
    async def _close(resource: Any) -> None:
        if resource is None:
            return
        close = getattr(resource, "aclose", None)
        if close is not None:
            await close()


__all__ = ["BinanceSpotDemoPaperAdapter"]
