"""Sole writer and published-state reader for KR daily plans."""

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.daily_plan import bundles, heads, links, rows, versions
from app.services.daily_plan.bundle import (
    bundle_sha256,
    canonical_bundle,
    canonical_json,
)
from app.services.daily_plan.validator import (
    parse_time,
    prepare_row,
    require_base_version,
    row_key,
)
from app.services.daily_plan.vocabulary import VERSION_KINDS
from app.services.daily_plan.watch import watch_state
from app.services.market_events.session_calendar import (
    next_trading_session,
    previous_trading_session,
    regular_session_bounds,
)

_KST = ZoneInfo("Asia/Seoul")
_PLAN_NAMESPACE = UUID("ed870506-5d0b-5907-9c26-ab799da98b1b")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


async def _lock_head(db: AsyncSession, day: date) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(6751, day.toordinal())))


async def _head(db: AsyncSession, day: date) -> UUID:
    existing = await db.scalar(select(heads.c.id).where(heads.c.session_date == day))
    if existing is not None:
        return existing
    head_id = uuid4()
    await db.execute(
        heads.insert().values(
            id=head_id,
            market="kr",
            session_date=day,
            cutoff_at=datetime.combine(day, time(8, 30), _KST),
        )
    )
    return head_id


async def store_input_bundle(
    db: AsyncSession,
    *,
    day: date,
    body: dict[str, Any],
    claimed_sha256: str,
) -> UUID:
    """Called by the server-side input assembler in its write transaction."""
    canonical = canonical_bundle(body)
    if hashlib.sha256(canonical).hexdigest() != claimed_sha256:
        raise ValueError("bundle_hash_mismatch")
    normalized = json.loads(canonical)
    await _lock_head(db, day)
    head_id = await _head(db, day)
    existing = await db.scalar(
        select(bundles.c.id).where(
            bundles.c.head_id == head_id,
            bundles.c.sha256 == claimed_sha256,
        )
    )
    if existing is not None:
        return existing
    bundle_id = uuid4()
    await db.execute(
        bundles.insert().values(
            id=bundle_id,
            head_id=head_id,
            sha256=claimed_sha256,
            body=normalized,
        )
    )
    return bundle_id


async def _version_rows(db: AsyncSession, version_id: UUID) -> list[dict]:
    result = await db.execute(select(rows).where(rows.c.version_id == version_id))
    return [dict(row) for row in result.mappings()]


async def _fold(db: AsyncSession, head_id: UUID, seq: int) -> list[dict]:
    stmt = (
        select(rows)
        .join(versions, rows.c.version_id == versions.c.id)
        .where(versions.c.head_id == head_id, versions.c.seq <= seq)
        .order_by(versions.c.seq, rows.c.id)
    )
    result = await db.execute(stmt)
    return list({row_key(row): dict(row) for row in result.mappings()}.values())


def _row_view(row: dict) -> dict:
    return {
        **row["body"],
        **{
            key: row[key]
            for key in (
                "id",
                "action",
                "execution_state",
                "adoption_key",
                "forecast_id",
            )
        },
    }


async def _write_result(db: AsyncSession, version: dict) -> dict:
    return {
        **version,
        "rows": [_row_view(row) for row in await _version_rows(db, version["id"])],
    }


