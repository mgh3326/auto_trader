"""Scheduleless monthly promotion: production 1m bars -> research deep history.

Contract (herdr-inbox/answer-codexmock-research-db-1805.md, R-2):

* reads production ``public.kr_candles_1m``; **never writes to it**
* promotes **completed sessions only** — an in-progress session is not final
* identical ``(time_utc, symbol, venue)`` with identical OHLCV/value is a no-op
* a disagreement is **never overwritten**; it is quarantined in
  ``research.kr_candle_promotion_conflicts`` so snapshot sealing is blocked
* promotion lag > 60 days fails closed and reports that provider backfill is
  needed. A range whose production origin already aged past the 90-day
  retention window is never reported as a success.

There is deliberately no dual-write and no scheduler: a research-DB failure must
not be able to disturb live ingestion, and a best-effort write must not be able
to leave a silent research gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

#: Beyond this, production rows are close enough to the 90-day retention edge
#: that a silent partial promotion becomes possible. Fail closed instead.
MAX_PROMOTION_LAG_DAYS = 60

#: Production retention. Anything older than this is already gone upstream.
PRODUCTION_RETENTION_DAYS = 90

VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "value")

SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(15, 30)


class PromotionBlocked(RuntimeError):
    """Raised when promotion must not proceed. Never downgraded to a warning."""


@dataclass
class PromotionResult:
    source: str
    venue: str
    dry_run: bool
    from_session: date | None = None
    to_session: date | None = None
    rows_read: int = 0
    rows_inserted: int = 0
    rows_noop_identical: int = 0
    conflicts_quarantined: int = 0
    lag_days: int | None = None
    watermark_advanced_to: date | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        for k in ("from_session", "to_session", "watermark_advanced_to"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


def classify_session_segment(ts_kst: datetime, venue: str) -> str:
    """Fail closed: only a bar we can actually place gets a concrete segment.

    KRX bars inside the regular session are KRX_REGULAR. NTX bars map to the
    pre/overlap/post windows around it. Anything else — including a KRX-labelled
    bar outside regular hours — is UNKNOWN rather than a guess.
    """
    t = ts_kst.time()
    if venue == "KRX":
        return "KRX_REGULAR" if SESSION_OPEN <= t <= SESSION_CLOSE else "UNKNOWN"
    if venue == "NTX":
        if t < SESSION_OPEN:
            return "NXT_PRE"
        if t <= SESSION_CLOSE:
            return "NXT_OVERLAP"
        return "NXT_POST"
    return "UNKNOWN"


def last_completed_session(now_kst: datetime) -> date:
    """Today counts only once its regular session has closed."""
    if now_kst.time() > SESSION_CLOSE:
        return now_kst.date()
    return now_kst.date() - timedelta(days=1)


async def _fetch_watermark(conn: Any, source: str, venue: str) -> date | None:
    return await conn.fetchval(
        "SELECT last_promoted_session_date_kst "
        "FROM research.kr_candle_promotion_watermark "
        "WHERE source = $1 AND venue = $2",
        source,
        venue,
    )


async def promote(
    conn: Any,
    *,
    source: str,
    venue: str,
    now_kst: datetime | None = None,
    dry_run: bool = True,
    batch_id: str,
    max_sessions: int | None = None,
) -> PromotionResult:
    """Promote completed production sessions into research. Read-only on public."""
    now = now_kst or datetime.now(KST)
    result = PromotionResult(source=source, venue=venue, dry_run=dry_run)

    watermark = await _fetch_watermark(conn, source, venue)
    to_session = last_completed_session(now)

    if watermark is None:
        earliest = await conn.fetchval(
            # NB: production's column is `time`; only research uses `time_utc`.
            "SELECT min(time AT TIME ZONE 'Asia/Seoul')::date "
            "FROM public.kr_candles_1m WHERE venue = $1",
            venue,
        )
        if earliest is None:
            result.notes.append(
                "production has no rows for this venue; nothing to promote"
            )
            return result
        from_session = earliest
        result.notes.append(
            f"no watermark; starting from earliest production session {earliest}"
        )
    else:
        from_session = watermark + timedelta(days=1)

    result.from_session = from_session
    result.to_session = to_session
    result.lag_days = (to_session - from_session).days

    if from_session > to_session:
        result.notes.append("already up to date")
        return result

    # --- fail-closed gates ------------------------------------------
    if result.lag_days > MAX_PROMOTION_LAG_DAYS:
        raise PromotionBlocked(
            f"promotion lag {result.lag_days}d exceeds {MAX_PROMOTION_LAG_DAYS}d for "
            f"{source}/{venue} ({from_session}..{to_session}). Provider backfill is "
            f"required; promotion cannot cover this range from production alone."
        )

    retention_floor = now.date() - timedelta(days=PRODUCTION_RETENTION_DAYS)
    if from_session < retention_floor:
        raise PromotionBlocked(
            f"requested range starts {from_session}, older than the production "
            f"{PRODUCTION_RETENTION_DAYS}d retention floor {retention_floor}. Those rows are "
            f"already dropped upstream; promoting would silently produce a partial range."
        )

    if max_sessions is not None:
        capped = from_session + timedelta(days=max_sessions - 1)
        if capped < to_session:
            to_session = capped
            result.to_session = to_session
            result.notes.append(f"range capped to {max_sessions} sessions by caller")

    start_utc = datetime.combine(from_session, time.min).replace(tzinfo=KST)
    end_utc = datetime.combine(to_session + timedelta(days=1), time.min).replace(
        tzinfo=KST
    )

    rows = await conn.fetch(
        """
        SELECT time, symbol, venue, open, high, low, close, volume, value
        FROM public.kr_candles_1m
        WHERE venue = $1 AND time >= $2 AND time < $3
        ORDER BY time
        """,
        venue,
        start_utc,
        end_utc,
    )
    result.rows_read = len(rows)
    if not rows:
        result.notes.append("no production rows in range")
        if not dry_run:
            await _advance_watermark(conn, source, venue, to_session, 0)
            result.watermark_advanced_to = to_session
        return result

    for r in rows:
        ts_kst = r["time"].astimezone(KST)
        segment = classify_session_segment(ts_kst, r["venue"])
        incoming = {c: r[c] for c in VALUE_COLUMNS}

        existing = await conn.fetchrow(
            "SELECT source, open, high, low, close, volume, value "
            "FROM research.kr_candles_1m "
            "WHERE time_utc = $1 AND symbol = $2 AND venue = $3",
            r["time"],
            r["symbol"],
            r["venue"],
        )

        if existing is not None:
            if all(existing[c] == incoming[c] for c in VALUE_COLUMNS):
                result.rows_noop_identical += 1
                continue
            result.conflicts_quarantined += 1
            if not dry_run:
                await conn.execute(
                    """
                    INSERT INTO research.kr_candle_promotion_conflicts
                        (time_utc, symbol, venue, existing_source, incoming_source,
                         existing_values, incoming_values, batch_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    """,
                    r["time"],
                    r["symbol"],
                    r["venue"],
                    existing["source"],
                    source,
                    _jsonify({c: existing[c] for c in VALUE_COLUMNS}),
                    _jsonify(incoming),
                    batch_id,
                )
            continue

        result.rows_inserted += 1
        if not dry_run:
            await conn.execute(
                """
                INSERT INTO research.kr_candles_1m
                    (time_utc, session_date_kst, symbol, venue, session_segment, source,
                     open, high, low, close, volume, value, retrieved_at, batch_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,now(),$13)
                ON CONFLICT (time_utc, symbol, venue) DO NOTHING
                """,
                r["time"],
                ts_kst.date(),
                r["symbol"],
                r["venue"],
                segment,
                source,
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                r["volume"],
                r["value"],
                batch_id,
            )

    if not dry_run:
        await _advance_watermark(conn, source, venue, to_session, result.rows_inserted)
        result.watermark_advanced_to = to_session

    if result.conflicts_quarantined:
        result.notes.append(
            f"{result.conflicts_quarantined} conflicts quarantined; snapshot sealing is "
            f"blocked until they are reviewed. No existing row was overwritten."
        )
    return result


def _jsonify(d: dict[str, Any]) -> str:
    import json

    return json.dumps({k: (float(v) if v is not None else None) for k, v in d.items()})


async def _advance_watermark(
    conn: Any, source: str, venue: str, to_session: date, rows: int
) -> None:
    await conn.execute(
        """
        INSERT INTO research.kr_candle_promotion_watermark
            (source, venue, last_promoted_session_date_kst, last_promoted_time_utc,
             rows_promoted_total, updated_at)
        VALUES ($1, $2, $3, now(), $4, now())
        ON CONFLICT (source, venue) DO UPDATE SET
            last_promoted_session_date_kst = EXCLUDED.last_promoted_session_date_kst,
            last_promoted_time_utc = EXCLUDED.last_promoted_time_utc,
            rows_promoted_total =
                research.kr_candle_promotion_watermark.rows_promoted_total + EXCLUDED.rows_promoted_total,
            updated_at = now()
        """,
        source,
        venue,
        to_session,
        rows,
    )
