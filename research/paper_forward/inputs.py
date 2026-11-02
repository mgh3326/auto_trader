"""Explicit intent feed and public market data; never load app settings or credentials."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import exchange_calendars as xcals

from app.core.symbol import to_db_symbol
from research.paper_forward.engine import Bar, Signal
from research.trade_retro.canonical import is_parking
from research.trade_retro.prices import _fetch_upbit, _fetch_yahoo, _yahoo_candidates


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive(value) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("prices and notionals must be decimal strings")
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("prices and notionals must be finite and positive")
    return result


def calendar(market: str, start: date, end: date) -> list[date]:
    if market == "crypto":
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [
        s.date()
        for s in xcals.get_calendar(
            "XNYS",
            start=str(start - timedelta(days=7)),
            end=str(end + timedelta(days=7)),
        ).sessions_in_range(str(start), str(end))
    ]


def close_time(market: str, day: date) -> datetime:
    if market == "crypto":
        return datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
    return (
        xcals.get_calendar(
            "XNYS", start=str(day - timedelta(days=7)), end=str(day + timedelta(days=7))
        )
        .session_close(str(day))
        .to_pydatetime()
    )


def last_completed(market: str, now: datetime) -> date:
    days = calendar(market, now.date() - timedelta(days=14), now.date())
    return max(day for day in days if close_time(market, day) <= now)


def universe(path: Path, market: str) -> list[str]:
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != "trade-retro-prices/v1":
        raise ValueError("universe must be the frozen research prices_manifest.json")
    keys = set(manifest["entries"]) | set(manifest["missing"])
    names = sorted(key.split("/", 1)[1] for key in keys if key.startswith(market + "/"))
    if not names or any(is_parking(market, name) for name in names):
        # Research snapshots contain parking too; it is excluded from the signal universe.
        names = [name for name in names if not is_parking(market, name)]
    if not names:
        raise ValueError("research universe is empty")
    for name in names:
        if not name.isascii() or not all(c.isalnum() or c == "." for c in name):
            raise ValueError("invalid research symbol")
        if market == "us" and to_db_symbol(name) != name:
            raise ValueError("research symbol must use DB dot format")
    return names


def signal_feed(
    path: Path | None,
    market: str,
    names: list[str],
    asof: date,
    required_from: date | None = None,
) -> tuple[list[Signal], str]:
    if path is None or not path.is_file():
        return [], "unverified:missing_signal_feed"
    feed = json.loads(path.read_text())
    if feed["schema"] != "paper-forward-signals/v1" or feed["market"] != market:
        raise ValueError("incorrect signal feed schema/market")
    through = datetime.fromisoformat(feed["complete_through"])
    if through.tzinfo is None:
        raise ValueError("signal feed needs an offset-aware coverage timestamp")
    reasons = []
    if through < close_time(market, asof):
        reasons.append("stale_signal_feed")
    covers_from = feed.get("covers_from")
    if covers_from is None:
        reasons.append("missing_signal_history")
    else:
        if not isinstance(covers_from, str):
            raise ValueError("covers_from must be an ISO date")
        covered_start = date.fromisoformat(covers_from)
        if str(covered_start) != covers_from:
            raise ValueError("covers_from must be an ISO date")
        if covered_start > (required_from or asof):
            reasons.append("incomplete_signal_history")
    state = "unverified:" + ";".join(reasons) if reasons else "verified"
    rows, keys = [], set()
    for row in feed["signals"]:
        at = datetime.fromisoformat(row["at"])
        if (
            at.tzinfo is None
            or not isinstance(row["key"], str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", row["key"])
            or row["key"] in keys
        ):
            raise ValueError(
                "signal timestamps/keys must be aware, nonempty and unique"
            )
        keys.add(row["key"])
        if row["symbol"] not in names:
            raise ValueError("signal is outside the frozen research universe")
        if at > through:
            raise ValueError("signal is beyond feed coverage")
        rows.append(
            Signal(
                row["key"],
                row["symbol"],
                at,
                positive(row["u"]),
                positive(row["control_limit"])
                if row.get("control_limit") is not None
                else None,
            )
        )
    return rows, state


def public_bars(
    market: str, names: list[str], start: date, end: date
) -> tuple[dict, dict]:
    bars, failures = {}, {}
    for name in names:
        try:
            if market == "crypto":
                rows = _fetch_upbit(f"KRW-{name}", start, end + timedelta(days=1))
            else:
                rows, _ = _fetch_yahoo(
                    _yahoo_candidates(market, name)[0], start, end + timedelta(days=1)
                )
            for row in rows:
                day = date.fromisoformat(row["date"])
                if start <= day <= end:
                    split = Decimal(row.get("split", "0"))
                    if not split.is_finite() or split < 0:
                        raise ValueError("invalid split")
                    bars[name, day] = Bar(
                        day, positive(row["open"]), positive(row["close"]), split
                    )
        except Exception as exc:
            failures[name] = type(
                exc
            ).__name__  # no raw provider response/exception text
        if market == "crypto":
            time.sleep(0.15)
    return bars, failures
