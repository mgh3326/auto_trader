"""ROB-878 child-2 — control-mode retrospective action repository.

This module is the single boundary between the application and retrospective
action storage. In ``shadow`` mode it delegates to the legacy JSONB
reader/writer. In ``canonical`` mode it reads from the child ledger and writes
the compatibility projection to parent JSONB under the GUC marker.

Key contracts:
- Control row is DB authority; missing/unknown mode fails closed.
- Parent is locked before children (FOR UPDATE, ORDER BY id).
- Projection starts from legacy_payload, overlays canonical fields, preserves
  unknown keys and display order.
- obsolete/expired project as legacy status=done + additive terminal_status.
- Canonical new actions may only start as open/in_progress.
- Actor is derived from authenticated identity, not caller payload.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import and_, case, func, select, text, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.timezone import now_kst
from app.models.review import (
    TradeRetrospective,
    TradeRetrospectiveAction,
    TradeRetrospectiveActionControl,
)
from app.schemas.trade_retrospective import VALID_NEXT_ACTION_STATUSES
from app.services.trade_journal.retrospective_query_filters import (
    kst_day_end,
    kst_day_start,
    outcome_filter_predicate,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = frozenset({"open", "in_progress"})
_TERMINAL_STATUSES = frozenset({"done", "obsolete", "expired"})
_VALID_INITIAL_STATUSES = frozenset({"open", "in_progress"})
_GUC_MARKER = "app.retrospective_action_projection_writer"
_GUC_VALUE = "v1"
_CUTOVER_ADVISORY_LOCK_ID = 878_880_001

# Fields that the projection overlays from canonical state onto legacy_payload.
_CANONICAL_OVERLAY_FIELDS = (
    "action",
    "owner",
    "issue_id",
    "status",
    "due_kst_date",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ActionControlError(RuntimeError):
    """Control row missing or invalid — writes fail closed."""


class ActionReconcileError(ValueError):
    """Save reconciliation contract violation (ownership, terminal, etc.)."""


class CutoverParityError(RuntimeError):
    """Cutover parity verification failed."""


class CutoverLockError(RuntimeError):
    """Cutover could not obtain its advisory/table locks within the bound."""


# ---------------------------------------------------------------------------
# Control mode
# ---------------------------------------------------------------------------


async def get_control_mode(db: AsyncSession) -> str:
    """Read the current control mode from the database.

    Fails closed if the control row is missing or the mode is unknown.
    """
    result = await db.execute(
        select(TradeRetrospectiveActionControl.mode).where(
            TradeRetrospectiveActionControl.id == 1
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise ActionControlError(
            "retrospective action control row is missing; writes fail closed"
        )
    if row not in ("shadow", "canonical"):
        raise ActionControlError(
            f'retrospective action control mode "{row}" is invalid; writes fail closed'
        )
    return row


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class RetrospectiveActionRepository:
    """Control-mode-aware repository for retrospective actions.

    In shadow mode, reads come from parent JSONB and writes go directly to
    parent JSONB (legacy behavior). In canonical mode, reads come from the
    child ledger and writes update children + rebuild the projection.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_control_mode(self) -> str:
        return await get_control_mode(self.db)

    # -- Read ---------------------------------------------------------------

    async def read_actions(self, retrospective_id: int) -> list[dict[str, Any]]:
        """Mode-aware read of actions for a retrospective.

        Shadow: reads parent JSONB next_actions.
        Canonical: reads child ledger rows ordered by position.
        """
        mode = await self.get_control_mode()
        if mode == "shadow":
            return await self._read_shadow(retrospective_id)
        return await self._read_canonical(retrospective_id)

    async def _read_shadow(self, retrospective_id: int) -> list[dict[str, Any]]:
        result = await self.db.execute(
            select(TradeRetrospective.next_actions).where(
                TradeRetrospective.id == retrospective_id
            )
        )
        raw = result.scalar_one_or_none()
        if raw is None:
            return []
        return list(raw) if isinstance(raw, list) else []

    async def _read_canonical(self, retrospective_id: int) -> list[dict[str, Any]]:
        result = await self.db.execute(
            select(TradeRetrospectiveAction)
            .where(TradeRetrospectiveAction.retrospective_id == retrospective_id)
            .order_by(TradeRetrospectiveAction.position, TradeRetrospectiveAction.id)
        )
        rows = result.scalars().all()
        return [self._action_to_dict(a) for a in rows]

    async def read_actions_many(
        self, parents: Sequence[TradeRetrospective]
    ) -> dict[int, list[dict[str, Any]] | None]:
        """Mode-aware batch hydration without one query per parent."""
        mode = await self.get_control_mode()
        if mode == "shadow":
            return {
                parent.id: (
                    list(parent.next_actions)
                    if isinstance(parent.next_actions, list)
                    else None
                )
                for parent in parents
            }

        by_parent = {parent.id: [] for parent in parents}
        if not by_parent:
            return by_parent
        result = await self.db.execute(
            select(TradeRetrospectiveAction)
            .where(TradeRetrospectiveAction.retrospective_id.in_(by_parent))
            .order_by(
                TradeRetrospectiveAction.retrospective_id,
                TradeRetrospectiveAction.position,
                TradeRetrospectiveAction.id,
            )
        )
        for action in result.scalars():
            by_parent[action.retrospective_id].append(self._action_to_dict(action))
        return by_parent

    def _action_to_dict(self, a: TradeRetrospectiveAction) -> dict[str, Any]:
        payload = dict(a.legacy_payload or {})
        # force_new is a write intent, never persisted/read back as state.
        payload.pop("force_new", None)
        payload.update(
            {
                "action_id": str(a.id),
                "creation_key": str(a.creation_key) if a.creation_key else None,
                "action": a.action,
                "owner": a.owner,
                "issue_id": a.issue_id,
                "status": a.status,
                "due_kst_date": a.due_kst_date.isoformat() if a.due_kst_date else None,
                "version": a.version,
                "position": a.position,
                "status_changed_at": (
                    a.status_changed_at.isoformat() if a.status_changed_at else None
                ),
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                "status_actor": a.status_actor,
                "status_source": a.status_source,
                "status_reason": a.status_reason,
                "status_evidence": a.status_evidence,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
        )
        return payload

    # -- Reconcile (canonical save) -----------------------------------------

    async def reconcile_actions(
        self,
        retrospective_id: int,
        actions: list[dict[str, Any]] | None,
        actor: str,
    ) -> None:
        """Reconcile incoming actions against canonical children.

        If ``actions`` is None, no reconciliation is performed (field-presence
        semantics: omitted next_actions means "don't touch children").
        """
        if actions is None:
            return

        mode = await self.get_control_mode()
        if mode != "canonical":
            # In shadow mode, reconcile is a no-op — the legacy writer
            # already wrote next_actions to parent JSONB.
            return

        # Lock parent row first
        parent_result = await self.db.execute(
            select(TradeRetrospective)
            .where(TradeRetrospective.id == retrospective_id)
            .with_for_update()
        )
        parent = parent_result.scalar_one_or_none()
        if parent is None:
            raise ActionReconcileError(f"retrospective {retrospective_id} not found")

        # Lock all children in stable ID order
        children_result = await self.db.execute(
            select(TradeRetrospectiveAction)
            .where(TradeRetrospectiveAction.retrospective_id == retrospective_id)
            .order_by(TradeRetrospectiveAction.id)
            .with_for_update()
        )
        existing_children: list[TradeRetrospectiveAction] = list(
            children_result.scalars().all()
        )
        display_children = sorted(
            existing_children,
            key=lambda child: (child.position, child.id),
        )

        # Build a lookup by id for action_id matching
        children_by_id: dict[uuid.UUID, TradeRetrospectiveAction] = {
            a.id: a for a in existing_children
        }
        # Track which children have been matched
        matched_ids: set[uuid.UUID] = set()

        # Track position assignments
        next_position = 0
        # Track creation_key → child for idempotent force_new
        children_by_ckey: dict[uuid.UUID, TradeRetrospectiveAction] = {
            a.creation_key: a for a in existing_children if a.creation_key is not None
        }
        request_creation_keys: set[uuid.UUID] = set()
        known_tuples = {
            (child.action, child.owner, child.issue_id, child.due_kst_date)
            for child in display_children
        }

        for incoming in actions:
            action_text = (incoming.get("action") or "").strip()
            if not action_text:
                raise ActionReconcileError("action must be a non-empty string")

            incoming_action_id = incoming.get("action_id")
            force_new = incoming.get("force_new", False)
            creation_key_str = incoming.get("creation_key")

            if incoming_action_id is None:
                if force_new and creation_key_str is None:
                    raise ActionReconcileError(
                        "creation_key is required when force_new is true"
                    )
                if not force_new and creation_key_str is not None:
                    raise ActionReconcileError(
                        "creation_key requires force_new to be true"
                    )

            if incoming_action_id is not None:
                if force_new:
                    raise ActionReconcileError(
                        "action_id cannot be combined with force_new"
                    )
                # Match by action_id
                try:
                    aid = uuid.UUID(str(incoming_action_id))
                except (TypeError, ValueError) as exc:
                    raise ActionReconcileError(
                        "action_id must be a valid UUID"
                    ) from exc
                if aid in matched_ids:
                    raise ActionReconcileError(f"duplicate action_id {aid}")
                child = children_by_id.get(aid)
                if child is None:
                    raise ActionReconcileError(
                        f"action_id {aid} does not belong to parent {retrospective_id}"
                    )
                if child.retrospective_id != retrospective_id:
                    raise ActionReconcileError(
                        f"action_id {aid} does not belong to retrospective {retrospective_id}"
                    )
                if creation_key_str is not None:
                    try:
                        echoed_creation_key = uuid.UUID(str(creation_key_str))
                    except (TypeError, ValueError) as exc:
                        raise ActionReconcileError(
                            "creation_key must be a valid UUID"
                        ) from exc
                    if echoed_creation_key != child.creation_key:
                        raise ActionReconcileError(
                            "action_id creation_key is immutable through save"
                        )
                incoming_due = incoming.get("due_kst_date")
                if isinstance(incoming_due, date):
                    incoming_due_date = incoming_due
                else:
                    incoming_due_date = (
                        date.fromisoformat(str(incoming_due)) if incoming_due else None
                    )
                if (
                    action_text,
                    incoming.get("owner"),
                    incoming.get("issue_id"),
                    incoming_due_date,
                ) != (
                    child.action,
                    child.owner,
                    child.issue_id,
                    child.due_kst_date,
                ):
                    raise ActionReconcileError(
                        "action_id canonical tuple is immutable through save; "
                        "create an amendment and use the transition API"
                    )
                matched_ids.add(aid)
                # Status handling: omitted = preserve, explicit = validate
                if "status" in incoming and incoming["status"] is not None:
                    if incoming["status"] != child.status:
                        raise ActionReconcileError(
                            f"cannot change status from '{child.status}' to "
                            f"'{incoming['status']}' through save; use the transition API"
                        )
                # Update position
                child.position = next_position
                next_position += 1
                continue

            if force_new and creation_key_str is not None:
                try:
                    ckey = uuid.UUID(str(creation_key_str))
                except (TypeError, ValueError) as exc:
                    raise ActionReconcileError(
                        "creation_key must be a valid UUID"
                    ) from exc
                if ckey in request_creation_keys:
                    raise ActionReconcileError(f"duplicate creation_key {ckey}")
                request_creation_keys.add(ckey)
                # Idempotent: reuse existing child with this creation_key
                existing = children_by_ckey.get(ckey)
                if existing is not None:
                    if existing.id in matched_ids:
                        raise ActionReconcileError(
                            f"creation_key {ckey} references an action already matched"
                        )
                    if self._canonical_tuple(incoming, action_text) != (
                        existing.action,
                        existing.owner,
                        existing.issue_id,
                        existing.due_kst_date,
                    ):
                        raise ActionReconcileError(
                            "creation_key canonical tuple is immutable through save"
                        )
                    if (
                        "status" in incoming
                        and incoming["status"] is not None
                        and incoming["status"] != existing.status
                    ):
                        raise ActionReconcileError(
                            "creation_key status is immutable through save; "
                            "use the transition API"
                        )
                    matched_ids.add(existing.id)
                    existing.position = next_position
                    next_position += 1
                    continue
                # Create new with creation_key
                self._create_new_action(
                    retrospective_id=retrospective_id,
                    position=next_position,
                    incoming=incoming,
                    action_text=action_text,
                    actor=actor,
                    creation_key=ckey,
                )
                known_tuples.add(self._canonical_tuple(incoming, action_text))
                next_position += 1
                continue

            # Occurrence-aware matching: find first unmatched child with
            # exact tuple (action, owner, issue_id, due_kst_date)
            matched = self._find_match(
                display_children, matched_ids, incoming, action_text
            )
            if matched is not None:
                matched_ids.add(matched.id)
                # Status: omitted = preserve, explicit = validate
                if "status" in incoming and incoming["status"] is not None:
                    if incoming["status"] != matched.status:
                        raise ActionReconcileError(
                            f"cannot change status from '{matched.status}' to "
                            f"'{incoming['status']}' through save; use the transition API"
                        )
                matched.position = next_position
                next_position += 1
            else:
                canonical_tuple = self._canonical_tuple(incoming, action_text)
                if canonical_tuple in known_tuples:
                    raise ActionReconcileError(
                        "an additional identical action occurrence requires "
                        "force_new=true with a stable creation_key"
                    )
                # Create new action
                self._create_new_action(
                    retrospective_id=retrospective_id,
                    position=next_position,
                    incoming=incoming,
                    action_text=action_text,
                    actor=actor,
                    creation_key=None,
                )
                known_tuples.add(canonical_tuple)
                next_position += 1

        # Omitted children follow in their prior relative order
        for child in display_children:
            if child.id not in matched_ids:
                child.position = next_position
                next_position += 1

        await self.db.flush()

        # Build and write projection
        await self._rebuild_projection(retrospective_id)

    def _find_match(
        self,
        children: list[TradeRetrospectiveAction],
        matched_ids: set[uuid.UUID],
        incoming: dict[str, Any],
        action_text: str,
    ) -> TradeRetrospectiveAction | None:
        """Find first unmatched child with exact canonical tuple."""
        incoming_owner = incoming.get("owner")
        incoming_issue_id = incoming.get("issue_id")
        incoming_due_date = self._incoming_due_date(incoming)
        for child in children:
            if child.id in matched_ids:
                continue
            if child.action != action_text:
                continue
            if child.owner != incoming_owner:
                continue
            if child.issue_id != incoming_issue_id:
                continue
            if child.due_kst_date != incoming_due_date:
                continue
            return child
        return None

    @staticmethod
    def _incoming_due_date(incoming: dict[str, Any]) -> date | None:
        incoming_due = incoming.get("due_kst_date")
        if isinstance(incoming_due, date):
            return incoming_due
        return date.fromisoformat(str(incoming_due)) if incoming_due else None

    @classmethod
    def _canonical_tuple(
        cls, incoming: dict[str, Any], action_text: str
    ) -> tuple[str, Any, Any, date | None]:
        return (
            action_text,
            incoming.get("owner"),
            incoming.get("issue_id"),
            cls._incoming_due_date(incoming),
        )

    def _create_new_action(
        self,
        *,
        retrospective_id: int,
        position: int,
        incoming: dict[str, Any],
        action_text: str,
        actor: str,
        creation_key: uuid.UUID | None,
    ) -> TradeRetrospectiveAction:
        """Create a new canonical action row."""
        status = incoming.get("status")
        if status is None:
            status = "open"
        if status not in _VALID_INITIAL_STATUSES:
            raise ActionReconcileError(
                f"cannot create action with terminal status '{status}'; "
                f"initial status must be one of {sorted(_VALID_INITIAL_STATUSES)}"
            )

        due_str = incoming.get("due_kst_date")
        due_date = date.fromisoformat(due_str) if due_str else None

        # Build legacy_payload from incoming (preserves unknown keys)
        legacy_payload: dict[str, Any] = {}
        for k, v in incoming.items():
            if k not in (
                "action_id",
                "force_new",
                "creation_key",
                "version",
                "action",
                "owner",
                "issue_id",
                "status",
                "due_kst_date",
            ):
                legacy_payload[k] = v
        # Also include the canonical fields in legacy_payload
        legacy_payload["action"] = action_text
        legacy_payload["owner"] = incoming.get("owner")
        legacy_payload["issue_id"] = incoming.get("issue_id")
        legacy_payload["status"] = status
        legacy_payload["due_kst_date"] = due_str

        row = TradeRetrospectiveAction(
            retrospective_id=retrospective_id,
            position=position,
            action=action_text,
            owner=incoming.get("owner"),
            issue_id=incoming.get("issue_id"),
            status=status,
            due_kst_date=due_date,
            version=1,
            status_actor=actor,
            status_source="retrospective_save",
            legacy_payload=legacy_payload,
            creation_key=creation_key,
        )
        self.db.add(row)
        return row

    # -- Projection ---------------------------------------------------------

    async def _rebuild_projection(self, retrospective_id: int) -> None:
        """Rebuild parent JSONB projection from canonical children.

        Sets the GUC marker before writing so the write-fence trigger permits
        the update in canonical mode.
        """
        # Read children (already locked by caller)
        result = await self.db.execute(
            select(TradeRetrospectiveAction)
            .where(TradeRetrospectiveAction.retrospective_id == retrospective_id)
            .order_by(TradeRetrospectiveAction.position, TradeRetrospectiveAction.id)
        )
        children = result.scalars().all()

        projection = [self._build_projection_item(c) for c in children]

        # Set GUC marker so the write-fence trigger permits this write
        await self.db.execute(text(f"SET LOCAL {_GUC_MARKER} = '{_GUC_VALUE}'"))

        # Update parent JSONB — only next_actions, nothing else
        await self.db.execute(
            text(
                "UPDATE review.trade_retrospectives SET next_actions = "
                "CAST(:proj AS jsonb) WHERE id = :rid"
            ),
            {"proj": json.dumps(projection), "rid": retrospective_id},
        )

    def _build_projection_item(self, child: TradeRetrospectiveAction) -> dict[str, Any]:
        """Build a single projection item from a canonical child.

        Starts from legacy_payload, overlays canonical fields, adds action_id
        and version. Preserves unknown keys from legacy_payload.
        """
        # Start from legacy_payload (copy)
        item: dict[str, Any] = {}
        if child.legacy_payload and isinstance(child.legacy_payload, dict):
            item.update(child.legacy_payload)
        item.pop("force_new", None)

        # Overlay canonical fields
        item["action"] = child.action
        item["owner"] = child.owner
        item["issue_id"] = child.issue_id

        # Map terminal states to legacy vocabulary
        if child.status in _TERMINAL_STATUSES:
            item["status"] = "done"
            if child.status in ("obsolete", "expired"):
                item["terminal_status"] = child.status
        else:
            item["status"] = child.status
            # Remove terminal_status if it was in legacy_payload but child is active
            item.pop("terminal_status", None)

        item["due_kst_date"] = (
            child.due_kst_date.isoformat() if child.due_kst_date else None
        )

        # Additive fields
        item["action_id"] = str(child.id)
        item["version"] = child.version
        if child.creation_key is not None:
            item["creation_key"] = str(child.creation_key)
        else:
            item.pop("creation_key", None)

        return item

    # -- Canonical query ----------------------------------------------------

    async def query_actions(
        self,
        *,
        statuses: frozenset[str] | None = None,
        market: str | None = None,
        symbol: str | None = None,
        symbol_search: str | None = None,
        owner: str | None = None,
        issue_id: str | None = None,
        overdue_only: bool = False,
        trigger_type: str | None = None,
        outcome_filter: str | None = None,
        kst_date_from: str | None = None,
        kst_date_to: str | None = None,
        due_before: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Canonical action query with pagination, filters, and ordering.

        Default status filter: open,in_progress (active only).
        Ordering: overdue first, then in_progress, due_date ASC NULLS LAST,
        updated_at DESC, id ASC.
        """
        # Active-default filter
        if statuses is None:
            statuses = _ACTIVE_STATUSES
        unknown_statuses = statuses - VALID_NEXT_ACTION_STATUSES
        if unknown_statuses:
            raise ValueError(f"invalid action statuses: {sorted(unknown_statuses)}")

        # Build filters
        filters = [
            TradeRetrospectiveAction.status.in_(statuses),
        ]

        # Join with parent for context fields
        parent_filters = []
        if market is not None:
            parent_filters.append(TradeRetrospective.market == market)
        if symbol is not None:
            parent_filters.append(TradeRetrospective.symbol == symbol.strip().upper())
        if symbol_search is not None and symbol_search.strip():
            escaped = (
                symbol_search.strip()
                .upper()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            parent_filters.append(
                TradeRetrospective.symbol.ilike(f"{escaped}%", escape="\\")
            )
        if trigger_type is not None:
            parent_filters.append(TradeRetrospective.trigger_type == trigger_type)
        if outcome_filter is not None:
            parent_filters.append(outcome_filter_predicate(outcome_filter))
        if kst_date_from is not None:
            parent_filters.append(
                TradeRetrospective.created_at >= kst_day_start(kst_date_from)
            )
        if kst_date_to is not None:
            parent_filters.append(
                TradeRetrospective.created_at <= kst_day_end(kst_date_to)
            )

        if owner is not None:
            filters.append(TradeRetrospectiveAction.owner == owner)
        if issue_id is not None:
            filters.append(TradeRetrospectiveAction.issue_id == issue_id)
        if due_before is not None:
            filters.append(
                TradeRetrospectiveAction.due_kst_date < date.fromisoformat(due_before)
            )

        # Overdue filter: active AND due_kst_date < today (KST)
        today_kst = now_kst().date()
        overdue_expr = and_(
            TradeRetrospectiveAction.status.in_(("open", "in_progress")),
            TradeRetrospectiveAction.due_kst_date.isnot(None),
            TradeRetrospectiveAction.due_kst_date < today_kst,
        )
        if overdue_only:
            filters.append(overdue_expr)

        # Build the join
        join_condition = (
            TradeRetrospectiveAction.retrospective_id == TradeRetrospective.id
        )

        filtered = (
            select(
                TradeRetrospectiveAction.id.label("action_id"),
                TradeRetrospectiveAction.version,
                TradeRetrospectiveAction.action,
                TradeRetrospectiveAction.owner,
                TradeRetrospectiveAction.issue_id,
                TradeRetrospectiveAction.status,
                TradeRetrospectiveAction.due_kst_date,
                TradeRetrospectiveAction.status_changed_at,
                TradeRetrospectiveAction.resolved_at,
                TradeRetrospectiveAction.status_actor,
                TradeRetrospectiveAction.status_source,
                TradeRetrospectiveAction.status_reason,
                TradeRetrospectiveAction.retrospective_id,
                TradeRetrospectiveAction.updated_at.label("action_updated_at"),
                TradeRetrospective.symbol.label("symbol"),
                TradeRetrospective.market.label("market"),
                TradeRetrospective.trigger_type.label("trigger_type"),
                TradeRetrospective.outcome.label("outcome"),
                TradeRetrospective.realized_pnl.label("realized_pnl"),
                TradeRetrospective.correlation_id.label("correlation_id"),
                TradeRetrospective.created_at.label("parent_created_at"),
                case((overdue_expr, 0), else_=1).label("overdue_rank"),
                case(
                    (TradeRetrospectiveAction.status == "in_progress", 0),
                    else_=1,
                ).label("progress_rank"),
            )
            .join(TradeRetrospective, join_condition)
            .where(*filters, *parent_filters)
            .cte("filtered_actions")
        )
        page_stmt = select(filtered).order_by(
            filtered.c.overdue_rank,
            filtered.c.progress_rank,
            filtered.c.due_kst_date.asc().nullslast(),
            filtered.c.action_updated_at.desc(),
            filtered.c.action_id.asc(),
        )
        if limit is not None:
            page_stmt = page_stmt.limit(limit)
        if offset:
            page_stmt = page_stmt.offset(offset)
        page = page_stmt.cte("action_page")
        totals = (
            select(func.count().label("total"))
            .select_from(filtered)
            .cte("action_totals")
        )
        page_columns = list(page.c)
        stmt = (
            select(totals.c.total, *page_columns)
            .select_from(totals.outerjoin(page, true()))
            .order_by(
                page.c.overdue_rank,
                page.c.progress_rank,
                page.c.due_kst_date.asc().nullslast(),
                page.c.action_updated_at.desc(),
                page.c.action_id.asc(),
            )
        )
        rows = (await self.db.execute(stmt)).mappings().all()
        total = int(rows[0]["total"]) if rows else 0

        items = []
        for row in rows:
            if row["action_id"] is None:
                continue

            is_overdue = (
                row["status"] in ("open", "in_progress")
                and row["due_kst_date"] is not None
                and row["due_kst_date"] < today_kst
            )

            items.append(
                {
                    "action_id": str(row["action_id"]),
                    "version": row["version"],
                    "action": row["action"],
                    "owner": row["owner"],
                    "issue_id": row["issue_id"],
                    "status": row["status"],
                    "due_kst_date": (
                        row["due_kst_date"].isoformat() if row["due_kst_date"] else None
                    ),
                    "overdue": is_overdue,
                    "status_changed_at": (
                        row["status_changed_at"].isoformat()
                        if row["status_changed_at"]
                        else None
                    ),
                    "resolved_at": (
                        row["resolved_at"].isoformat() if row["resolved_at"] else None
                    ),
                    "status_actor": row["status_actor"],
                    "status_source": row["status_source"],
                    "status_reason": row["status_reason"],
                    "retrospective_id": row["retrospective_id"],
                    "correlation_id": row["correlation_id"],
                    "symbol": row["symbol"],
                    "market": row["market"],
                    "trigger_type": row["trigger_type"],
                    "outcome": row["outcome"],
                    "realized_pnl": (
                        float(row["realized_pnl"])
                        if row["realized_pnl"] is not None
                        else None
                    ),
                    "created_at": (
                        row["parent_created_at"].isoformat()
                        if row["parent_created_at"]
                        else None
                    ),
                }
            )

        return {
            "total": int(total),
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "as_of": datetime.now(UTC),
            "items": items,
        }


# ---------------------------------------------------------------------------
# Cutover
# ---------------------------------------------------------------------------


async def run_cutover(
    conn: AsyncConnection,
    *,
    if_shadow: bool = False,
    lock_timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """Run the canonical cutover in the caller's transaction.

    Steps:
    1. Take transaction-scoped advisory lock and read control mode.
    2. If already canonical and if_shadow, return without heavy table locks.
    3. In shadow mode, lock parent → control → actions and re-read control mode.
    4. Delete all shadow children.
    5. Rebuild from frozen parent JSON (same backfill as migration).
    6. Verify full parity (count, ordinal, all canonical fields).
    7. Switch mode to canonical atomically.

    On parity failure, the entire transaction (including the mode switch) is
    left for the caller to roll back.
    """
    if lock_timeout_ms <= 0:
        raise ValueError("lock_timeout_ms must be positive")

    await conn.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{lock_timeout_ms}ms"},
    )

    # 1. Advisory lock (transaction-scoped). Do not queue indefinitely behind
    # another cutover; the deploy can retry safely while mode remains shadow.
    try:
        lock_result = await conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": _CUTOVER_ADVISORY_LOCK_ID},
        )
    except DBAPIError as exc:
        raise CutoverLockError("failed to acquire cutover advisory lock") from exc
    if not lock_result.scalar_one():
        raise CutoverLockError("cutover advisory lock is already held")

    # Avoid blocking writers on every post-cutover deploy. The advisory lock
    # serializes compliant cutovers; the second read after table locking closes
    # the gap against an out-of-band control-row update.
    ctrl_row = await _read_cutover_control(conn)
    idempotent_result = _validate_cutover_mode(ctrl_row, if_shadow=if_shadow)
    if idempotent_result is not None:
        return idempotent_result

    # 2. Lock tables in order: parent → control → actions
    try:
        await conn.execute(
            text("LOCK TABLE review.trade_retrospectives IN SHARE ROW EXCLUSIVE MODE")
        )
        await conn.execute(
            text(
                "LOCK TABLE review.trade_retrospective_action_control "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        await conn.execute(
            text(
                "LOCK TABLE review.trade_retrospective_actions "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    except DBAPIError as exc:
        raise CutoverLockError(
            f"cutover table lock timed out after {lock_timeout_ms}ms"
        ) from exc

    # 3. Re-read while the frozen table set is held.
    ctrl_row = await _read_cutover_control(conn, for_update=True)
    idempotent_result = _validate_cutover_mode(ctrl_row, if_shadow=if_shadow)
    if idempotent_result is not None:
        return idempotent_result

    await _validate_cutover_input(conn)

    # 4. Delete all existing children
    await conn.execute(text("DELETE FROM review.trade_retrospective_actions"))

    # 5. Rebuild from frozen parent JSON (same backfill SQL as migration)
    await conn.execute(
        text(
            """
            INSERT INTO review.trade_retrospective_actions (
                id, retrospective_id, creation_key, position, action,
                owner, issue_id, status, due_kst_date, version,
                status_changed_at, resolved_at,
                status_actor, status_source, status_reason, status_evidence,
                legacy_payload, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                t.id,
                CASE
                    WHEN elem.value ? 'creation_key'
                        THEN (elem.value->>'creation_key')::uuid
                    ELSE NULL
                END,
                (elem.ordinality - 1)::integer,
                btrim(elem.value->>'action'),
                elem.value->>'owner',
                elem.value->>'issue_id',
                CASE
                    WHEN btrim(COALESCE(elem.value->>'status', '')) = ''
                        THEN 'open'
                    ELSE elem.value->>'status'
                END,
                CASE
                    WHEN btrim(COALESCE(elem.value->>'due_kst_date', '')) = ''
                        THEN NULL
                    ELSE (elem.value->>'due_kst_date')::date
                END,
                1,
                t.updated_at,
                CASE
                    WHEN elem.value->>'status' = 'done' THEN t.updated_at
                    ELSE NULL
                END,
                'migration:rob-878',
                'migration',
                NULL,
                CASE
                    WHEN elem.value->>'status' = 'done' THEN
                        jsonb_build_object(
                            'schema_version', 1,
                            'kind', 'legacy_status',
                            'source', 'migration',
                            'reference', 'review.trade_retrospectives.next_actions',
                            'observed_at', t.updated_at,
                            'summary', 'historical done; exact completion time unavailable'
                        )
                    ELSE NULL
                END,
                elem.value,
                t.created_at,
                t.updated_at
            FROM review.trade_retrospectives t
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(t.next_actions) = 'array'
                        THEN t.next_actions
                    ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS elem(value, ordinality)
            """
        )
    )

    # 6. Verify full parity (count, ordinal, all canonical fields)
    await _verify_parity(conn)

    # Count actions
    count_result = await conn.execute(
        text("SELECT count(*) FROM review.trade_retrospective_actions")
    )
    action_count = count_result.scalar_one()

    # 7. Switch mode to canonical atomically
    await conn.execute(
        text(
            "UPDATE review.trade_retrospective_action_control "
            "SET mode = 'canonical', cutover_at = now(), "
            "    cutover_action_count = :count, updated_at = now() "
            "WHERE id = 1"
        ),
        {"count": action_count},
    )

    return {
        "mode": "canonical",
        "action_count": action_count,
        "cutover_at": datetime.now(UTC),
        "idempotent": False,
    }


async def _read_cutover_control(
    conn: AsyncConnection, *, for_update: bool = False
) -> Any:
    suffix = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        text(
            "SELECT mode, cutover_at, cutover_action_count "
            "FROM review.trade_retrospective_action_control WHERE id = 1" + suffix
        )
    )
    return result.fetchone()


def _validate_cutover_mode(ctrl_row: Any, *, if_shadow: bool) -> dict[str, Any] | None:
    if ctrl_row is None:
        raise ActionControlError(
            "retrospective action control row is missing; cutover aborted"
        )
    current_mode = ctrl_row.mode
    if current_mode == "canonical":
        if not if_shadow:
            raise ActionControlError(
                "retrospective action mode is already canonical; "
                "use --if-shadow for an idempotent no-op"
            )
        if ctrl_row.cutover_at is None or ctrl_row.cutover_action_count is None:
            raise ActionControlError(
                "canonical control row is missing cutover metadata; "
                "manual recovery is required"
            )
        return {
            "mode": "canonical",
            "action_count": ctrl_row.cutover_action_count,
            "cutover_at": ctrl_row.cutover_at,
            "idempotent": True,
        }
    if current_mode != "shadow":
        raise ActionControlError(
            f'cannot cutover from mode "{current_mode}"; expected shadow'
        )
    return None


async def _validate_cutover_input(conn: AsyncConnection) -> None:
    """Validate the locked parent JSON snapshot before destructive rebuild."""
    result = await conn.execute(
        text(
            "SELECT id, next_actions FROM review.trade_retrospectives "
            "WHERE next_actions IS NOT NULL ORDER BY id"
        )
    )
    for row in result:
        actions = row.next_actions
        if actions is not None and not isinstance(actions, list):
            raise CutoverParityError(
                f"retrospective {row.id}: next_actions is not an array"
            )
        creation_keys: set[uuid.UUID] = set()
        for index, item in enumerate(actions or []):
            prefix = f"retrospective {row.id} action[{index}]"
            if not isinstance(item, dict):
                raise CutoverParityError(f"{prefix}: element is not an object")

            action = item.get("action")
            if action is not None and not isinstance(action, str):
                raise CutoverParityError(f"{prefix}: action must be a string")
            if not isinstance(action, str) or not action.strip():
                raise CutoverParityError(f"{prefix}: blank action")

            if "force_new" in item:
                raise CutoverParityError(
                    f"{prefix}: force_new is transport-only and must not be persisted"
                )

            raw_creation_key = item.get("creation_key")
            if "creation_key" in item:
                if not isinstance(raw_creation_key, str):
                    raise CutoverParityError(f"{prefix}: invalid creation_key")
                try:
                    creation_key = uuid.UUID(raw_creation_key)
                except ValueError as exc:
                    raise CutoverParityError(f"{prefix}: invalid creation_key") from exc
                if creation_key in creation_keys:
                    raise CutoverParityError(
                        f"{prefix}: duplicate creation_key {creation_key}"
                    )
                creation_keys.add(creation_key)

            status = item.get("status")
            if status is not None:
                if not isinstance(status, str):
                    raise CutoverParityError(f"{prefix}: unknown status {status!r}")
                if status.strip(" ") and status not in {
                    "open",
                    "in_progress",
                    "done",
                }:
                    raise CutoverParityError(f"{prefix}: unknown status {status!r}")

            raw_due = item.get("due_kst_date")
            if raw_due is None:
                continue
            if not isinstance(raw_due, str):
                raise CutoverParityError(f"{prefix}: invalid due_kst_date {raw_due!r}")
            if raw_due.strip(" ") == "":
                continue
            try:
                parsed_due = date.fromisoformat(raw_due)
            except ValueError as exc:
                raise CutoverParityError(
                    f"{prefix}: invalid due_kst_date {raw_due!r}"
                ) from exc
            if parsed_due.isoformat() != raw_due:
                raise CutoverParityError(f"{prefix}: invalid due_kst_date {raw_due!r}")


async def _verify_parity(conn: AsyncConnection) -> None:
    """Verify count and full field parity between parent JSONB and children.

    Raises CutoverParityError on any mismatch.
    """
    # Count parity
    count_result = await conn.execute(
        text(
            """
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN jsonb_typeof(next_actions) = 'array'
                        THEN jsonb_array_length(next_actions)
                        ELSE 0
                    END
                ), 0) AS parent_count,
                (SELECT count(*) FROM review.trade_retrospective_actions) AS child_count
            FROM review.trade_retrospectives
            """
        )
    )
    counts = count_result.one()
    if counts.parent_count != counts.child_count:
        raise CutoverParityError(
            f"count mismatch: parent has {counts.parent_count} actions, "
            f"child has {counts.child_count}"
        )

    # Full field parity
    mismatch_result = await conn.execute(
        text(
            """
            WITH expected AS (
                SELECT
                    t.id AS retrospective_id,
                    (elem.ordinality - 1)::integer AS position,
                    CASE
                        WHEN elem.value ? 'creation_key'
                            THEN (elem.value->>'creation_key')::uuid
                        ELSE NULL
                    END AS creation_key,
                    btrim(elem.value->>'action') AS action,
                    elem.value->>'owner' AS owner,
                    elem.value->>'issue_id' AS issue_id,
                    CASE
                        WHEN btrim(COALESCE(elem.value->>'status', '')) = ''
                            THEN 'open'
                        ELSE elem.value->>'status'
                    END AS status,
                    CASE
                        WHEN btrim(COALESCE(elem.value->>'due_kst_date', '')) = ''
                            THEN NULL
                        ELSE (elem.value->>'due_kst_date')::date
                    END AS due_kst_date,
                    t.updated_at AS status_changed_at,
                    CASE
                        WHEN elem.value->>'status' = 'done' THEN t.updated_at
                        ELSE NULL
                    END AS resolved_at,
                    NULL::text AS status_reason,
                    CASE
                        WHEN elem.value->>'status' = 'done' THEN
                            jsonb_build_object(
                                'schema_version', 1,
                                'kind', 'legacy_status',
                                'source', 'migration',
                                'reference', 'review.trade_retrospectives.next_actions',
                                'observed_at', t.updated_at,
                                'summary', 'historical done; exact completion time unavailable'
                            )
                        ELSE NULL
                    END AS status_evidence,
                    elem.value AS legacy_payload,
                    t.created_at AS created_at,
                    t.updated_at AS updated_at
                FROM review.trade_retrospectives t
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(t.next_actions) = 'array'
                            THEN t.next_actions
                        ELSE '[]'::jsonb
                    END
                ) WITH ORDINALITY AS elem(value, ordinality)
            )
            SELECT e.retrospective_id, e.position
            FROM expected e
            LEFT JOIN review.trade_retrospective_actions a
              ON a.retrospective_id = e.retrospective_id
             AND a.position = e.position
            WHERE a.id IS NULL
               OR a.creation_key IS DISTINCT FROM e.creation_key
               OR a.action IS DISTINCT FROM e.action
               OR a.owner IS DISTINCT FROM e.owner
               OR a.issue_id IS DISTINCT FROM e.issue_id
               OR a.status IS DISTINCT FROM e.status
               OR a.due_kst_date IS DISTINCT FROM e.due_kst_date
               OR a.version <> 1
               OR a.status_changed_at IS DISTINCT FROM e.status_changed_at
               OR a.resolved_at IS DISTINCT FROM e.resolved_at
               OR a.status_actor <> 'migration:rob-878'
               OR a.status_source <> 'migration'
               OR a.status_reason IS DISTINCT FROM e.status_reason
               OR a.status_evidence IS DISTINCT FROM e.status_evidence
               OR a.legacy_payload IS DISTINCT FROM e.legacy_payload
               OR a.created_at IS DISTINCT FROM e.created_at
               OR a.updated_at IS DISTINCT FROM e.updated_at
            ORDER BY e.retrospective_id, e.position
            LIMIT 1
            """
        )
    )
    mismatch = mismatch_result.fetchone()
    if mismatch is not None:
        raise CutoverParityError(
            f"parity mismatch: retrospective {mismatch.retrospective_id} "
            f"action[{mismatch.position}] field/ordinal mismatch"
        )
