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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.timezone import now_kst
from app.models.review import (
    TradeRetrospective,
    TradeRetrospectiveAction,
    TradeRetrospectiveActionControl,
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

    def _action_to_dict(self, a: TradeRetrospectiveAction) -> dict[str, Any]:
        return {
            "action_id": a.id,
            "action": a.action,
            "owner": a.owner,
            "issue_id": a.issue_id,
            "status": a.status,
            "due_kst_date": a.due_kst_date.isoformat() if a.due_kst_date else None,
            "version": a.version,
            "position": a.position,
        }

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

        for incoming in actions:
            action_text = (incoming.get("action") or "").strip()
            if not action_text:
                raise ActionReconcileError("action must be a non-empty string")

            incoming_action_id = incoming.get("action_id")
            force_new = incoming.get("force_new", False)
            creation_key_str = incoming.get("creation_key")

            if incoming_action_id is not None:
                # Match by action_id
                aid = uuid.UUID(str(incoming_action_id))
                child = children_by_id.get(aid)
                if child is None:
                    raise ActionReconcileError(
                        f"action_id {aid} does not belong to parent {retrospective_id}"
                    )
                if child.retrospective_id != retrospective_id:
                    raise ActionReconcileError(
                        f"action_id {aid} does not belong to retrospective {retrospective_id}"
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
                ckey = uuid.UUID(str(creation_key_str))
                # Idempotent: reuse existing child with this creation_key
                existing = children_by_ckey.get(ckey)
                if existing is not None:
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
                next_position += 1
                continue

            # Occurrence-aware matching: find first unmatched child with
            # exact tuple (action, owner, issue_id, due_kst_date)
            matched = self._find_match(
                existing_children, matched_ids, incoming, action_text
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
                # Create new action
                self._create_new_action(
                    retrospective_id=retrospective_id,
                    position=next_position,
                    incoming=incoming,
                    action_text=action_text,
                    actor=actor,
                    creation_key=None,
                )
                next_position += 1

        # Omitted children follow in their prior relative order
        for child in existing_children:
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
        incoming_due = incoming.get("due_kst_date")
        incoming_due_date = date.fromisoformat(incoming_due) if incoming_due else None
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
        limit: int = 50,
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

        if owner is not None:
            filters.append(TradeRetrospectiveAction.owner == owner)
        if issue_id is not None:
            filters.append(TradeRetrospectiveAction.issue_id == issue_id)

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

        # Count total
        count_stmt = (
            select(func.count())
            .select_from(TradeRetrospectiveAction)
            .join(TradeRetrospective, join_condition)
            .where(*filters, *parent_filters)
        )
        total = (await self.db.execute(count_stmt)).scalar_one()

        today_kst = now_kst().date()
        overdue_case = text(
            "CASE WHEN trade_retrospective_actions.status IN ('open','in_progress') "
            "AND trade_retrospective_actions.due_kst_date IS NOT NULL "
            "AND trade_retrospective_actions.due_kst_date < :today_kst "
            "THEN 0 ELSE 1 END"
        ).bindparams(today_kst=today_kst)

        progress_order = text(
            "CASE WHEN trade_retrospective_actions.status = 'in_progress' "
            "THEN 0 ELSE 1 END"
        )

        stmt = (
            select(
                TradeRetrospectiveAction,
                TradeRetrospective.symbol,
                TradeRetrospective.market,
                TradeRetrospective.trigger_type,
                TradeRetrospective.outcome,
                TradeRetrospective.realized_pnl,
                TradeRetrospective.correlation_id,
                TradeRetrospective.created_at.label("parent_created_at"),
            )
            .join(TradeRetrospective, join_condition)
            .where(*filters, *parent_filters)
            .order_by(
                overdue_case,
                progress_order,
                TradeRetrospectiveAction.due_kst_date.asc().nullslast(),
                TradeRetrospectiveAction.updated_at.desc(),
                TradeRetrospectiveAction.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )

        rows = (await self.db.execute(stmt)).all()

        items = []
        for row in rows:
            action = row[0]
            parent_symbol = row[1]
            parent_market = row[2]
            parent_trigger = row[3]
            parent_outcome = row[4]
            parent_pnl = row[5]
            parent_cid = row[6]
            parent_created = row[7]

            is_overdue = (
                action.status in ("open", "in_progress")
                and action.due_kst_date is not None
                and action.due_kst_date < today_kst
            )

            items.append(
                {
                    "action_id": str(action.id),
                    "version": action.version,
                    "action": action.action,
                    "owner": action.owner,
                    "issue_id": action.issue_id,
                    "status": action.status,
                    "due_kst_date": (
                        action.due_kst_date.isoformat() if action.due_kst_date else None
                    ),
                    "overdue": is_overdue,
                    "status_changed_at": (
                        action.status_changed_at.isoformat()
                        if action.status_changed_at
                        else None
                    ),
                    "resolved_at": (
                        action.resolved_at.isoformat() if action.resolved_at else None
                    ),
                    "status_actor": action.status_actor,
                    "status_source": action.status_source,
                    "retrospective_id": action.retrospective_id,
                    "correlation_id": parent_cid,
                    "symbol": parent_symbol,
                    "market": parent_market,
                    "trigger_type": parent_trigger,
                    "outcome": parent_outcome,
                    "realized_pnl": (
                        float(parent_pnl) if parent_pnl is not None else None
                    ),
                    "created_at": (
                        parent_created.isoformat() if parent_created else None
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
    conn: AsyncConnection, *, if_shadow: bool = False
) -> dict[str, Any]:
    """Run the canonical cutover in the caller's transaction.

    Steps:
    1. Take transaction-scoped advisory lock.
    2. LOCK TABLE parent IN SHARE ROW EXCLUSIVE MODE.
    3. Read control row; if already canonical and if_shadow, return idempotent.
    4. Delete all shadow children.
    5. Rebuild from frozen parent JSON (same backfill as migration).
    6. Verify full parity (count, ordinal, all canonical fields).
    7. Switch mode to canonical atomically.

    On parity failure, the entire transaction (including the mode switch) is
    left for the caller to roll back.
    """
    # 1. Advisory lock (transaction-scoped)
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _CUTOVER_ADVISORY_LOCK_ID},
    )

    # 2. Lock tables in order: parent → control → actions
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
            "LOCK TABLE review.trade_retrospective_actions IN SHARE ROW EXCLUSIVE MODE"
        )
    )

    # 3. Read control row
    ctrl_result = await conn.execute(
        text(
            "SELECT mode, cutover_at, cutover_action_count "
            "FROM review.trade_retrospective_action_control WHERE id = 1"
        )
    )
    ctrl_row = ctrl_result.fetchone()
    if ctrl_row is None:
        raise ActionControlError(
            "retrospective action control row is missing; cutover aborted"
        )
    current_mode = ctrl_row.mode

    if current_mode == "canonical":
        # Idempotent: already canonical
        return {
            "mode": "canonical",
            "action_count": ctrl_row.cutover_action_count or 0,
            "cutover_at": ctrl_row.cutover_at,
            "idempotent": True,
        }

    if current_mode != "shadow":
        raise ActionControlError(
            f'cannot cutover from mode "{current_mode}"; expected shadow'
        )

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
                NULL,
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
            "    cutover_action_count = :count "
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
                    elem.value AS legacy_payload
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
               OR a.creation_key IS NOT NULL
               OR a.action IS DISTINCT FROM e.action
               OR a.owner IS DISTINCT FROM e.owner
               OR a.issue_id IS DISTINCT FROM e.issue_id
               OR a.status IS DISTINCT FROM e.status
               OR a.due_kst_date IS DISTINCT FROM e.due_kst_date
               OR a.version <> 1
               OR a.status_actor <> 'migration:rob-878'
               OR a.status_source <> 'migration'
               OR a.legacy_payload IS DISTINCT FROM e.legacy_payload
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
