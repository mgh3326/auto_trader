"""BROKER_RISK judgement from durable ledger/proposal data only (task416).

Initial implementation draft: predecessor kimi-k3; completed and verified by
the task416 successor.

The detector reads the execution ledger and the order-proposal tables and
classifies four operator-urgent categories from the decision record
(``decision/2026-09-18/fill-triage-resident-lanes`` §결정 5):

* ``duplicate_order`` — the broker reported the same fill content under a
  different ``fill_seq`` (same broker order, same side/qty/price twice);
* ``order_state_unknown`` — a proposal rung for the order is stuck in
  ``unverified`` (submission outcome unknown);
* ``cancel_failed`` — a ``cancel``-action proposal targeting the order has a
  failed approval dispatch (``failed`` / ``partial_failed``);
* ``limit_exceeded`` — the fill's notional exceeds a per-currency
  *observation* cap.  These caps are new monitoring thresholds, unrelated to
  the hard execution-surface invariants (leverage 1x, notional caps), which
  this module never touches.

Hard boundaries: no broker calls, no DB writes, no LLM, no scheduler.  Every
judgement carries ``evidence`` (row ids and field values); judgements without
evidence are never pushed.  Only the four categories above are eligible for
immediate push — normal fills and watch alerts never qualify.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill

logger = logging.getLogger(__name__)

CATEGORY_DUPLICATE_ORDER = "duplicate_order"
CATEGORY_ORDER_STATE_UNKNOWN = "order_state_unknown"
CATEGORY_CANCEL_FAILED = "cancel_failed"
CATEGORY_LIMIT_EXCEEDED = "limit_exceeded"

#: Closed push-eligibility vocabulary.  Adding a category is a push-eligibility
#: expansion and requires an operator decision — do not extend casually.
RISK_CATEGORIES: frozenset[str] = frozenset(
    {
        CATEGORY_DUPLICATE_ORDER,
        CATEGORY_ORDER_STATE_UNKNOWN,
        CATEGORY_CANCEL_FAILED,
        CATEGORY_LIMIT_EXCEEDED,
    }
)

_UNVERIFIED_RUNG_STATE = "unverified"
_CANCEL_DISPATCH_FAILED_STATES: frozenset[str] = frozenset({"failed", "partial_failed"})

#: Default observation caps for ``limit_exceeded``.  Conservative tripwires an
#: order of magnitude above ordinary retail order sizes; override per
#: deployment through ``BrokerRiskConfig``.
DEFAULT_NOTIONAL_CAPS: dict[str, Decimal] = {
    "KRW": Decimal("100000000"),
    "USD": Decimal("100000"),
}


@dataclass(frozen=True)
class BrokerRiskJudgement:
    """One BROKER_RISK verdict.  ``evidence`` must name the rows/fields used."""

    category: str
    market: str
    symbol: str
    summary: str
    evidence: dict[str, Any]
    dedupe_id: str


class BrokerRiskEvidenceSource(Protocol):
    """Read-only evidence queries the detector relies on (fakeable in tests)."""

    async def list_fills_for_order(
        self, *, broker: str, account_mode: str, venue: str, broker_order_id: str
    ) -> list[dict[str, Any]]: ...

    async def list_rungs_for_broker_order(
        self, broker_order_id: str
    ) -> list[dict[str, Any]]: ...

    async def list_cancel_proposals_for_target(
        self, target_broker_order_id: str
    ) -> list[dict[str, Any]]: ...


class SqlAlchemyEvidenceSource:
    """Production evidence source over the ledger/proposal tables (read-only)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_fills_for_order(
        self, *, broker: str, account_mode: str, venue: str, broker_order_id: str
    ) -> list[dict[str, Any]]:
        result = await self._db.execute(
            select(ExecutionLedger)
            .where(
                ExecutionLedger.broker == broker,
                ExecutionLedger.account_mode == account_mode,
                ExecutionLedger.venue == venue,
                ExecutionLedger.broker_order_id == broker_order_id,
            )
            .order_by(ExecutionLedger.id.asc())
        )
        return [sanitize_fill(row) for row in result.scalars().all()]

    async def list_rungs_for_broker_order(
        self, broker_order_id: str
    ) -> list[dict[str, Any]]:
        result = await self._db.execute(
            select(OrderProposalRung)
            .where(OrderProposalRung.broker_order_id == broker_order_id)
            .order_by(OrderProposalRung.id.asc())
        )
        return [
            {
                "rung_id": int(row.id),
                "proposal_pk": int(row.proposal_pk),
                "rung_index": int(row.rung_index),
                "state": row.state,
                "idempotency_key": row.idempotency_key,
                "quantity": None if row.quantity is None else str(row.quantity),
                "limit_price": (
                    None if row.limit_price is None else str(row.limit_price)
                ),
                "notional": None if row.notional is None else str(row.notional),
                "void_reason": row.void_reason,
                "void_reason_group": row.void_reason_group,
                "correlation_id": row.correlation_id,
            }
            for row in result.scalars().all()
        ]

    async def list_cancel_proposals_for_target(
        self, target_broker_order_id: str
    ) -> list[dict[str, Any]]:
        result = await self._db.execute(
            select(OrderProposal)
            .where(
                OrderProposal.action == "cancel",
                OrderProposal.target_broker_order_id == target_broker_order_id,
            )
            .order_by(OrderProposal.id.asc())
        )
        return [
            {
                "proposal_row_id": int(row.id),
                "proposal_id": str(row.proposal_id),
                "lifecycle_state": row.lifecycle_state,
                "approval_dispatch_state": row.approval_dispatch_state,
                "approval_dispatch_failure_code": row.approval_dispatch_failure_code,
                "target_broker_order_id": row.target_broker_order_id,
            }
            for row in result.scalars().all()
        ]