async def write_version(
    db: AsyncSession,
    *,
    day: date,
    request: dict[str, Any],
    now: datetime,
    policy: Any,
    policy_hash: str,
) -> dict:
    """Validate and insert one publication in the caller's transaction."""
    await _lock_head(db, day)
    head_id = await _head(db, day)
    request_hash = _digest(request)
    key = request["idempotency_key"]
    if type(key) is not str or not 1 <= len(key) <= 200:
        raise ValueError("invalid_idempotency_key")
    existing = (
        (
            await db.execute(
                select(versions).where(
                    versions.c.head_id == head_id,
                    versions.c.idempotency_key == key,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["request_sha256"] != request_hash:
            raise ValueError("idempotency_conflict")
        return await _write_result(db, dict(existing))
    seq = (
        await db.scalar(
            select(func.max(versions.c.seq)).where(versions.c.head_id == head_id)
        )
        or 0
    )
    require_base_version(request["expected_prev_seq"], seq)
    kind = request["kind"]
    if kind not in VERSION_KINDS:
        raise ValueError("invalid_version_kind")
    regular = regular_session_bounds("kr", day)
    if regular is None or now.utcoffset() is None or now.astimezone(_KST).date() != day:
        raise ValueError("calendar_unknown")
    cutoff = datetime.combine(day, time(8, 30), _KST)
    asof = parse_time(request["asof"])
    if (
        asof > now
        or asof.astimezone(_KST).date() != day
        or (kind == "premarket" and asof > cutoff)
    ):
        raise ValueError("invalid_version_time")

    bundle_id = (
        UUID(request["input_bundle_id"]) if request.get("input_bundle_id") else None
    )
    bundle_hash = request.get("input_bundle_sha256")
    if (bundle_id is None) != (bundle_hash is None):
        raise ValueError("invalid_bundle_reference")
    bundle_closed_at = asof
    evidence = {}
    if bundle_id:
        bundle = (
            (
                await db.execute(
                    select(bundles).where(
                        bundles.c.head_id == head_id,
                        bundles.c.id == bundle_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if bundle is None:
            bundle_id = None
            bundle_hash = None
        else:
            if (
                bundle["sha256"] != bundle_hash
                or bundle_sha256(bundle["body"]) != bundle_hash
            ):
                raise ValueError("bundle_hash_mismatch")
            bundle_closed_at = parse_time(bundle["body"]["bundle_closed_at"])
            if bundle_closed_at > asof or bundle_closed_at > bundle["created_at"]:
                raise ValueError("invalid_bundle_time")
            evidence = {row_key(row): row for row in bundle["body"]["rows"]}

    previous = previous_trading_session("kr", day)
    previous_bounds = regular_session_bounds("kr", previous) if previous else None
    if previous_bounds is None:
        raise ValueError("calendar_unknown")
    proposed = request["rows"]
    if not proposed or len(proposed) > 1000:
        raise ValueError("invalid_row_count")
    previous_plan = None
    prepared = []
    seen = set()
    for raw in proposed:
        key_for_row = row_key({**raw, "symbol": raw["symbol"].strip().upper()})
        close = _session_close(day, raw["venue"], raw["session"], regular[1])
        row = prepare_row(
            raw,
            evidence=evidence.get(key_for_row),
            asof=now,
            session_close=close,
            bundle_closed_at=bundle_closed_at,
            oldest_accepted_at=previous_bounds[1],
            no_add_floor_pct=Decimal(str(policy.daily_plan.no_add_floor_pct)),
        )
        if row_key(row) in seen:
            raise ValueError("duplicate_row_key")
        seen.add(row_key(row))
        carry_from = row.get("carried_from_row_id")
        if carry_from is not None:
            if previous_plan is None:
                previous_plan = (
                    await get_plan(
                        db,
                        day=previous,
                        seq=None,
                        now=now,
                        max_carries=policy.daily_plan.carry_max_sessions,
                    )
                    or {}
                )
            previous_rows = previous_plan.get("rows", [])
            origin = next(
                (item for item in previous_rows if str(item["id"]) == carry_from), None
            )
            if (
                origin is None
                or row_key(origin) != row_key(row)
                or origin.get("watch_state", {}).get("next_session") != day
                or row["action"] != "watch"
            ):
                raise ValueError("invalid_carry_origin")
            row["carry_count"] = origin["watch_state"]["carry_count"]
        prepared.append(row)

    strategy_id = request["strategy_id"]
    strategy_version = request["strategy_rule_version"]
    if any(
        type(value) is not str or not 1 <= len(value) <= 200
        for value in (strategy_id, strategy_version)
    ):
        raise ValueError("invalid_strategy_reference")
    version = {
        "id": uuid4(),
        "head_id": head_id,
        "seq": seq + 1,
        "kind": kind,
        "status": "PUBLISHED",
        "idempotency_key": key,
        "request_sha256": request_hash,
        "input_bundle_id": bundle_id,
        "input_bundle_sha256": bundle_hash,
        "strategy_id": strategy_id,
        "strategy_rule_version": strategy_version,
        "policy_version": policy.version,
        "policy_content_hash": policy_hash,
        "metadata": {
            "asof": asof.isoformat(),
            "trigger_ref": request.get("trigger_ref"),
        },
    }
    await db.execute(versions.insert().values(**version))
    for row in prepared:
        await _insert_row(db, version, row)
    return await _write_result(db, version)


def _session_close(
    day: date, venue: str, session: str, regular_close: datetime
) -> datetime:
    if venue == "NXT" and session in ("premarket", "aftermarket"):
        close = time(8, 50) if session == "premarket" else time(20)
        return datetime.combine(day, close, _KST)
    return regular_close


async def _insert_row(db: AsyncSession, version: dict, row: dict) -> None:
    adoption_key = content_hash = forecast_id = None
    if row.get("disposition") == "adopted":
        if (
            row["row_kind"] != "candidate"
            or row["action"] != "new"
            or version["input_bundle_sha256"] is None
        ):
            raise ValueError("invalid_adoption")
        adoption_key = _digest(
            [
                str(version["head_id"]),
                row_key(row),
                version["input_bundle_sha256"],
                "forecast_save",
            ]
        )
        content_hash = _digest(row)
        existing = (
            (await db.execute(select(rows).where(rows.c.adoption_key == adoption_key)))
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["adoption_content_sha256"] != content_hash:
                raise ValueError("adoption_conflict")
            return
        forecast_id = uuid5(_PLAN_NAMESPACE, adoption_key)
    await db.execute(
        rows.insert().values(
            id=uuid4(),
            version_id=version["id"],
            **{
                key: row.get(key)
                for key in (
                    "row_kind",
                    "market",
                    "symbol",
                    "account_ref",
                    "action",
                    "action_basis",
                )
            },
            adoption_key=adoption_key,
            adoption_content_sha256=content_hash,
            forecast_id=forecast_id,
            body=row,
        )
    )


async def get_plan(
    db: AsyncSession, *, day: date, seq: int | None, now: datetime, max_carries: int
) -> dict | None:
    stmt = (
        select(versions)
        .join(heads, heads.c.id == versions.c.head_id)
        .where(heads.c.session_date == day)
        .order_by(versions.c.seq.desc())
        .limit(1)
    )
    if seq is not None:
        stmt = stmt.where(versions.c.seq == seq)
    version = (await db.execute(stmt)).mappings().one_or_none()
    if version is None:
        return None
    views = []
    for row in await _fold(db, version["head_id"], version["seq"]):
        view = _row_view(row)
        if row["action"] == "watch":
            view["watch_state"] = asdict(
                watch_state(
                    asof=now,
                    valid_until=parse_time(view["valid_until"]),
                    carry_allowed=view["carry_allowed"],
                    carry_count=view["carry_count"],
                    carry_max_sessions=max_carries,
                    next_session=next_trading_session("kr", day),
                )
            )
        if row["forecast_id"]:
            state = await db.scalar(
                select(links.c.state)
                .where(links.c.row_id == row["id"])
                .order_by(links.c.created_at.desc(), links.c.id.desc())
                .limit(1)
            )
            view["forecast_link_state"] = state or "pending"
        views.append(view)
    incomplete = [
        view["id"]
        for view in views
        if view.get("forecast_link_state", "confirmed") != "confirmed"
    ]
    return {
        **dict(version),
        "rows": views,
        "link_complete": not incomplete,
        "unresolved_rows": incomplete,
    }


async def list_plans(
    db: AsyncSession,
    *,
    now: datetime,
    max_carries: int,
    queue_for: date | None,
) -> dict:
    latest = (
        select(versions.c.head_id, func.max(versions.c.seq).label("seq"))
        .group_by(versions.c.head_id)
        .subquery()
    )
    statement = (
        select(heads.c.session_date, latest.c.head_id, latest.c.seq)
        .join(latest, heads.c.id == latest.c.head_id)
        .order_by(heads.c.session_date.desc())
        .limit(60)
    )
    result = await db.execute(statement)
    plans = [dict(row) for row in result.mappings()]
    queue = []
    if queue_for is not None:
        previous = previous_trading_session("kr", queue_for)
        plan = (
            await get_plan(db, day=previous, seq=None, now=now, max_carries=max_carries)
            if previous
            else None
        )
        if plan:
            queue = [
                row
                for row in plan["rows"]
                if row.get("watch_state", {}).get("next_session") == queue_for
            ]
    return {"plans": plans, "premarket_reevaluation_queue": queue}


async def confirm_links(
    db: AsyncSession, *, day: date, seq: int, now: datetime
) -> dict:
    """Attach evidence already committed by forecast_save to adopted rows."""
    await _lock_head(db, day)
    version_id = await db.scalar(
        select(versions.c.id)
        .join(heads, heads.c.id == versions.c.head_id)
        .where(heads.c.session_date == day, versions.c.seq == seq)
    )
    if version_id is None:
        raise ValueError("version_not_found")
    results = []
    for row in await _version_rows(db, version_id):
        if row["forecast_id"] is None:
            continue
        state, decision_id = await _link_state(db, row)
        next_day = next_trading_session("kr", day)
        if state == "pending" and next_day and now.astimezone(_KST).date() >= next_day:
            state = "link_unresolved"
        if state != "pending":
            await db.execute(
                insert(links)
                .values(
                    id=uuid4(),
                    row_id=row["id"],
                    state=state,
                    decision_id=decision_id,
                )
                .on_conflict_do_nothing(index_elements=[links.c.row_id, links.c.state])
            )
        results.append(
            {
                "row_id": row["id"],
                "forecast_id": row["forecast_id"],
                "state": state,
                "decision_id": decision_id,
            }
        )
    return {
        "links": results,
        "link_complete": all(item["state"] == "confirmed" for item in results),
    }


async def _link_state(db: AsyncSession, row: dict) -> tuple[str, UUID | None]:
    forecast = (
        (
            await db.execute(
                text("""
        SELECT symbol, instrument_type::text AS market, correlation_id, decision_bucket
          FROM review.trade_forecasts WHERE forecast_id = :forecast_id
    """),
                {"forecast_id": row["forecast_id"]},
            )
        )
        .mappings()
        .one_or_none()
    )
    if forecast is None:
        return "pending", None
    decisions = (
        (
            await db.execute(
                text("""
        SELECT decision_id, symbol, market, side, account_mode
          FROM review.decision_evidence
         WHERE decision_kind = 'declined' AND CAST(:forecast_id AS uuid) = ANY(forecast_ids)
         LIMIT 2
    """),
                {"forecast_id": row["forecast_id"]},
            )
        )
        .mappings()
        .all()
    )
    if not decisions:
        return "link_no_decision", None
    if len(decisions) != 1:
        return "link_ambiguous", None
    decision = decisions[0]
    rungs = (
        (
            await db.execute(
                text("""
        SELECT side, planned_quantity, planned_notional, limit_price
          FROM review.decision_evidence_rungs WHERE decision_id = :decision_id LIMIT 2
    """),
                {"decision_id": decision["decision_id"]},
            )
        )
        .mappings()
        .all()
    )
    body = row["body"]
    from app.services.daily_plan.validator import parse_positive_decimal

    if (
        forecast["correlation_id"] != row["adoption_key"]
        or forecast["decision_bucket"] != "deferred_no_action"
        or forecast["market"] != "equity_kr"
        or forecast["symbol"] != row["symbol"]
        or decision["market"] != "equity_kr"
        or decision["symbol"] != row["symbol"]
        or decision["side"] != "buy"
        or decision["account_mode"] != body.get("would_be_account_mode")
        or len(rungs) != 1
        or rungs[0]["side"] != "buy"
    ):
        return "link_mismatch", None
    for field, evidence_field in (
        ("would_be_limit_price", "limit_price"),
        ("would_be_quantity", "planned_quantity"),
        ("would_be_notional", "planned_notional"),
    ):
        value = body.get(field)
        expected = parse_positive_decimal(value) if value else None
        if rungs[0][evidence_field] != expected:
            return "link_mismatch", None
    return "confirmed", decision["decision_id"]
