# app/services/trade_journal_write_service.py
"""ROB-120 — Write-through service for the operator-facing thesis journal.

Hard rules:
  * Only `live` account journals are created or updated here. Paper journals
    are created by the existing paper-trade journal pipeline.
  * Status is restricted to {draft, active}. Terminal transitions
    (closed, stopped, expired) and exit_* / pnl_* fields are owned by
    downstream services and never mutated here.
  * `extra_metadata` is rewritten with the merge of any pre-existing keys
    plus (`research_session_id`, `research_summary_id`) — no other keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.schemas.trade_journal import (
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)

_WRITABLE_STATUSES = frozenset({"draft", "active"})


class JournalWriteError(Exception):
    """Raised when a payload would mutate forbidden fields."""


def _to_read(j: TradeJournal) -> JournalReadResponse:
    meta = j.extra_metadata or {}
    return JournalReadResponse(
        id=j.id,
        symbol=j.symbol,
        instrument_type=j.instrument_type.value
        if hasattr(j.instrument_type, "value")
        else str(j.instrument_type),
        side=j.side,  # type: ignore[arg-type]
        thesis=j.thesis,
        strategy=j.strategy,
        target_price=float(j.target_price) if j.target_price is not None else None,
        stop_loss=float(j.stop_loss) if j.stop_loss is not None else None,
        min_hold_days=j.min_hold_days,
        hold_until=j.hold_until.isoformat() if j.hold_until else None,
        status=j.status,  # type: ignore[arg-type]
        account=j.account,
        account_type=j.account_type,  # type: ignore[arg-type]
        notes=j.notes,
        research_session_id=meta.get("research_session_id")
        if isinstance(meta, dict)
        else None,
        research_summary_id=meta.get("research_summary_id")
        if isinstance(meta, dict)
        else None,
        created_at=j.created_at.isoformat(),
        updated_at=j.updated_at.isoformat(),
    )


def _coerce_instrument_type(raw: str) -> InstrumentType:
    try:
        return InstrumentType(raw)
    except ValueError as exc:
        raise JournalWriteError(f"invalid instrument_type: {raw}") from exc


def _build_metadata(
    existing: dict[str, Any] | None,
    research_session_id: int | None,
    research_summary_id: int | None,
) -> dict[str, Any] | None:
    out: dict[str, Any] = dict(existing or {})
    if research_session_id is not None:
        out["research_session_id"] = research_session_id
    if research_summary_id is not None:
        out["research_summary_id"] = research_summary_id
    return out or None


class TradeJournalWriteService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, req: JournalCreateRequest) -> JournalReadResponse:
        if req.status not in _WRITABLE_STATUSES:
            raise JournalWriteError(f"status {req.status!r} not writable")
        hold_until: datetime | None = None
        if req.min_hold_days is not None:
            hold_until = datetime.now(UTC) + timedelta(days=req.min_hold_days)
        journal = TradeJournal(
            symbol=req.symbol.strip(),
            instrument_type=_coerce_instrument_type(req.instrument_type),
            side=req.side,
            thesis=req.thesis,
            strategy=req.strategy,
            target_price=req.target_price,
            stop_loss=req.stop_loss,
            min_hold_days=req.min_hold_days,
            hold_until=hold_until,
            status=req.status,
            account=req.account,
            account_type="live",
            notes=req.notes,
            extra_metadata=_build_metadata(
                None, req.research_session_id, req.research_summary_id
            ),
        )
        self.db.add(journal)
        await self.db.flush()
        await self.db.refresh(journal)
        return _to_read(journal)

    async def update(
        self, journal_id: int, req: JournalUpdateRequest
    ) -> JournalReadResponse:
        payload: dict[str, Any] = req.model_dump(exclude_none=True)
        return await self._apply_update(journal_id, payload)

    async def _apply_update(
        self, journal_id: int, payload: dict[str, Any]
    ) -> JournalReadResponse:
        if "status" in payload and payload["status"] not in _WRITABLE_STATUSES:
            raise JournalWriteError(
                f"refusing to update status to {payload['status']!r}"
            )
        # Belt-and-suspenders: forbidden columns must never be touched here.
        forbidden = {
            "trade_id",
            "exit_price",
            "exit_date",
            "exit_reason",
            "pnl_pct",
            "paper_trade_id",
            "account_type",
        }
        offending = forbidden & payload.keys()
        if offending:
            raise JournalWriteError(
                f"refusing to mutate forbidden fields: {sorted(offending)}"
            )

        row = (
            await self.db.execute(
                select(TradeJournal).where(TradeJournal.id == journal_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise JournalWriteError(f"journal {journal_id} not found")
        if row.account_type != "live":
            raise JournalWriteError("paper journals are not editable here")

        research_session_id = payload.pop("research_session_id", None)
        research_summary_id = payload.pop("research_summary_id", None)

        for key, value in payload.items():
            if hasattr(row, key):
                setattr(row, key, value)

        if "min_hold_days" in payload and payload["min_hold_days"] is not None:
            row.hold_until = datetime.now(UTC) + timedelta(
                days=payload["min_hold_days"]
            )

        if research_session_id is not None or research_summary_id is not None:
            row.extra_metadata = _build_metadata(
                row.extra_metadata, research_session_id, research_summary_id
            )

        await self.db.flush()
        await self.db.refresh(row)
        return _to_read(row)
