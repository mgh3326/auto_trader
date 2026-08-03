"""Normalised 1-minute bar fetchers for the three backfill sources.

Every source returns the same shape so the equality gate and the collector can
treat them interchangeably:

    {minute_kst_naive: {"open","high","low","close","volume","value"}}

`value` is **not** uniformly meaningful across sources — see VALUE_SEMANTICS.
Callers must not compare it blindly.

The 09:00-20:00 KST fetch freeze is enforced *here*, in code, rather than left
to operator discipline: `assert_fetch_window_open()` is called by every fetcher.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

KST = timezone(timedelta(hours=9))

#: Regular KRX session. NXT bars are discarded in Phase 1 by instruction.
SESSION_OPEN = dtime(9, 0)
SESSION_CLOSE = dtime(15, 30)

#: Fetching is forbidden while the regular session or NXT is open.
FREEZE_START = dtime(9, 0)
FREEZE_END = dtime(20, 0)

#: What each source's `value` field actually is. Discovered during Stage A prep.
VALUE_SEMANTICS: dict[str, str] = {
    # broker-reported traded value (거래대금)
    "kiwoom": "broker_reported_trde_prica",
    "kis": "broker_reported_acml_tr_pbmn",
    # NOT broker-reported: app/services/brokers/toss/candles.py computes
    # value = close * volume. Comparing it against the others measures the
    # repo's own arithmetic, not source agreement.
    "toss": "synthesised_close_times_volume",
}

PACE_SECONDS: dict[str, float] = {"toss": 0.3, "kiwoom": 2.0, "kis": 0.5}


class FetchWindowClosed(RuntimeError):
    """Raised when a fetch is attempted inside the 09:00-20:00 KST freeze."""


def now_kst() -> datetime:
    return datetime.now(KST)


def assert_fetch_window_open(*, override_now: datetime | None = None) -> None:
    n = override_now or now_kst()
    if FREEZE_START <= n.time() < FREEZE_END:
        raise FetchWindowClosed(
            f"fetch frozen 09:00-20:00 KST (regular session + NXT); now {n:%H:%M:%S} KST"
        )


class Pacer:
    """Serial per-source pacer. One instance per source stream."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.interval = PACE_SECONDS[source]
        self.calls = 0
        self._last = 0.0

    async def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.interval:
            await asyncio.sleep(self.interval - delta)
        self._last = time.monotonic()
        self.calls += 1


def _to_float(raw: Any) -> float:
    # Kiwoom prefixes price/volume with a direction sign ("-78800"); the
    # magnitude is the value, the sign is 전일대비 direction.
    return abs(float(str(raw).strip().replace(",", "") or 0))


def in_regular_session(ts: datetime) -> bool:
    """Regular-session bars only; 15:30 closing bar included, NXT discarded."""
    return SESSION_OPEN <= ts.time() <= SESSION_CLOSE


# --------------------------------------------------------------------------
# Kiwoom (mock host) — ka10080, tic_scope=1
# --------------------------------------------------------------------------


