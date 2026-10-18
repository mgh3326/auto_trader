"""Shared fixtures for ROB-1391 tests -- de-identified, but real shapes.

* order rows: exactly the column set the ``db_source`` SELECTs return for
  ``review.toss_live_order_ledger`` / ``review.live_order_ledger``
  (Numeric(20,8) values arrive as 8-decimal strings, ``trade_date`` as a
  tz-aware timestamp);
* Alpaca bars/quotes: the ``/v2/stocks/bars`` and ``/v2/stocks/quotes`` JSON
  objects (``t`` RFC-3339 with nanoseconds, ``o/h/l/c/v/n/vw``,
  ``ap/as/ax/bp/bs/bx/c/z``);
* calendar: Nov-Dec 2025 XNYS -- Thanksgiving holiday (11-27), early closes
  11-28 and 12-24, Christmas (12-25).
Tickers are placeholders (SYMA...), ids synthetic, amounts invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.rob1391_us_resubmit_shadow import engine
from research.rob1391_us_resubmit_shadow.db_source import SourceSnapshot
from research.rob1391_us_resubmit_shadow.ledger import Ledger
from research.rob1391_us_resubmit_shadow.manifest import build_manifest
from research.rob1391_us_resubmit_shadow.market_data import (
    OK,
    UNKNOWN,
    AskEvidence,
    BarsResult,
    QuoteResult,
    parse_bar,
    parse_valid_quote,
)
from research.rob1391_us_resubmit_shadow.us_calendar import ET, calendar_for

SEAL_AT = datetime(2025, 11, 14, 12, 0, tzinfo=UTC)  # before the 11-14 open
POLICY = {"version": "2026-09-30.1", "content_hash": "62c7a733a778"}
CODE_SHA = "c0ffee" * 6 + "abcd"
FAR_FUTURE = datetime(2026, 3, 2, tzinfo=UTC)
CAL = calendar_for(SEAL_AT.date())

FEE_SCHEDULE = {
    "schedule_id": "fixture-2025-11",
    "source": "fixture (not a real broker schedule)",
    "as_of": "2025-11-13",
    "by_source": {
        "toss_live_order_ledger": {
            "commission_rate": "0.001",
            "min_commission_usd": "0",
        },
        "live_order_ledger": {"commission_rate": "0.0025", "min_commission_usd": "0"},
    },
    "sec_fee_rate": "0.0000278",
    "finra_taf_per_share": "0.000166",
    "finra_taf_max_usd": "8.30",
    "fx": "none_usd_ledger",
}


def d8(value: Any) -> str | None:
    """Numeric(20,8) as asyncpg hands it to us (8 decimals)."""

    return None if value is None else f"{Decimal(str(value)):.8f}"


def toss_row(
    source_id: int,
    symbol: str,
    *,
    ordered_at: datetime,
    price: Any,
    qty: Any,
    status: str = "expired",
    expired_at: datetime | None = None,
    filled: Any = 0,
    avg: Any = None,
    **over: Any,
) -> dict[str, Any]:
    if expired_at is None and status == "expired":
        d0 = CAL.working_session(ordered_at)
        # Toss sweeps US DAY orders at 08:50 KST = 23:50 UTC, after the close.
        expired_at = datetime.combine(d0, datetime.min.time(), tzinfo=UTC) + timedelta(
            hours=23, minutes=50, seconds=12
        )
    row = {
        "source": "toss_live_order_ledger",
        "source_id": str(source_id),
        "broker_order_id": f"TOSSORD{source_id:08d}",
        "symbol": symbol,
        "market": "us",
        "side": "buy",
        "order_type": "limit",
        "operation_kind": "place",
        "time_in_force": None,
        "quantity": d8(qty),
        "price": d8(price),
        "currency": "USD",
        "status": status,
        "requires_manual_review": False,
        "ordered_at": ordered_at,
        "expired_at": expired_at,
        "filled_qty": d8(filled),
        "avg_fill_price": d8(avg),
        "exchange": None,
    }
    row.update(over)
    return row


def kis_row(
    source_id: int,
    symbol: str,
    *,
    ordered_at: datetime,
    price: Any,
    qty: Any,
    status: str = "expired",
    filled: Any = None,
    **over: Any,
) -> dict[str, Any]:
    row = {
        "source": "live_order_ledger",
        "source_id": str(source_id),
        "broker_order_id": f"00301{source_id:05d}",
        "symbol": symbol,
        "market": "us",
        "side": "buy",
        "order_type": "limit",
        "operation_kind": "place",
        "time_in_force": None,
        "quantity": d8(qty),
        "price": d8(price),
        "currency": "USD",
        "status": status,
        "requires_manual_review": False,
        "ordered_at": ordered_at,
        "expired_at": None,
        "filled_qty": d8(filled),
        "avg_fill_price": None,
        "exchange": "NASD",
    }
    row.update(over)
    return row


def universe_row(symbol: str, **over: Any) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "exchange": "NASD",
        "name_en": f"{symbol} Holdings Inc",
        "is_active": True,
        "is_common_stock": True,
        "security_type": "STOCK",
        "is_common_share": True,
        "listing_status": "LISTED",
        "leverage_factor": None,
    }
    row.update(over)
    return row


def rung_row(
    broker_order_id: str,
    *,
    qty: Any,
    price: Any,
    notional: Any = None,
    root: str = "00000000-0000-4000-8000-000000000001",
    account_mode: str = "toss_live",
    rung_index: int = 0,
    policy_version: str | None = None,
) -> dict[str, Any]:
    return {
        "broker_order_id": broker_order_id,
        "rung_index": rung_index,
        "rung_quantity": f"{Decimal(str(qty)):.12f}",
        "rung_limit_price": f"{Decimal(str(price)):.12f}",
        "rung_notional": None if notional is None else f"{Decimal(str(notional)):.12f}",
        "proposal_root": root,
        "account_mode": account_mode,
        "order_policy_version": policy_version,
    }


def et(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET).astimezone(UTC)


def z(ts: datetime, nanos: str = "") -> str:
    """Alpaca timestamp string (``Z``, optional nanosecond tail)."""

    base = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}{nanos}Z"


def bar_json(
    ts: datetime,
    *,
    o: float,
    h: float,
    low: float,
    c: float,
    v: int | None,
    vw: float | None = None,
) -> dict[str, Any]:
    out = {
        "t": z(ts),
        "o": o,
        "h": h,
        "l": low,
        "c": c,
        "v": v,
        "n": 37,
        "vw": vw if vw is not None else c,
    }
    if v is None:
        out.pop("v")
    return out


def quote_json(
    ts: datetime,
    *,
    bid: float,
    ask: float,
    nanos: str = ".012345678",
    bs: int = 3,
    as_: int = 2,
) -> dict[str, Any]:
    return {
        "t": z(ts, nanos),
        "ax": "Q",
        "ap": ask,
        "as": as_,
        "bx": "P",
        "bp": bid,
        "bs": bs,
        "c": ["R"],
        "z": "C",
    }


class FakeSource:
    def __init__(self) -> None:
        self.order_rows: list[dict[str, Any]] = []
        self.rung_rows: list[dict[str, Any]] = []
        self.universe_rows: list[dict[str, Any]] = []
        self.calls = 0

    def fetch(self, since: datetime) -> SourceSnapshot:
        self.calls += 1
        return SourceSnapshot(
            order_rows=[dict(r) for r in self.order_rows if r["ordered_at"] >= since],
            rung_rows=[dict(r) for r in self.rung_rows],
            universe_rows=[dict(r) for r in self.universe_rows],
        )


@dataclass
class FakeMarket:
    """Serves Alpaca-shaped JSON through the real parsers."""

    feed: str = "sip"
    price: dict[str, float] = field(default_factory=dict)
    daily_override: dict[tuple[str, str, date], dict[str, Any] | None] = field(
        default_factory=dict
    )
    minutes: dict[tuple[str, date], list[dict[str, Any]]] = field(default_factory=dict)
    quotes: dict[tuple[str, date], list[dict[str, Any]]] = field(default_factory=dict)
    unknown: set[tuple[str, str, date]] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _daily_json(
        self, symbol: str, day: date, adjustment: str
    ) -> dict[str, Any] | None:
        key = (symbol, adjustment, day)
        if key in self.daily_override:
            return self.daily_override[key]
        if adjustment == "split" and (symbol, "raw", day) in self.daily_override:
            return self.daily_override[(symbol, "raw", day)]
        p = self.price.get(symbol, 100.0)
        return bar_json(
            datetime(day.year, day.month, day.day, 5, tzinfo=UTC),
            o=p,
            h=p,
            low=p,
            c=p,
            v=1_000_000,
            vw=p,
        )

    def daily_bars(
        self, symbol: str, start: date, end: date, *, adjustment: str = "raw"
    ) -> BarsResult:
        self.calls.append(("daily", symbol))
        if ("daily", symbol, end) in self.unknown:
            return BarsResult(UNKNOWN, reason="http_500", digests=("d-unknown",))
        bars = []
        day = start
        while day <= end:
            if CAL.is_session(day):
                raw = self._daily_json(symbol, day, adjustment)
                if raw is not None:
                    bars.append(parse_bar(raw))
            day += timedelta(days=1)
        return BarsResult(OK, bars=tuple(bars), digests=(f"d-{symbol}-{end}",))

    def _session_minutes(self, symbol: str, day: date) -> list[dict[str, Any]]:
        if (symbol, day) in self.minutes:
            return self.minutes[(symbol, day)]
        sess = CAL.session(day)
        p = self.price.get(symbol, 100.0)
        out, t = [], sess.open_utc
        while t < sess.close_utc:
            out.append(bar_json(t, o=p, h=p * 1.01, low=p * 1.005, c=p, v=20_000))
            t += timedelta(minutes=1)
        return out

    def minute_bars(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> BarsResult:
        day = start_utc.astimezone(ET).date()
        self.calls.append(("minute", symbol))
        if ("minute", symbol, day) in self.unknown:
            return BarsResult(UNKNOWN, reason="pagination_cap")
        bars = [parse_bar(b) for b in self._session_minutes(symbol, day)]
        return BarsResult(
            OK,
            bars=tuple(b for b in bars if start_utc <= b.start_utc < end_utc),
            digests=(f"m-{symbol}-{day}",),
        )

    def _session_quotes(self, symbol: str, day: date) -> list[dict[str, Any]]:
        if (symbol, day) in self.quotes:
            return self.quotes[(symbol, day)]
        p = self.price.get(symbol, 100.0)
        sess = CAL.session(day)
        return [
            quote_json(
                sess.open_utc + timedelta(seconds=1),
                bid=round(p - 0.01, 2),
                ask=round(p + 0.01, 2),
            )
        ]

    def first_valid_quote(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> QuoteResult:
        day = start_utc.astimezone(ET).date()
        self.calls.append(("quote", symbol))
        if ("quote", symbol, day) in self.unknown:
            return QuoteResult(UNKNOWN, reason="http_503")
        for raw in self._session_quotes(symbol, day):
            q = parse_valid_quote(raw)
            if q is not None and start_utc <= q.at_utc < end_utc:
                return QuoteResult(OK, quote=q, digests=(f"q-{symbol}-{day}",))
        return QuoteResult(OK, quote=None, reason="no_valid_quote")

    def ask_at_or_below(
        self, symbol: str, start_utc: datetime, end_utc: datetime, limit: Decimal
    ) -> AskEvidence:
        day = start_utc.astimezone(ET).date()
        self.calls.append(("ask", symbol))
        if ("ask", symbol, day) in self.unknown:
            return AskEvidence(UNKNOWN, reason="pagination_cap")
        for raw in self._session_quotes(symbol, day):
            q = parse_valid_quote(raw)
            if q is None or not (start_utc <= q.at_utc < end_utc):
                continue
            if q.ask <= limit:
                return AskEvidence(
                    OK, found=True, at_utc=q.at_utc, ask=q.ask, digests=("a",)
                )
        return AskEvidence(OK, found=False, digests=("a",))


def seal(
    tmp_path: Path,
    *,
    fee: bool = True,
    budget_rule: str = "prereg_fill_plus_cost_le_budget",
    deviation_ref: str | None = None,
) -> Ledger:
    import json

    prereg = tmp_path / "prereg.md"
    prereg.write_text("fixture prereg (not the private original)\n")
    fee_path = None
    if fee:
        fee_path = tmp_path / "fees.json"
        fee_path.write_text(json.dumps(FEE_SCHEDULE))
    manifest = build_manifest(
        seal_at=SEAL_AT,
        approval_ref="fixture-approval",
        prereg_path=prereg,
        code_sha=CODE_SHA,
        policy_stamp=dict(POLICY),
        deployed_policy=dict(POLICY),
        feed="sip",
        fee_schedule_path=fee_path,
        budget_rule=budget_rule,
        deviation_ref=deviation_ref,
    )
    ledger = Ledger(tmp_path / "ledger")
    digest = ledger.write_manifest(manifest)
    ledger.append("sealed", {"manifest_sha256": digest}, now=SEAL_AT)
    return ledger


def context(
    ledger: Ledger,
    source: FakeSource,
    market: FakeMarket,
    *,
    now: datetime = FAR_FUTURE,
    policy: dict | None = None,
    modules: dict | None = None,
    eng: Any = engine,
) -> engine.Context:
    clock = {"now": now}
    ctx = eng.Context(
        ledger=ledger,
        market=market,
        source=source,
        now=lambda: clock["now"],
        code_sha=lambda: CODE_SHA,
        policy_stamp=lambda: dict(policy or POLICY),
        loaded_modules=lambda: dict(modules or {}),
    )
    ctx.clock = clock  # type: ignore[attr-defined]
    ctx.load()
    return ctx


def run_through(ctx: engine.Context, day: date) -> list[tuple[str, str]]:
    return engine.advance(ctx, day)
