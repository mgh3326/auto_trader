"""Fake Upbit public endpoint over recorded or synthetic candles (hk 6036)."""

from __future__ import annotations

import csv
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.upbit_intraday_shadow.public_data import UpbitPublicData, slot_of
from research.upbit_intraday.fetch import MARKETS_URL, PAGE_SIZE, Pacer, Row
from research.upbit_intraday.public_http import FetchResponse, check_url

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "upbit_intraday_shadow"
    / "2026-11-25"
)
KST = timedelta(hours=9)
QUIET = {
    "warning": False,
    "caution": {"PRICE_FLUCTUATIONS": False, "TRADING_VOLUME_SOARING": False},
}


def kst(text: str) -> datetime:
    """``"11-25 09:00"`` (KST, 2026) as an aware UTC datetime."""
    local = datetime.strptime(f"2026-{text}", "%Y-%m-%d %H:%M")
    return (local - KST).replace(tzinfo=UTC)


def kst_label(when: datetime) -> str:
    return (when.astimezone(UTC) + KST).strftime("%m-%d %H:%M")


def recorded_rows(market: str) -> list[Row]:
    with (FIXTURES / f"{market}.csv").open(newline="") as fh:
        return [Row(**record) for record in csv.DictReader(fh)]


def recorded_market_all() -> list[dict]:
    return json.loads((FIXTURES / "market_all.json").read_text())


def _candle_json(market: str, row: Row) -> str:
    # Numbers go into the body as the exact text Upbit sent.
    return (
        f'{{"market":"{market}","candle_date_time_utc":"{row.time_utc}",'
        f'"opening_price":{row.open},"high_price":{row.high},'
        f'"low_price":{row.low},"trade_price":{row.close},'
        f'"candle_acc_trade_price":{row.quote_volume},'
        f'"candle_acc_trade_volume":{row.volume},"unit":15}}'
    )


class FakeUpbit:
    """Answers the two allowlisted URLs; anything else fails the allowlist."""

    def __init__(
        self,
        candles: dict[str, list[Row]],
        market_events: dict[str, object] | None = None,
        *,
        statuses: dict[str, int] | None = None,
    ) -> None:
        self.candles = candles
        events = (
            dict.fromkeys(candles, QUIET) if market_events is None else market_events
        )
        self.market_all = [{"market": m, "market_event": e} for m, e in events.items()]
        self.statuses = statuses or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> FetchResponse:
        check_url(url)
        self.urls.append(url)
        if url == MARKETS_URL:
            return FetchResponse(200, json.dumps(self.market_all).encode())
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        market = query["market"]
        status = self.statuses.get(market, 200 if market in self.candles else 404)
        if status != 200:
            return FetchResponse(status, b'{"error":{"name":404}}')
        before = datetime.fromisoformat(query["to"].replace("Z", "+00:00"))
        rows = sorted(
            (
                r
                for r in self.candles[market]
                if datetime.fromisoformat(r.time_utc).replace(tzinfo=UTC) < before
            ),
            key=lambda r: r.time_utc,
            reverse=True,
        )[:PAGE_SIZE]
        body = "[" + ",".join(_candle_json(market, r) for r in rows) + "]"
        return FetchResponse(200, body.encode())


def data_at(fake: FakeUpbit, now: datetime) -> UpbitPublicData:
    return UpbitPublicData(
        fake, pacer=Pacer(0.0, sleep=lambda _s: None), clock=lambda: now
    )


# --- synthetic candles ------------------------------------------------------------

BASE = kst("11-20 00:00")


def bar(
    index: int,
    *,
    o: str = "100",
    h: str = "101",
    low: str = "99",
    c: str = "100",
    q: str = "40000000",
) -> Row:
    """Candle ``index`` quarter hours after ``BASE``."""
    when = BASE + timedelta(minutes=15 * index)
    return Row(when.strftime("%Y-%m-%dT%H:%M:%S"), o, h, low, c, "1", q)


def at(index: int) -> datetime:
    return BASE + timedelta(minutes=15 * index)


def run_after(index: int) -> datetime:
    """A run one minute after bar ``index`` closed."""
    return at(index + 1) + timedelta(minutes=1)


def end_slot_after(index: int) -> int:
    return slot_of(at(index))