async def fetch_kiwoom_minutes(
    *,
    client: Any,
    symbol: str,
    pacer: Pacer,
    max_pages: int = 1,
    base_dt: str | None = None,
) -> tuple[dict[datetime, dict[str, float]], dict[str, Any]]:
    from app.services.brokers.kiwoom.live_market_data import CHART_PATH

    assert_fetch_window_open()
    out: dict[datetime, dict[str, float]] = {}
    meta: dict[str, Any] = {"pages": 0, "rows_raw": 0, "next_key_seen": False}

    cont_yn: str | None = None
    next_key: str | None = None
    for _ in range(max_pages):
        body: dict[str, Any] = {
            "stk_cd": symbol,
            "tic_scope": "1",
            "upd_stkpc_tp": "1",
        }
        if base_dt:
            body["base_dt"] = base_dt
        await pacer.wait()
        payload = await client.post_api(
            api_id="ka10080",
            path=CHART_PATH,
            body=body,
            **({"cont_yn": cont_yn, "next_key": next_key} if cont_yn else {}),
        )
        rows = payload.get("stk_min_pole_chart_qry") or []
        meta["pages"] += 1
        meta["rows_raw"] += len(rows)
        if not rows:
            break
        for r in rows:
            raw_ts = str(r.get("cntr_tm", "")).strip()
            if len(raw_ts) < 12:
                continue
            ts = datetime.strptime(raw_ts[:12], "%Y%m%d%H%M")
            out[ts] = {
                "open": _to_float(r.get("open_pric")),
                "high": _to_float(r.get("high_pric")),
                "low": _to_float(r.get("low_pric")),
                "close": _to_float(r.get("cur_prc")),
                "volume": _to_float(r.get("trde_qty")),
                "value": _to_float(r.get("trde_prica")),
            }
        # post_api merges the cont-yn / next-key response headers into payload.
        more = str(payload.get("cont_yn", "")).strip().upper() == "Y"
        nk = str(payload.get("next_key", "")).strip() or None
        if not (more and nk):
            break
        meta["next_key_seen"] = True
        cont_yn, next_key = "Y", nk

    return out, meta


# --------------------------------------------------------------------------
# Toss (live read host) — /api/v1/candles interval=1m
# --------------------------------------------------------------------------


async def fetch_toss_minutes(
    *,
    client: Any,
    symbol: str,
    pacer: Pacer,
    count: int = 400,
    before: str | None = None,
    max_pages: int = 3,
) -> tuple[dict[datetime, dict[str, float]], dict[str, Any]]:
    assert_fetch_window_open()
    out: dict[datetime, dict[str, float]] = {}
    meta: dict[str, Any] = {"pages": 0, "rows_raw": 0, "next_before": None}

    cursor = before
    for _ in range(max_pages):
        await pacer.wait()
        page = await client.candles(
            symbol, interval="1m", count=min(count, 200), before=cursor
        )
        meta["pages"] += 1
        meta["rows_raw"] += len(page.candles)
        if not page.candles:
            break
        for c in page.candles:
            ts = datetime.fromisoformat(str(c.timestamp).replace("Z", "+00:00"))
            ts = ts.astimezone(KST).replace(tzinfo=None) if ts.tzinfo else ts
            close = float(c.close_price)
            volume = float(c.volume)
            out[ts.replace(second=0, microsecond=0)] = {
                "open": float(c.open_price),
                "high": float(c.high_price),
                "low": float(c.low_price),
                "close": close,
                # synthesised, not broker-reported — see VALUE_SEMANTICS
                "value": close * volume,
                "volume": volume,
            }
        cursor = page.next_before
        meta["next_before"] = cursor
        if not cursor or len(out) >= count:
            break

    return out, meta


# --------------------------------------------------------------------------
# KIS (mock host) — FHKST03010230
# --------------------------------------------------------------------------


async def fetch_kis_minutes(
    *,
    client: Any,
    symbol: str,
    pacer: Pacer,
    session_date: date,
    end_time: str = "153000",
    max_pages: int = 4,
) -> tuple[dict[datetime, dict[str, float]], dict[str, Any]]:
    assert_fetch_window_open()
    out: dict[datetime, dict[str, float]] = {}
    meta: dict[str, Any] = {"pages": 0, "rows_raw": 0}

    cursor = end_time
    for _ in range(max_pages):
        await pacer.wait()
        frame = await client.inquire_time_dailychartprice(
            symbol, n=120, end_date=session_date, end_time=cursor
        )
        meta["pages"] += 1
        meta["rows_raw"] += len(frame)
        if frame.empty:
            break
        for _idx, r in frame.iterrows():
            ts = r["datetime"]
            ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if ts.tzinfo is not None:
                ts = ts.astimezone(KST).replace(tzinfo=None)
            out[ts.replace(second=0, microsecond=0)] = {
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
                "value": float(r["value"]),
            }
        earliest = min(out)
        if earliest.time() <= SESSION_OPEN:
            break
        cursor = (earliest - timedelta(minutes=1)).strftime("%H%M%S")

    return out, meta