@dataclass(frozen=True)
class BrokerRiskConfig:
    notional_caps: Mapping[str, Decimal] = field(
        default_factory=lambda: dict(DEFAULT_NOTIONAL_CAPS)
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class BrokerRiskDetector:
    """Pure classifier: durable rows in, evidence-carrying judgements out."""

    def __init__(self, config: BrokerRiskConfig | None = None) -> None:
        self.config = config or BrokerRiskConfig()

    async def detect(
        self, fill: Mapping[str, Any], *, source: BrokerRiskEvidenceSource
    ) -> list[BrokerRiskJudgement]:
        judgements: list[BrokerRiskJudgement] = []
        for probe in (
            self._detect_order_duplicate,
            self._detect_state_unknown,
            self._detect_cancel_failed,
        ):
            try:
                judgement = await probe(fill, source=source)
            except Exception:  # noqa: BLE001 - detection is fail-open observation
                logger.warning(
                    "BROKER_RISK probe failed (fail-open): probe=%s ledger_id=%s",
                    probe.__name__,
                    fill.get("ledger_id"),
                    exc_info=True,
                )
                continue
            if judgement is not None:
                judgements.append(judgement)
        limit = self._detect_limit_exceeded(fill)
        if limit is not None:
            judgements.append(limit)
        return judgements

    async def _detect_order_duplicate(
        self, fill: Mapping[str, Any], *, source: BrokerRiskEvidenceSource
    ) -> BrokerRiskJudgement | None:
        siblings = await source.list_fills_for_order(
            broker=str(fill["broker"]),
            account_mode=str(fill["account_mode"]),
            venue=str(fill["venue"]),
            broker_order_id=str(fill["broker_order_id"]),
        )
        qty, price = _decimal(fill["filled_qty"]), _decimal(fill["filled_price"])
        if qty is None or price is None:
            return None
        duplicates = [
            row
            for row in siblings
            if int(row["ledger_id"]) != int(fill["ledger_id"])
            and row["side"] == fill["side"]
            and _decimal(row["filled_qty"]) == qty
            and _decimal(row["filled_price"]) == price
        ]
        if not duplicates:
            return None
        ledger_ids = [int(fill["ledger_id"])] + [
            int(row["ledger_id"]) for row in duplicates
        ]
        return BrokerRiskJudgement(
            category=CATEGORY_DUPLICATE_ORDER,
            market=str(fill["market"]),
            symbol=str(fill["symbol"]),
            summary=(
                f"주문 중복 의심: broker_order_id={fill['broker_order_id']} 에 "
                f"동일 {fill['side']} {qty}@{price} 체결이 fill_seq "
                f"{[int(row['fill_seq']) for row in duplicates]} 로 중복 보고됨"
            ),
            evidence={
                "ledger_ids": sorted(ledger_ids),
                "fill_seqs": sorted(
                    [int(fill["fill_seq"])]
                    + [int(row["fill_seq"]) for row in duplicates]
                ),
                "duplicate_fill_seqs": [int(row["fill_seq"]) for row in duplicates],
                "broker": fill["broker"],
                "account_mode": fill["account_mode"],
                "venue": fill["venue"],
                "broker_order_id": fill["broker_order_id"],
                "side": fill["side"],
                "filled_qty": str(qty),
                "filled_price": str(price),
            },
            dedupe_id=(
                f"duplicate_order:{fill['broker']}:{fill['account_mode']}:"
                f"{fill['venue']}:{fill['broker_order_id']}:"
                f"{min(ledger_ids)}-{max(ledger_ids)}"
            ),
        )

    async def _detect_state_unknown(
        self, fill: Mapping[str, Any], *, source: BrokerRiskEvidenceSource
    ) -> BrokerRiskJudgement | None:
        rungs = await source.list_rungs_for_broker_order(str(fill["broker_order_id"]))
        unverified = [rung for rung in rungs if rung["state"] == _UNVERIFIED_RUNG_STATE]
        if not unverified:
            return None
        return BrokerRiskJudgement(
            category=CATEGORY_ORDER_STATE_UNKNOWN,
            market=str(fill["market"]),
            symbol=str(fill["symbol"]),
            summary=(
                f"주문 상태 불명: broker_order_id={fill['broker_order_id']} 의 "
                f"proposal rung {len(unverified)}건이 unverified"
            ),
            evidence={
                "ledger_id": int(fill["ledger_id"]),
                "broker_order_id": fill["broker_order_id"],
                "rung_ids": [int(rung["rung_id"]) for rung in unverified],
                "proposal_pks": [int(rung["proposal_pk"]) for rung in unverified],
                "void_reasons": [rung["void_reason"] for rung in unverified],
                "correlation_ids": [rung["correlation_id"] for rung in unverified],
            },
            dedupe_id=(
                f"order_state_unknown:{fill['broker']}:{fill['account_mode']}:"
                f"{fill['venue']}:{fill['broker_order_id']}:"
                f"{min(int(rung['rung_id']) for rung in unverified)}"
            ),
        )

    async def _detect_cancel_failed(
        self, fill: Mapping[str, Any], *, source: BrokerRiskEvidenceSource
    ) -> BrokerRiskJudgement | None:
        proposals = await source.list_cancel_proposals_for_target(
            str(fill["broker_order_id"])
        )
        failed = [
            proposal
            for proposal in proposals
            if proposal["approval_dispatch_state"] in _CANCEL_DISPATCH_FAILED_STATES
        ]
        if not failed:
            return None
        return BrokerRiskJudgement(
            category=CATEGORY_CANCEL_FAILED,
            market=str(fill["market"]),
            symbol=str(fill["symbol"]),
            summary=(
                f"취소 실패: broker_order_id={fill['broker_order_id']} 를 target 으로 "
                f"하는 cancel 제안 {len(failed)}건의 dispatch 가 실패"
            ),
            evidence={
                "ledger_id": int(fill["ledger_id"]),
                "broker_order_id": fill["broker_order_id"],
                "proposal_row_ids": [int(p["proposal_row_id"]) for p in failed],
                "proposal_ids": [p["proposal_id"] for p in failed],
                "lifecycle_states": [p["lifecycle_state"] for p in failed],
                "approval_dispatch_states": [
                    p["approval_dispatch_state"] for p in failed
                ],
                "approval_dispatch_failure_codes": [
                    p["approval_dispatch_failure_code"] for p in failed
                ],
            },
            dedupe_id=(
                f"cancel_failed:{fill['broker']}:{fill['account_mode']}:"
                f"{fill['venue']}:{fill['broker_order_id']}:"
                f"{min(int(p['proposal_row_id']) for p in failed)}"
            ),
        )

    def _detect_limit_exceeded(
        self, fill: Mapping[str, Any]
    ) -> BrokerRiskJudgement | None:
        cap = self.config.notional_caps.get(str(fill["currency"]))
        if cap is None:
            return None
        notional = _decimal(fill["filled_notional"])
        if notional is None or notional <= cap:
            return None
        return BrokerRiskJudgement(
            category=CATEGORY_LIMIT_EXCEEDED,
            market=str(fill["market"]),
            symbol=str(fill["symbol"]),
            summary=(
                f"한도 초과: {fill['symbol']} 체결대금 {fill['currency']} "
                f"{notional} > 관측 캡 {cap}"
            ),
            evidence={
                "ledger_id": int(fill["ledger_id"]),
                "broker_order_id": fill["broker_order_id"],
                "currency": fill["currency"],
                "filled_notional": str(notional),
                "observation_cap": str(cap),
            },
            dedupe_id=(
                f"limit_exceeded:{fill['broker']}:{fill['account_mode']}:"
                f"{fill['venue']}:{fill['broker_order_id']}:{fill['ledger_id']}"
            ),
        )


class RiskPushNotifier(Protocol):
    """Immediate-push surface for BROKER_RISK judgements (fakeable in tests)."""

    async def push(self, judgement: BrokerRiskJudgement) -> bool: ...


def render_risk_push_text(judgement: BrokerRiskJudgement) -> str:
    """Render a judgement with its evidence; secrets never appear here because
    evidence is restricted to row ids and numeric/text field values."""
    lines = [
        f"[BROKER_RISK] {judgement.category} — {judgement.market} {judgement.symbol}",
        judgement.summary,
        "evidence:",
    ]
    lines.extend(
        f"- {key}: {value}" for key, value in sorted(judgement.evidence.items())
    )
    return "\n".join(lines)


class TradeNotifierRiskPush:
    """Production push via the existing TradeNotifier interface.

    Uses the same Telegram-only mirror path as the kiwoom b0x live-order-risk
    notifier; it never raises — a failed delivery returns ``False``.
    """

    def __init__(self, notifier_factory: Any | None = None) -> None:
        self._notifier_factory = notifier_factory or _get_trade_notifier

    async def push(self, judgement: BrokerRiskJudgement) -> bool:
        try:
            return bool(
                await self._notifier_factory().notify_agent_message(
                    render_risk_push_text(judgement),
                    parse_mode=None,
                    correlation_id=judgement.dedupe_id,
                    skip_discord=True,
                    mirror_telegram=True,
                )
            )
        except Exception:  # noqa: BLE001 - push is strictly best effort
            logger.warning(
                "BROKER_RISK push failed (fail-open): dedupe_id=%s",
                judgement.dedupe_id,
                exc_info=True,
            )
            return False


def _get_trade_notifier() -> Any:
    """Keep config-heavy notifier imports out of scheduleless CLI ``--help``."""
    from app.monitoring.trade_notifier import get_trade_notifier

    return get_trade_notifier()


__all__ = [
    "CATEGORY_CANCEL_FAILED",
    "CATEGORY_LIMIT_EXCEEDED",
    "CATEGORY_DUPLICATE_ORDER",
    "CATEGORY_ORDER_STATE_UNKNOWN",
    "DEFAULT_NOTIONAL_CAPS",
    "RISK_CATEGORIES",
    "BrokerRiskConfig",
    "BrokerRiskDetector",
    "BrokerRiskEvidenceSource",
    "BrokerRiskJudgement",
    "RiskPushNotifier",
    "SqlAlchemyEvidenceSource",
    "TradeNotifierRiskPush",
    "render_risk_push_text",
]
