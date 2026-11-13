"""Append decision evidence within the caller's transaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@dataclass(frozen=True, kw_only=True)
class DecisionEvidence:
    decision_kind: Literal["proposal", "direct_order", "declined"]
    decided_by: str
    market: Literal["equity_kr", "equity_us", "crypto"]
    symbol: str
    decided_at: datetime
    account_mode: Literal["kis_live", "toss_live", "upbit"] | None = None
    side: Literal["buy", "sell"] | None = None
    strategy: str | None = None
    sleeve_id: int | None = None
    candidate_call_id: UUID | None = None
    candidate_symbol: str | None = None
    candidate_link_basis: Literal["recorded", "inference"] | None = None
    currency: Literal["KRW", "USD"] | None = None
    stop_price: Decimal | None = None
    stop_basis: Literal["support", "pct_floor", "volatility"] | None = None
    stop_source: Literal["exit_plan", "order_argument"] | None = None
    target_price: Decimal | None = None
    horizon_sessions: int | None = None
    forecast_ids: tuple[UUID, ...] | None = None
    report_item_uuid: UUID | None = None
    policy_version: str | None = None
    policy_content_hash: str | None = None


@dataclass(frozen=True, kw_only=True)
class DecisionRung:
    rung_index: int
    side: Literal["buy", "sell"]
    planned_quantity: Decimal | None = None
    planned_notional: Decimal | None = None
    limit_price: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class ReferencePrice:
    kind: Literal["limit", "approval_price", "quote_at_decision", "candidate_price"]
    price: Decimal
    rung_index: int | None = None
    asof: datetime | None = None


def _normalized(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("decimal must be finite")
        if not value:
            return "0"
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (tuple, list)):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, float):
        raise TypeError("float is not an exact decimal")
    return value


def evidence_hash(evidence: DecisionEvidence) -> str:
    payload = json.dumps(
        _normalized(asdict(evidence)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_decimals(*values: Decimal | None) -> None:
    for value in values:
        if value is None:
            continue
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("prices and quantities must be finite Decimal values")
        if not value:
            continue
        _, digits, exponent = value.as_tuple()
        while exponent < 0 and digits[-1] == 0:
            digits = digits[:-1]
            exponent += 1
        if max(-exponent, 0) > 12 or max(len(digits) + exponent, 0) > 26:
            raise ValueError("prices and quantities must fit numeric(38,12)")


def _check_reference_price(price: ReferencePrice) -> None:
    _check_decimals(price.price)
    if price.asof is not None and price.asof.utcoffset() is None:
        raise ValueError("asof must be timezone-aware")


def account_scope(account_mode: Literal["kis_live", "toss_live", "upbit"]) -> str:
    return {"kis_live": "kis_live", "toss_live": "toss_live", "upbit": "upbit_live"}[
        account_mode
    ]


class DecisionEvidenceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        evidence: DecisionEvidence,
        rungs: list[DecisionRung],
        prices: list[ReferencePrice],
    ) -> UUID:
        _check_decimals(evidence.stop_price, evidence.target_price)
        for rung in rungs:
            _check_decimals(
                rung.planned_quantity, rung.planned_notional, rung.limit_price
            )
        for price in prices:
            _check_reference_price(price)
        decision_id = uuid4()
        values = asdict(evidence)
        values.update(decision_id=decision_id, evidence_hash=evidence_hash(evidence))
        if values["forecast_ids"] is not None:
            values["forecast_ids"] = list(values["forecast_ids"])
        columns = ", ".join(values)
        binds = ", ".join(f":{key}" for key in values)
        await self.session.execute(
            text(f"INSERT INTO review.decision_evidence ({columns}) VALUES ({binds})"),
            values,
        )
        for rung in rungs:
            await self._insert(
                "decision_evidence_rungs", {"decision_id": decision_id, **asdict(rung)}
            )
        for price in prices:
            await self.add_reference_price(decision_id, price)
        return decision_id

    async def add_reference_price(
        self, decision_id: UUID, price: ReferencePrice
    ) -> None:
        _check_reference_price(price)
        await self._insert(
            "decision_reference_prices", {"decision_id": decision_id, **asdict(price)}
        )

    async def link_order(
        self,
        *,
        attempt_id: UUID,
        decision_id: UUID,
        account_scope: Literal["kis_live", "toss_live", "upbit_live"],
        send_key: str,
        proposal_id: UUID | None = None,
        rung_index: int | None = None,
    ) -> None:
        await self._insert(
            "decision_order_links",
            {
                "attempt_id": attempt_id,
                "decision_id": decision_id,
                "account_scope": account_scope,
                "send_key": send_key,
                "proposal_id": proposal_id,
                "rung_index": rung_index,
            },
        )

    async def record_outcome(
        self, attempt_id: UUID, outcome: Literal["proven_not_sent", "sent"]
    ) -> None:
        await self._insert(
            "decision_order_link_outcomes",
            {"attempt_id": attempt_id, "outcome": outcome},
        )

    async def _insert(self, table: str, values: dict) -> None:
        columns = ", ".join(values)
        binds = ", ".join(f":{key}" for key in values)
        await self.session.execute(
            text(f"INSERT INTO review.{table} ({columns}) VALUES ({binds})"), values
        )


_OBSERVATION_TIMEOUT = 1.0
_observation_tasks: set[asyncio.Task] = set()
_observation_sessions = None


def observation_session():
    """Use a separate, non-pooled connection, never the order session pool."""
    global _observation_sessions
    if _observation_sessions is None:
        from app.core.config import settings

        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        _observation_sessions = async_sessionmaker(engine, expire_on_commit=False)
    return _observation_sessions()


def _start_observation(coro):
    if any(not task.done() for task in _observation_tasks):
        coro.close()
        logging.getLogger(__name__).error("decision_order_evidence reason=writer_busy")
        return None
    task = asyncio.create_task(coro)
    _observation_tasks.add(task)
    task.add_done_callback(_observation_tasks.discard)
    return task


async def begin_order_attempt(
    session_factory,
    *,
    account_mode,
    market,
    symbol,
    side,
    strategy,
    send_key,
    proposal_id,
    rung_index,
    decision_id,
    proposal_flow,
):
    """Commit one observation in a bounded, independent transaction."""
    reason = "decision_write_failed"
    attempt_id = uuid4()

    async def write():
        try:
            nonlocal reason
            async with session_factory() as session:
                service = DecisionEvidenceService(session)
                resolved = decision_id
                if not proposal_flow:
                    resolved = await service.record(
                        DecisionEvidence(
                            decision_kind="direct_order",
                            decided_by="direct_order",
                            market=market,
                            account_mode=account_mode,
                            symbol=symbol,
                            side=side,
                            strategy=strategy,
                            decided_at=datetime.now(UTC),
                        ),
                        [],
                        [],
                    )
                reason = "attempt_write_failed"
                if resolved is None:
                    raise ValueError("proposal decision missing")
                await service.link_order(
                    attempt_id=attempt_id,
                    decision_id=resolved,
                    account_scope=account_scope(account_mode),
                    send_key=send_key,
                    proposal_id=proposal_id,
                    rung_index=rung_index,
                )
                await session.commit()
            return True
        except Exception:  # noqa: BLE001 - evidence must never gate an order
            logging.getLogger(__name__).error(
                "decision_order_evidence reason=%s", reason
            )
            return False

    task = _start_observation(write())
    if task is None:
        return None
    done, _ = await asyncio.wait({task}, timeout=_OBSERVATION_TIMEOUT)
    if not done:
        logging.getLogger(__name__).error(
            "decision_order_evidence reason=attempt_timeout"
        )
        return attempt_id
    return attempt_id if task.result() else None


async def finish_order_attempt(session_factory, attempt_id, outcome):
    async def write():
        try:
            async with session_factory() as session:
                await DecisionEvidenceService(session).record_outcome(
                    attempt_id, outcome
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - evidence must never gate an order
            logging.getLogger(__name__).error(
                "decision_order_evidence reason=outcome_write_failed"
            )

    task = _start_observation(write())
    if task is None:
        return
    done, _ = await asyncio.wait({task}, timeout=_OBSERVATION_TIMEOUT)
    if not done:
        logging.getLogger(__name__).error(
            "decision_order_evidence reason=outcome_timeout"
        )


class AttemptSendHook:
    """Preserve the hook and conservatively count entered POST boundaries."""

    def __init__(self, hook):
        self.hook = hook
        self.post_count = 0
        self.attempt_id: UUID | None = None

    async def __call__(self, *args, **kwargs):
        result = await self.hook(*args, **kwargs) if self.hook is not None else None
        self.post_count += 1
        return result
