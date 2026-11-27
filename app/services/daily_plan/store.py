"""Sole writer and published-state reader for daily plans (KR, US, crypto)."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import Text, func, literal, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.daily_plan import bundles, heads, links, rows, versions
from app.schemas.trading_policy import TradingPolicyDocument
from app.services.daily_plan.allocation import build_comparison
from app.services.daily_plan.bundle import (
    bundle_sha256,
    canonical_bundle,
    canonical_json,
)
from app.services.daily_plan.risk import replay_risk
from app.services.daily_plan.validator import (
    parse_positive_decimal,
    parse_time,
    prepare_row,
    require_base_version,
    row_key,
)
from app.services.daily_plan.vocabulary import (
    ALLOCATION_METADATA_KEY,
    MARKETS,
    PLAN_MODES,
    VERSION_KINDS,
)
from app.services.daily_plan.watch import watch_state
from app.services.market_events.session_calendar import (
    next_trading_session,
    previous_trading_session,
    regular_session_bounds,
)

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")
_ZONES = {"kr": _KST, "us": _ET, "crypto": UTC}
# Latest asof a premarket version may carry, in the market's own clock.
_CUTOFFS = {"kr": time(8, 30), "us": time(4), "crypto": time(0)}
_INSTRUMENTS = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


def validate_market(value: Any) -> str:
    if value not in MARKETS:
        raise ValueError("invalid_market")
    return value


def _cutoff(market: str, day: date) -> datetime:
    return datetime.combine(day, _CUTOFFS[market], _ZONES[market])


def _regular_bounds(market: str, day: date) -> tuple[datetime, datetime] | None:
    if market == "crypto":
        start = datetime.combine(day, time(0), UTC)
        return start, start + timedelta(days=1)
    return regular_session_bounds(market, day)


def _previous_session(market: str, day: date) -> date | None:
    if market == "crypto":
        return day - timedelta(days=1)
    return previous_trading_session(market, day)


def _next_session(market: str, day: date) -> date | None:
    if market == "crypto":
        return day + timedelta(days=1)
    return next_trading_session(market, day)


_PLAN_NAMESPACE = UUID("ed870506-5d0b-5907-9c26-ab799da98b1b")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


async def _lock_head(db: AsyncSession, day: date) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(6751, day.toordinal())))


async def _head(
    db: AsyncSession, day: date, market: str = "kr", mode: str = "plan_only"
) -> UUID:
    where = (heads.c.market == market, heads.c.session_date == day)
    existing = await db.scalar(select(heads.c.id).where(*where, heads.c.mode == mode))
    if existing is not None:
        return existing
    # A head's mode is fixed when it is created; another mode is a conflict.
    if await db.scalar(select(heads.c.id).where(*where)) is not None:
        raise ValueError("plan_mode_conflict")
    head_id = uuid4()
    await db.execute(
        heads.insert().values(
            id=head_id,
            market=market,
            session_date=day,
            mode=mode,
            cutoff_at=_cutoff(market, day),
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
    if parse_time(normalized["bundle_closed_at"]).astimezone(_KST).date() != day:
        raise ValueError("invalid_bundle_time")
    keys = [row_key(row) for row in normalized["rows"]]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate_bundle_row_key")
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
    metadata = versions.c.metadata.op("-", return_type=JSONB)(
        literal(ALLOCATION_METADATA_KEY, Text)
    ).label("metadata")
    result = await db.execute(
        select(rows, metadata)
        .select_from(versions.outerjoin(rows, rows.c.version_id == versions.c.id))
        .where(versions.c.head_id == head_id, versions.c.seq <= seq)
        .order_by(versions.c.seq, rows.c.id)
    )
    folded = {}
    by_id = {}
    for record in result.mappings():
        row = dict(record)
        replay_ids = row.pop("metadata").get("adoption_replay_rows", [])
        if row["id"] is not None:
            by_id[str(row["id"])] = row
            folded[row_key(row)] = row
        for replay_id in replay_ids:
            replay = by_id[replay_id]
            folded[row_key(replay)] = replay
    return list(folded.values())


def _session_view(version: dict) -> dict:
    """A version as sessions may read it.

    The ranking comparison is operator-only: ranking A is close to control C,
    and a session that sees it would anchor on it (design §4.2).
    """
    metadata = {
        key: value
        for key, value in version["metadata"].items()
        if key != ALLOCATION_METADATA_KEY
    }
    return {**version, "metadata": metadata}


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


async def _publication_rows(db: AsyncSession, version: dict) -> list[dict]:
    result = await _version_rows(db, version["id"])
    replay_ids = version["metadata"].get("adoption_replay_rows", [])
    if replay_ids:
        replay = await db.execute(
            select(rows).where(rows.c.id.in_([UUID(item) for item in replay_ids]))
        )
        result.extend(dict(row) for row in replay.mappings())
    return result


async def _write_result(db: AsyncSession, version: dict) -> dict:
    return {
        **_session_view(version),
        "rows": [_row_view(row) for row in await _publication_rows(db, version)],
    }


async def write_version(
    db: AsyncSession,
    *,
    day: date,
    request: dict[str, Any],
    now: datetime,
    policy: TradingPolicyDocument,
    policy_hash: str,
) -> dict:
    """Validate and insert one publication in the caller's transaction.

    ``market`` (default kr) and ``mode`` (default plan_only) are explicit
    request fields. An ``executable`` row is stored as such but nothing reads
    it yet: the intent issuer is a later ticket (hk 6939 N2).
    """
    market = validate_market(request.get("market", "kr"))
    mode = request.get("mode", "plan_only")
    if mode not in PLAN_MODES:
        raise ValueError("invalid_plan_mode")
    zone = _ZONES[market]
    await _lock_head(db, day)
    head_id = await _head(db, day, market, mode)
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
    regular = _regular_bounds(market, day)
    if regular is None or now.utcoffset() is None or now.astimezone(zone).date() != day:
        raise ValueError("calendar_unknown")
    cutoff = _cutoff(market, day)
    asof = parse_time(request["asof"])
    if (
        asof > now
        or asof.astimezone(zone).date() != day
        or (kind == "premarket" and asof > cutoff)
    ):
        raise ValueError("invalid_version_time")

    bundle_id = (
        UUID(request["input_bundle_id"]) if request.get("input_bundle_id") else None
    )
    bundle_hash = request.get("input_bundle_sha256")
    if (bundle_id is None) != (bundle_hash is None):
        raise ValueError("invalid_bundle_reference")
    if bundle_id and market != "kr":
        # The server-side bundle assembler is KR only (hk 6902).
        raise ValueError("bundle_market_unsupported")
    bundle_closed_at = asof
    bundle_body = None
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
            raise ValueError("bundle_not_found")
        if (
            bundle["sha256"] != bundle_hash
            or bundle_sha256(bundle["body"]) != bundle_hash
        ):
            raise ValueError("bundle_hash_mismatch")
        bundle_closed_at = parse_time(bundle["body"]["bundle_closed_at"])
        if bundle_closed_at > asof or bundle_closed_at > bundle["created_at"]:
            raise ValueError("invalid_bundle_time")
        bundle_body = bundle["body"]
        evidence = {row_key(row): row for row in bundle_body["rows"]}

    previous = _previous_session(market, day)
    previous_bounds = _regular_bounds(market, previous) if previous else None
    if previous_bounds is None:
        raise ValueError("calendar_unknown")
    proposed = request["rows"]
    if not proposed or len(proposed) > 1000:
        raise ValueError("invalid_row_count")
    previous_plan = None
    current_rows = None
    prepared = []
    seen = set()
    for raw in proposed:
        if raw.get("market") != market:
            raise ValueError("market_mismatch")
        key_for_row = row_key({**raw, "symbol": raw["symbol"].strip().upper()})
        close = _session_close(market, day, raw["venue"], raw["session"], regular)
        row_evidence = evidence.get(key_for_row)
        row = prepare_row(
            raw,
            evidence=row_evidence,
            asof=now,
            session_close=close,
            bundle_closed_at=bundle_closed_at,
            oldest_accepted_at=previous_bounds[1],
            no_add_floor_pct=Decimal(str(policy.daily_plan.no_add_floor_pct)),
        )
        if row["row_kind"] == "holding" and row_evidence:
            verdict = replay_risk(
                row_evidence,
                policy.mechanical_loss_cut.markets["kr"],
                symbol=row["symbol"],
            )
            verdict.update(
                policy_version=policy.version, policy_content_hash=policy_hash
            )
            row["rule_verdict"] = verdict
            if (
                row["not_executable_reason"]
                in (None, "add_blocked_lot_at_or_below_floor")
                and row_evidence.get("account_scope") == "routable"
            ):
                if verdict["result"] == "cut":
                    if (
                        row["action"] != "risk_sell"
                        or row["action_basis"] != "rule"
                        or parse_positive_decimal(row["would_be_quantity"])
                        != Decimal(verdict["quantity"])
                    ):
                        raise ValueError("risk_rule_mismatch")
                elif row["action"] == "risk_sell":
                    raise ValueError("risk_rule_mismatch")
            if row["action"] == "risk_sell" and verdict["result"] != "cut":
                raise ValueError("risk_rule_mismatch")
        if row_key(row) in seen:
            raise ValueError("duplicate_row_key")
        seen.add(row_key(row))
        carry_from = row.get("carried_from_row_id")
        if carry_from is not None or (
            row["action"] == "watch" and row["carry_allowed"]
        ):
            if previous_plan is None:
                previous_plan = (
                    await get_plan(
                        db,
                        day=previous,
                        seq=None,
                        now=now,
                        max_carries=policy.daily_plan.carry_max_sessions,
                        market=market,
                    )
                    or {}
                )
            origin = next(
                (
                    item
                    for item in previous_plan.get("rows", [])
                    if row_key(item) == row_key(row)
                    and item.get("watch_state", {}).get("next_session") == day
                ),
                None,
            )
            if carry_from is not None and (
                origin is None
                or str(origin["id"]) != carry_from
                or row["action"] != "watch"
            ):
                raise ValueError("invalid_carry_origin")
            if origin is not None:
                row["carried_from_row_id"] = str(origin["id"])
                row["carry_count"] = origin["watch_state"]["carry_count"]
        if seq and row["action"] == "watch":
            if current_rows is None:
                current_rows = await _current_rows(db, head_id, seq)
            prior = current_rows.get(row_key(row), {})
            if prior.get("carry_count", 0) > row["carry_count"]:
                row["carry_count"] = prior["carry_count"]
                row["carried_from_row_id"] = prior["carried_from_row_id"]
        prepared.append(row)

    strategy_id = request["strategy_id"]
    strategy_version = request["strategy_rule_version"]
    if any(
        type(value) is not str or not 1 <= len(value) <= 200
        for value in (strategy_id, strategy_version)
    ):
        raise ValueError("invalid_strategy_reference")
    # Budgets compete across the whole plan, so compare every candidate in
    # force after this publication, not only the rows it changes.
    if seq and current_rows is None:
        current_rows = await _current_rows(db, head_id, seq)
    candidates = {
        key[2]: body
        for key, body in (current_rows or {}).items()
        if key[0] == "candidate"
    }
    candidates.update(
        {row["symbol"]: row for row in prepared if row["row_kind"] == "candidate"}
    )
    comparison = build_comparison(
        bundle=bundle_body,
        bundle_sha256=bundle_hash,
        candidates=candidates,
        account_facts=(bundle_body or {}).get("accounts"),
        max_adopt=policy.daily_plan.max_adopt,
        cutoff_at=bundle_closed_at,
        stale_before=previous_bounds[1],
        evaluated_at=now,
    )
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
            ALLOCATION_METADATA_KEY: comparison,
        },
    }
    inserts = []
    replay_ids = []
    for row in prepared:
        item = await _prepare_insert(db, version, row, mode)
        if isinstance(item, UUID):
            replay_ids.append(str(item))
        else:
            inserts.append(item)
    version["metadata"]["adoption_replay_rows"] = replay_ids
    await db.execute(versions.insert().values(**version))
    for item in inserts:
        await db.execute(rows.insert().values(**item))
    return await _write_result(db, version)


async def _current_rows(db: AsyncSession, head_id: UUID, seq: int) -> dict:
    return {row_key(item): item["body"] for item in await _fold(db, head_id, seq)}


def _session_close(
    market: str,
    day: date,
    venue: str,
    session: str,
    regular: tuple[datetime, datetime],
) -> datetime:
    regular_close = regular[1]
    if market == "crypto":
        return regular_close
    if market == "us":
        if session == "us_pre":
            return regular[0]
        if session == "us_post":
            # Extended hours end four hours after the (possibly early) close.
            return regular_close + timedelta(hours=4)
        return regular_close
    if venue == "NXT" and session == "premarket":
        return datetime.combine(day, time(8, 50), _KST)
    if session == "aftermarket":
        return datetime.combine(day, time(20), _KST)
    return regular_close


async def _prepare_insert(
    db: AsyncSession, version: dict, row: dict, mode: str = "plan_only"
) -> dict | UUID:
    adoption_key = content_hash = forecast_id = None
    if row.get("disposition") == "adopted":
        if (
            row["row_kind"] != "candidate"
            or row["action"] not in ("new", "add")
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
            return existing["id"]
        forecast_id = uuid5(_PLAN_NAMESPACE, adoption_key)
    return {
        "id": uuid4(),
        "version_id": version["id"],
        "execution_state": mode,
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
        "adoption_key": adoption_key,
        "adoption_content_sha256": content_hash,
        "forecast_id": forecast_id,
        "body": row,
    }


async def get_plan(
    db: AsyncSession,
    *,
    day: date,
    seq: int | None,
    now: datetime,
    max_carries: int,
    market: str = "kr",
) -> dict | None:
    market = validate_market(market)
    stmt = (
        select(versions)
        .join(heads, heads.c.id == versions.c.head_id)
        .where(heads.c.market == market, heads.c.session_date == day)
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
                    next_session=_next_session(market, day),
                    session_date=now.astimezone(_ZONES[market]).date(),
                )
            )
        if row["forecast_id"]:
            state = await db.scalar(
                select(links.c.state)
                .where(links.c.row_id == row["id"])
                .order_by(links.c.seq.desc())
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
        **_session_view(dict(version)),
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
    market: str = "kr",
) -> dict:
    market = validate_market(market)
    latest = (
        select(versions.c.head_id, func.max(versions.c.seq).label("seq"))
        .group_by(versions.c.head_id)
        .subquery()
    )
    statement = (
        select(heads.c.session_date, latest.c.head_id, latest.c.seq)
        .join(latest, heads.c.id == latest.c.head_id)
        .where(heads.c.market == market)
        .order_by(heads.c.session_date.desc())
        .limit(60)
    )
    result = await db.execute(statement)
    plans = [dict(row) for row in result.mappings()]
    queue = []
    if queue_for is not None:
        previous = _previous_session(market, queue_for)
        plan = (
            await get_plan(
                db,
                day=previous,
                seq=None,
                now=now,
                max_carries=max_carries,
                market=market,
            )
            if previous
            else None
        )
        if plan:
            current = await get_plan(
                db,
                day=queue_for,
                seq=None,
                now=now,
                max_carries=max_carries,
                market=market,
            )
            decided = {row_key(row) for row in current["rows"]} if current else set()
            queue = [
                row
                for row in plan["rows"]
                if row.get("watch_state", {}).get("next_session") == queue_for
                and row_key(row) not in decided
            ]
    return {"plans": plans, "premarket_reevaluation_queue": queue}


async def confirm_links(
    db: AsyncSession, *, day: date, seq: int, now: datetime, market: str = "kr"
) -> dict:
    """Attach evidence already committed by forecast_save to adopted rows."""
    market = validate_market(market)
    await _lock_head(db, day)
    version = (
        (
            await db.execute(
                select(versions)
                .join(heads, heads.c.id == versions.c.head_id)
                .where(
                    heads.c.market == market,
                    heads.c.session_date == day,
                    versions.c.seq == seq,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if version is None:
        raise ValueError("version_not_found")
    candidates = await _publication_rows(db, dict(version))
    results = []
    next_day = _next_session(market, day)
    for row in candidates:
        if row["forecast_id"] is None:
            continue
        state, decision_id = await _link_state(db, row)
        if (
            state == "pending"
            and next_day
            and now.astimezone(_ZONES[market]).date() >= next_day
        ):
            state = "link_unresolved"
        previous = (
            (
                await db.execute(
                    select(links)
                    .where(links.c.row_id == row["id"])
                    .order_by(links.c.seq.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if state != "pending" and (
            previous is None
            or previous["state"] != state
            or previous["decision_id"] != decision_id
        ):
            await db.execute(
                links.insert().values(
                    id=uuid4(),
                    row_id=row["id"],
                    state=state,
                    decision_id=decision_id,
                    seq=1 if previous is None else previous["seq"] + 1,
                )
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
    instrument = _INSTRUMENTS[row.get("market", "kr")]
    if (
        forecast["correlation_id"] != row["adoption_key"]
        or forecast["decision_bucket"] != "deferred_no_action"
        or forecast["market"] != instrument
        or forecast["symbol"] != row["symbol"]
        or decision["market"] != instrument
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
