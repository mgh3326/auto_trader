# Binance Demo WS Scalping Daemon — Slice 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the event-driven trigger pipeline for the ROB-317 daemon — futures stream parsing (`fstream` aggTrade/bookTicker/closed-kline), per-symbol state updates, a candle-buffer signal that reuses `evaluate_signal` unchanged, and an asyncio supervisor that turns closed-kline events into freshness-gated, debounced `TriggerEvent`s with reconnect/backoff. **No broker mutation and no risk re-check yet — that is slice 4.**

**Architecture:** Slice 3 of 4 (slice 1 = design doc, slice 2 = inert foundation, both landed). The trigger fires on **closed 1m kline** events over a rolling candle buffer (reacting at candle close instead of up to 5 min later), while bookTicker/aggTrade feed quote/trade **freshness** that gates the trigger. The signal logic (`demo_scalping/signal.evaluate_signal`) and risk contract are reused verbatim. The supervisor consumes an **injectable event source**, so every test uses a fake async stream — no network, no orders. The real `fstream` client is thin glue over the injectable seam. When the daemon is active, `on_trigger` only logs structured JSON in this slice; slice 4 replaces that callback with the executor bridge.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `pytest-asyncio`, `ruff`, `websockets`, stdlib `asyncio`/`collections.deque`/`datetime`/`decimal`/`json`. Reuses ROB-285 `compute_backoff_delay`/`is_unhealthy` and the `127.0.0.1` ws test override pattern.

---

## Boundary & reuse notes (read before starting)

- `demo_scalping_ws/` stays **read-only**: it may import `app.services.brokers.binance.ws_client` (KlineEvent/BookTickerEvent + backoff helpers), `demo_scalping.signal` (`Candle`, `SignalConfig`, `evaluate_signal`, `SignalDecision`), `demo_scalping.contract` (`ReasonCode`, `Side`, `Product`), and `host_allowlist`. It must **NOT** import any execution client, `demo_scalping_exec`, or `demo.ledger` — the AST guard from slice 2 enforces this and must stay green.
- `evaluate_signal(candles, config)` needs `max(sma_slow=25, breakout_lookback+1=21) = 25` candles; fewer returns `INSUFFICIENT_HISTORY`. The buffer keeps `maxlen=200`.
- `MarketState` (slice 2) is **not modified**. Freshness uses its existing `book_ticker_at`/`agg_trade_at` + `is_stale`. Closed-kline receipt does **not** refresh quote freshness on purpose: a dead bookTicker stream must still trip `STALE_DATA` even while klines arrive.
- The futures combined-stream payload differs from spot: futures `bookTicker` carries `"e":"bookTicker"` and there is an `aggTrade` stream the ROB-285 spot parser does not handle — hence a dedicated futures parser here rather than editing the shared client.

## File Structure

| File | Responsibility |
|---|---|
| `app/services/brokers/binance/demo_scalping_ws/market_stream.py` (create) | `AggTradeEvent`, `parse_futures_message`, `build_futures_stream_url`, `FuturesMarketStream` (host-guarded WS client) |
| `app/services/brokers/binance/demo_scalping_ws/events.py` (create) | `apply_event` (event → `MarketState` mutation) + `kline_to_candle` |
| `app/services/brokers/binance/demo_scalping_ws/signal.py` (create) | `EventDrivenSignal` — rolling candle buffer + `evaluate_signal` |
| `app/services/brokers/binance/demo_scalping_ws/supervisor.py` (create) | `TriggerEvent`, `ScalpingDaemonSupervisor` — asyncio loop, freshness gate, debounce, reconnect/backoff |
| `scripts/binance_demo_scalping_ws_daemon.py` (modify) | Active path builds supervisor + real stream; `on_trigger` logs structured JSON (no mutation) |
| `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py` (create) | parser + url builder + host guard |
| `tests/services/brokers/binance/demo_scalping_ws/test_events.py` (create) | `apply_event` + `kline_to_candle` |
| `tests/services/brokers/binance/demo_scalping_ws/test_signal.py` (create) | `EventDrivenSignal` |
| `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py` (create) | trigger / freshness / debounce / reconnect (fake source) |
| `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_live_fixture.py` (create) | `FuturesMarketStream` round-trip via local `websockets.serve` |

**Deferred to slice 4 (do NOT create here):** `demo_scalping_exec/ws_bridge.py`, risk re-check wiring, concurrency guard, confirm-gated executor call, analytics/review wiring, dedicated runbook.

---

### Task 1: Futures stream parsing + URL builder + host guard

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/market_stream.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py`:

```python
"""ROB-317 — futures stream parsing + URL builder + host guard."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    build_futures_stream_url,
    parse_futures_message,
)

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def _wrap(stream: str, data: dict) -> str:
    return json.dumps({"stream": stream, "data": data})


def test_parse_agg_trade() -> None:
    raw = _wrap(
        "xrpusdt@aggTrade",
        {"e": "aggTrade", "s": "XRPUSDT", "p": "0.5123", "q": "100",
         "T": 1716724800000, "m": True},
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, AggTradeEvent)
    assert ev.symbol == "XRPUSDT"
    assert ev.price == Decimal("0.5123")
    assert ev.is_buyer_maker is True


def test_parse_book_ticker_futures_has_e_field() -> None:
    raw = _wrap(
        "xrpusdt@bookTicker",
        {"e": "bookTicker", "u": 400900217, "s": "XRPUSDT",
         "b": "0.5120", "B": "31.2", "a": "0.5125", "A": "40.6"},
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, BookTickerEvent)
    assert ev.bid_price == Decimal("0.5120")
    assert ev.ask_price == Decimal("0.5125")
    assert ev.received_at == _NOW


def test_parse_closed_kline() -> None:
    raw = _wrap(
        "xrpusdt@kline_1m",
        {"e": "kline", "s": "XRPUSDT", "k": {
            "t": 1716724740000, "T": 1716724799999, "s": "XRPUSDT", "i": "1m",
            "o": "0.50", "h": "0.52", "l": "0.49", "c": "0.515",
            "v": "1000", "q": "515", "n": 42, "x": True}},
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, KlineEvent)
    assert ev.is_closed is True
    assert ev.close == Decimal("0.515")


def test_parse_drops_in_progress_kline() -> None:
    raw = _wrap(
        "xrpusdt@kline_1m",
        {"e": "kline", "s": "XRPUSDT", "k": {
            "t": 1716724740000, "T": 1716724799999, "s": "XRPUSDT", "i": "1m",
            "o": "0.50", "h": "0.52", "l": "0.49", "c": "0.515",
            "v": "1000", "q": "515", "n": 42, "x": False}},
    )
    assert parse_futures_message(raw, now=_NOW) is None


def test_parse_ignores_garbage_and_unknown() -> None:
    assert parse_futures_message("not json", now=_NOW) is None
    assert parse_futures_message(_wrap("x@depth", {"e": "depthUpdate"}), now=_NOW) is None


def test_build_url_combines_streams_for_allowlisted_host() -> None:
    url = build_futures_stream_url(
        ["XRPUSDT", "DOGEUSDT"],
        streams=("aggTrade", "bookTicker", "kline_1m"),
        base_url="wss://fstream.binance.com",
    )
    assert url.startswith("wss://fstream.binance.com/stream?streams=")
    assert "xrpusdt@aggTrade" in url
    assert "dogeusdt@kline_1m" in url


def test_build_url_rejects_non_fstream_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        build_futures_stream_url(
            ["XRPUSDT"], streams=("aggTrade",), base_url="wss://fapi.binance.com"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: ... market_stream`.

- [ ] **Step 3: Create `market_stream.py`**

```python
"""ROB-317 — futures public market stream (read-only, fstream).

Parses the USD-M futures combined-stream payloads the daemon consumes:
aggTrade (momentum/freshness), bookTicker (spread/freshness), and closed
kline_1m (signal). Unsigned, read-only. Host is guarded against the
read-only PUBLIC_FUTURES_STREAM_HOSTS allowlist — never a signed mutation
host. See ROB-317 design §2, §4.

This module intentionally does NOT reuse BinancePublicWSClient's parser:
the futures payload shape differs (bookTicker carries "e":"bookTicker";
aggTrade is a stream the spot parser does not handle). It reuses only the
pure backoff helpers (compute_backoff_delay / is_unhealthy).
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
)
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent

_DEFAULT_BASE_URL = "wss://fstream.binance.com"


@dataclass(frozen=True, slots=True)
class AggTradeEvent:
    symbol: str
    price: Decimal
    qty: Decimal
    trade_time: dt.datetime
    is_buyer_maker: bool


FuturesWsEvent = KlineEvent | BookTickerEvent | AggTradeEvent


def parse_futures_message(raw: str, *, now: dt.datetime) -> FuturesWsEvent | None:
    """Parse one combined-stream message into a normalized event, or None.

    Returns None for malformed JSON, in-progress klines (``x: False``), and
    stream types the daemon does not consume. ``now`` is the receipt time
    used for bookTicker freshness (injected for deterministic tests).
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
    if not isinstance(data, dict):
        return None
    etype = data.get("e")
    if etype == "kline":
        k = data.get("k") or {}
        if not k.get("x"):
            return None
        return KlineEvent(
            symbol=k["s"],
            interval=k["i"],
            open_time=dt.datetime.fromtimestamp(k["t"] / 1000.0, tz=dt.UTC),
            close_time=dt.datetime.fromtimestamp(k["T"] / 1000.0, tz=dt.UTC),
            open=Decimal(k["o"]),
            high=Decimal(k["h"]),
            low=Decimal(k["l"]),
            close=Decimal(k["c"]),
            base_volume=Decimal(k["v"]),
            quote_volume=Decimal(k["q"]),
            trade_count=int(k["n"]),
            is_closed=True,
        )
    if etype == "bookTicker":
        return BookTickerEvent(
            symbol=data["s"],
            bid_price=Decimal(data["b"]),
            bid_qty=Decimal(data["B"]),
            ask_price=Decimal(data["a"]),
            ask_qty=Decimal(data["A"]),
            received_at=now,
        )
    if etype == "aggTrade":
        return AggTradeEvent(
            symbol=data["s"],
            price=Decimal(data["p"]),
            qty=Decimal(data["q"]),
            trade_time=dt.datetime.fromtimestamp(data["T"] / 1000.0, tz=dt.UTC),
            is_buyer_maker=bool(data["m"]),
        )
    return None


def _assert_host_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    # Local test override mirrors ROB-285: 127.0.0.1 over plain ws is the
    # websockets.serve fixture; production is always wss to fstream.
    if host == "127.0.0.1" and parsed.scheme == "ws":
        return
    if host not in PUBLIC_FUTURES_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Futures stream host blocked: {host!r} not in "
            f"{sorted(PUBLIC_FUTURES_STREAM_HOSTS)}"
        )


def build_futures_stream_url(
    symbols: Sequence[str],
    *,
    streams: Sequence[str],
    base_url: str = _DEFAULT_BASE_URL,
) -> str:
    """Build a combined-stream URL for ``symbols`` × ``streams``, host-guarded."""
    _assert_host_allowed(base_url)
    parts = [f"{s.lower()}@{stream}" for s in symbols for stream in streams]
    return f"{base_url.rstrip('/')}/stream?streams=" + "/".join(parts)


class FuturesMarketStream:
    """Read-only futures combined-stream subscriber (host-guarded)."""

    def __init__(self, *, url: str) -> None:
        _assert_host_allowed(url)
        self._url = url
        self._ws: Any = None

    async def __aenter__(self) -> "FuturesMarketStream":
        self._ws = await websockets.connect(self._url, ping_interval=20)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def events(
        self, *, stop_after: int | None = None
    ) -> AsyncIterator[FuturesWsEvent]:
        assert self._ws is not None, "FuturesMarketStream not connected"
        emitted = 0
        async for raw in self._ws:
            ev = parse_futures_message(raw, now=dt.datetime.now(tz=dt.UTC))
            if ev is None:
                continue
            yield ev
            emitted += 1
            if stop_after is not None and emitted >= stop_after:
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/market_stream.py \
        tests/services/brokers/binance/demo_scalping_ws/test_market_stream_parse.py
git commit -m "$(cat <<'EOF'
feat(rob-317): futures stream parser + url builder + host guard

Slice 3. parse_futures_message (aggTrade/bookTicker/closed-kline; drops
in-progress klines + garbage), build_futures_stream_url (host-guarded to
fstream), FuturesMarketStream client. Read-only; no signed host.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Event → state updater + kline → candle mapping

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/events.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_events.py`:

```python
"""ROB-317 — event → MarketState updater + kline → Candle mapping."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from app.services.brokers.binance.demo_scalping_ws.events import (
    apply_event,
    kline_to_candle,
)
from app.services.brokers.binance.demo_scalping_ws.market_stream import AggTradeEvent
from app.services.brokers.binance.demo_scalping_ws.state import MarketState

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def _kline(close: str = "0.515") -> KlineEvent:
    return KlineEvent(
        symbol="XRPUSDT", interval="1m",
        open_time=dt.datetime(2026, 5, 26, 11, 59, tzinfo=dt.UTC),
        close_time=dt.datetime(2026, 5, 26, 11, 59, 59, tzinfo=dt.UTC),
        open=Decimal("0.50"), high=Decimal("0.52"), low=Decimal("0.49"),
        close=Decimal(close), base_volume=Decimal("1000"),
        quote_volume=Decimal("515"), trade_count=42, is_closed=True,
    )


def test_apply_book_ticker_updates_quote_and_freshness() -> None:
    state = MarketState(symbol="XRPUSDT")
    ev = BookTickerEvent(
        symbol="XRPUSDT", bid_price=Decimal("0.512"), bid_qty=Decimal("1"),
        ask_price=Decimal("0.513"), ask_qty=Decimal("1"), received_at=_NOW,
    )
    apply_event(state, ev)
    assert state.bid_price == Decimal("0.512")
    assert state.ask_price == Decimal("0.513")
    # Freshness comes from the event's own receipt time, not a wall clock.
    assert state.book_ticker_at == _NOW


def test_apply_agg_trade_updates_trade_and_freshness() -> None:
    state = MarketState(symbol="XRPUSDT")
    ev = AggTradeEvent(
        symbol="XRPUSDT", price=Decimal("0.5125"), qty=Decimal("10"),
        trade_time=_NOW, is_buyer_maker=False,
    )
    apply_event(state, ev)
    assert state.last_trade_price == Decimal("0.5125")
    assert state.agg_trade_at == _NOW


def test_apply_kline_does_not_touch_quote_freshness() -> None:
    # A closed kline must NOT mask a dead bookTicker/aggTrade stream.
    state = MarketState(symbol="XRPUSDT")
    apply_event(state, _kline())
    assert state.book_ticker_at is None
    assert state.agg_trade_at is None


def test_kline_to_candle_maps_fields() -> None:
    candle = kline_to_candle(_kline(close="0.515"))
    assert candle.close == Decimal("0.515")
    assert candle.high == Decimal("0.52")
    assert candle.close_time_ms == int(
        dt.datetime(2026, 5, 26, 11, 59, 59, tzinfo=dt.UTC).timestamp() * 1000
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: ... events`.

- [ ] **Step 3: Create `events.py`**

```python
"""ROB-317 — event → state mutation + kline → Candle mapping (pure).

``apply_event`` updates quote/trade fields + freshness timestamps from
bookTicker/aggTrade events. Freshness is stamped from each event's OWN
receipt/trade time (design §5: "last event received"), not a wall clock, so
the supervisor's trigger-time clock measures true staleness against it.
Closed klines deliberately do NOT update quote freshness (a dead bookTicker
stream must still trip STALE_DATA); klines are routed to the signal buffer
by the supervisor instead.
"""

from __future__ import annotations

from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    FuturesWsEvent,
)
from app.services.brokers.binance.demo_scalping_ws.state import MarketState


def apply_event(state: MarketState, event: FuturesWsEvent) -> None:
    """Mutate ``state`` from a market event. Klines are a no-op here.

    Freshness timestamps come from the event itself (``received_at`` for
    bookTicker, ``trade_time`` for aggTrade).
    """
    if isinstance(event, BookTickerEvent):
        state.bid_price = event.bid_price
        state.ask_price = event.ask_price
        state.book_ticker_at = event.received_at
    elif isinstance(event, AggTradeEvent):
        state.last_trade_price = event.price
        state.agg_trade_at = event.trade_time
    # KlineEvent: intentionally no state mutation (routed to the signal buffer).


def kline_to_candle(event: KlineEvent) -> Candle:
    """Map a closed-kline event to the signal's Candle value object."""
    return Candle(
        open_time_ms=int(event.open_time.timestamp() * 1000),
        open=event.open,
        high=event.high,
        low=event.low,
        close=event.close,
        close_time_ms=int(event.close_time.timestamp() * 1000),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_events.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/events.py \
        tests/services/brokers/binance/demo_scalping_ws/test_events.py
git commit -m "$(cat <<'EOF'
feat(rob-317): event->state updater + kline->candle mapping

Slice 3. apply_event refreshes quote/trade + freshness from bookTicker/
aggTrade; closed klines do not touch quote freshness (dead quote stream
still trips STALE_DATA). kline_to_candle bridges to the signal Candle.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Event-driven signal (rolling candle buffer)

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/signal.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_signal.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_signal.py`:

```python
"""ROB-317 — event-driven signal over a rolling candle buffer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import ReasonCode
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.brokers.binance.demo_scalping_ws.signal import EventDrivenSignal


def _kline(close: str, *, high: str | None = None, low: str | None = None,
           minute: int = 0) -> KlineEvent:
    base = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC) + dt.timedelta(minutes=minute)
    c = Decimal(close)
    return KlineEvent(
        symbol="XRPUSDT", interval="1m", open_time=base,
        close_time=base + dt.timedelta(seconds=59), open=c,
        high=Decimal(high) if high else c, low=Decimal(low) if low else c,
        close=c, base_volume=Decimal("1000"), quote_volume=Decimal("515"),
        trade_count=42, is_closed=True,
    )


def test_insufficient_history_until_buffer_fills() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT")
    decision = sig.ingest_kline(_kline("0.50", minute=0))
    assert decision.has_entry is False
    assert ReasonCode.INSUFFICIENT_HISTORY in decision.reason_codes


def test_long_breakout_fires_after_enough_candles() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT")
    # 24 flat candles at 0.50 (range 0.49-0.51), then a breakout close above
    # the prior 20-bar high with a rising fast SMA.
    last = None
    for m in range(24):
        last = sig.ingest_kline(_kline("0.50", high="0.51", low="0.49", minute=m))
    assert last.has_entry is False  # 24 < 25 needed
    fill = sig.ingest_kline(_kline("0.50", high="0.51", low="0.49", minute=24))
    assert fill.has_entry is False  # 25th: flat, no breakout
    breakout = sig.ingest_kline(_kline("0.60", high="0.60", low="0.50", minute=25))
    assert breakout.has_entry is True
    assert breakout.side == "BUY"
    assert ReasonCode.ENTER_LONG_BREAKOUT in breakout.reason_codes


def test_buffer_is_bounded() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT", max_candles=30)
    for m in range(100):
        sig.ingest_kline(_kline("0.50", minute=m))
    assert sig.candle_count == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_signal.py -v`
Expected: FAIL — `ModuleNotFoundError: ... signal` (the demo_scalping_ws one).

- [ ] **Step 3: Create `signal.py`**

```python
"""ROB-317 — event-driven scalping signal.

Wraps the pure, candle-based ``demo_scalping.signal.evaluate_signal`` with a
bounded rolling buffer. On each closed kline the buffer is appended and the
signal re-evaluated — reacting at candle close rather than on a 5-minute
timer. The strategy/thresholds are reused verbatim; only the feed cadence
changes. See ROB-317 design §1, §3.2.
"""

from __future__ import annotations

from collections import deque

from app.services.brokers.binance.demo_scalping.contract import Product
from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    SignalDecision,
    evaluate_signal,
)
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.brokers.binance.demo_scalping_ws.events import kline_to_candle


class EventDrivenSignal:
    """Per-symbol rolling-buffer adapter over ``evaluate_signal``."""

    def __init__(
        self,
        *,
        product: Product,
        symbol: str,
        config: SignalConfig | None = None,
        max_candles: int = 200,
    ) -> None:
        self._product = product
        self._symbol = symbol
        # Spot is long-only; futures may take the mirror short (same default
        # as demo_scalping.runner.evaluate_symbol).
        self._config = config or SignalConfig(allow_short=product == "usdm_futures")
        self._candles: deque[Candle] = deque(maxlen=max_candles)

    @property
    def candle_count(self) -> int:
        return len(self._candles)

    def ingest_kline(self, event: KlineEvent) -> SignalDecision:
        """Append the closed candle and re-evaluate the signal."""
        self._candles.append(kline_to_candle(event))
        return evaluate_signal(list(self._candles), self._config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_signal.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/signal.py \
        tests/services/brokers/binance/demo_scalping_ws/test_signal.py
git commit -m "$(cat <<'EOF'
feat(rob-317): event-driven signal over rolling candle buffer

Slice 3. EventDrivenSignal appends each closed kline to a bounded buffer and
re-runs the unchanged evaluate_signal — fires at candle close, not on a timer.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Supervisor — trigger, freshness gate, debounce (fake source)

The supervisor consumes an injectable event source. On a closed kline it routes to the per-symbol signal; if `has_entry`, it emits a `TriggerEvent` **only** when (a) the symbol's quote/trade data is fresh and (b) the per-symbol debounce window has elapsed. `now` and `sleep` are injected for deterministic tests. Reconnect is added in Task 5.

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/supervisor.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`:

```python
"""ROB-317 — supervisor trigger / freshness / debounce (fake source)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    FuturesWsEvent,
)
from app.services.brokers.binance.demo_scalping_ws.supervisor import (
    ScalpingDaemonSupervisor,
    TriggerEvent,
)

pytestmark = pytest.mark.asyncio

_T0 = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC)


def _book(now_offset: int = 0) -> BookTickerEvent:
    return BookTickerEvent(
        symbol="XRPUSDT", bid_price=Decimal("0.50"), bid_qty=Decimal("1"),
        ask_price=Decimal("0.5001"), ask_qty=Decimal("1"),
        received_at=_T0 + dt.timedelta(seconds=now_offset),
    )


def _kline(close: str, *, high: str, low: str, minute: int) -> KlineEvent:
    base = _T0 + dt.timedelta(minutes=minute)
    return KlineEvent(
        symbol="XRPUSDT", interval="1m", open_time=base,
        close_time=base + dt.timedelta(seconds=59), open=Decimal(close),
        high=Decimal(high), low=Decimal(low), close=Decimal(close),
        base_volume=Decimal("1000"), quote_volume=Decimal("515"),
        trade_count=42, is_closed=True,
    )


def _breakout_sequence() -> list[FuturesWsEvent]:
    events: list[FuturesWsEvent] = [_book()]
    for m in range(25):
        events.append(_kline("0.50", high="0.51", low="0.49", minute=m))
    events.append(_kline("0.60", high="0.60", low="0.50", minute=25))
    return events


async def _source_from(events: list[FuturesWsEvent]) -> AsyncIterator[FuturesWsEvent]:
    for ev in events:
        yield ev


class _Clock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now


async def test_fresh_breakout_emits_trigger() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))  # within 120s of the book event
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert len(captured) == 1
    assert captured[0].symbol == "XRPUSDT"
    assert captured[0].side == "BUY"


async def test_stale_quote_blocks_trigger() -> None:
    # Clock far past the single book event -> STALE_DATA, no trigger.
    clock = _Clock(_T0 + dt.timedelta(seconds=600))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert captured == []


async def test_debounce_suppresses_second_trigger() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(
        symbols=["XRPUSDT"], clock=clock, debounce_seconds=300
    )
    seq = _breakout_sequence()
    seq.append(_kline("0.70", high="0.70", low="0.60", minute=26))  # 2nd breakout
    captured: list[TriggerEvent] = []
    await sup.run(lambda: _source_from(seq), on_trigger=_async_appender(captured))
    assert len(captured) == 1  # second suppressed by debounce


def _async_appender(sink: list):
    async def _append(trigger) -> None:
        sink.append(trigger)
    return _append
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: ... supervisor`.

- [ ] **Step 3: Create `supervisor.py`**

```python
"""ROB-317 — asyncio scalping daemon supervisor.

Consumes an injectable event source (real fstream client in production, a
fake async iterator in tests). Routes closed klines to the per-symbol
signal; on an entry, emits a TriggerEvent only when the symbol's quote/trade
data is fresh and the per-symbol debounce window has elapsed. bookTicker/
aggTrade events refresh state. Reconnect/backoff is added in Task 5.

This slice performs NO risk re-check and NO broker mutation: ``on_trigger``
is a caller-supplied coroutine (slice 4 wires it to the executor bridge;
this slice logs structured JSON). See ROB-317 design §6.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import (
    Product,
    ReasonCode,
    ScalpingRiskLimits,
    Side,
)
from app.services.brokers.binance.demo_scalping.signal import (
    SignalConfig,
    SignalDecision,
)
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.brokers.binance.demo_scalping_ws.events import apply_event
from app.services.brokers.binance.demo_scalping_ws.market_stream import FuturesWsEvent
from app.services.brokers.binance.demo_scalping_ws.signal import EventDrivenSignal
from app.services.brokers.binance.demo_scalping_ws.state import MarketState

logger = logging.getLogger("rob317.demo_scalping_ws")

SourceFactory = Callable[[], AsyncIterator[FuturesWsEvent]]
OnTrigger = Callable[["TriggerEvent"], Awaitable[None]]
Clock = Callable[[], dt.datetime]


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """An event-driven entry candidate, pre-risk-check (slice 4 consumes it)."""

    product: Product
    symbol: str
    side: Side
    decision: SignalDecision
    bid_price: Decimal | None
    ask_price: Decimal | None
    data_age_seconds: float | None
    emitted_at: dt.datetime


def _default_clock() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class ScalpingDaemonSupervisor:
    """Per-symbol event router with freshness + debounce gates."""

    def __init__(
        self,
        *,
        symbols: list[str],
        product: Product = "usdm_futures",
        limits: ScalpingRiskLimits | None = None,
        signal_config: SignalConfig | None = None,
        debounce_seconds: float = 300.0,
        clock: Clock = _default_clock,
    ) -> None:
        self._product = product
        self._limits = limits or ScalpingRiskLimits()
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._state: dict[str, MarketState] = {
            s: MarketState(symbol=s) for s in symbols
        }
        self._signals: dict[str, EventDrivenSignal] = {
            s: EventDrivenSignal(
                product=product, symbol=s, config=signal_config
            )
            for s in symbols
        }
        self._last_trigger_at: dict[str, dt.datetime] = {}

    async def run(
        self,
        source_factory: SourceFactory,
        *,
        on_trigger: OnTrigger,
        stop_after: int | None = None,
    ) -> None:
        """Consume one source to exhaustion (single pass; reconnect: Task 5)."""
        consumed = 0
        source = source_factory()
        async for event in source:
            trigger = self._handle_event(event)
            if trigger is not None:
                await on_trigger(trigger)
            consumed += 1
            if stop_after is not None and consumed >= stop_after:
                return

    def _handle_event(self, event: FuturesWsEvent) -> TriggerEvent | None:
        now = self._clock()
        symbol = event.symbol
        state = self._state.get(symbol)
        if state is None:
            return None  # not an allowlisted/subscribed symbol
        if not isinstance(event, KlineEvent):
            apply_event(state, event)
            return None
        decision = self._signals[symbol].ingest_kline(event)
        if not decision.has_entry or decision.side is None:
            return None
        return self._gate_and_build(symbol, state, decision, now)

    def _gate_and_build(
        self,
        symbol: str,
        state: MarketState,
        decision: SignalDecision,
        now: dt.datetime,
    ) -> TriggerEvent | None:
        if state.is_stale(now=now, max_age_seconds=self._limits.max_data_age_seconds):
            logger.info(
                "trigger suppressed symbol=%s reason=%s",
                symbol,
                ReasonCode.STALE_DATA,
            )
            return None
        last = self._last_trigger_at.get(symbol)
        if last is not None and (now - last).total_seconds() < self._debounce_seconds:
            logger.info("trigger suppressed symbol=%s reason=debounce", symbol)
            return None
        self._last_trigger_at[symbol] = now
        last_event = state.last_event_at()
        age = None if last_event is None else (now - last_event).total_seconds()
        logger.info(
            "trigger symbol=%s side=%s reasons=%s",
            symbol,
            decision.side,
            decision.reason_codes,
        )
        return TriggerEvent(
            product=self._product,
            symbol=symbol,
            side=decision.side,
            decision=decision,
            bid_price=state.bid_price,
            ask_price=state.ask_price,
            data_age_seconds=age,
            emitted_at=now,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -v`
Expected: PASS (3 tests). If `pytest.mark.asyncio` is unrecognized, confirm `pytest-asyncio` is installed (`uv run python -c "import pytest_asyncio"`) and that `asyncio_mode = auto` or the marker is configured in `pyproject.toml`; otherwise add `@pytest.mark.asyncio` per test (already applied via `pytestmark`).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/supervisor.py \
        tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py
git commit -m "$(cat <<'EOF'
feat(rob-317): supervisor trigger pipeline + freshness + debounce

Slice 3. ScalpingDaemonSupervisor routes events: kline->signal->TriggerEvent,
gated by quote/trade freshness (STALE_DATA) and per-symbol debounce. Injectable
clock + source. No risk re-check, no mutation (slice 4).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Reconnect / backoff loop

Wrap the single-pass consume in a reconnect loop reusing ROB-285 `compute_backoff_delay`/`is_unhealthy`. `sleep` is injected so tests assert delays without waiting. On clean source exhaustion the loop ends; on a connection error it backs off and re-invokes the factory until the unhealthy threshold, then re-raises.

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_ws/supervisor.py`
- Test: append to `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`:

```python
async def test_run_with_reconnect_recovers_after_transient_error() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    slept: list[float] = []

    calls = {"n": 0}

    def factory() -> AsyncIterator[FuturesWsEvent]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _raises_after_book()
        return _source_from(_breakout_sequence())

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await sup.run_with_reconnect(
        factory,
        on_trigger=_async_appender(captured),
        sleep=fake_sleep,
    )
    assert len(captured) == 1  # recovered on the 2nd connection
    assert len(slept) == 1  # backed off once between attempts


async def test_run_with_reconnect_raises_when_unhealthy() -> None:
    clock = _Clock(_T0)
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    def always_fails() -> AsyncIterator[FuturesWsEvent]:
        return _raises_immediately()

    with pytest.raises(ConnectionError):
        await sup.run_with_reconnect(
            always_fails, on_trigger=_async_appender([]), sleep=fake_sleep
        )
    assert len(slept) == 2  # 2 backoffs before the 3rd failure trips unhealthy


async def _raises_after_book() -> AsyncIterator[FuturesWsEvent]:
    yield _book()
    raise ConnectionError("socket dropped")


async def _raises_immediately() -> AsyncIterator[FuturesWsEvent]:
    raise ConnectionError("cannot connect")
    yield  # pragma: no cover - makes this an async generator
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -k reconnect -v`
Expected: FAIL — `AttributeError: 'ScalpingDaemonSupervisor' object has no attribute 'run_with_reconnect'`.

- [ ] **Step 3: Add `run_with_reconnect` to `supervisor.py`**

Add these imports near the top of `supervisor.py` (merge with existing import lines):

```python
import asyncio

from app.services.brokers.binance.ws_client import (
    compute_backoff_delay,
    is_unhealthy,
)
```

Add a module-level type alias next to the others:

```python
Sleep = Callable[[float], Awaitable[None]]
```

Add this method to `ScalpingDaemonSupervisor` (below `run`):

```python
    async def run_with_reconnect(
        self,
        source_factory: SourceFactory,
        *,
        on_trigger: OnTrigger,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Run with reconnect: back off on connection errors until unhealthy.

        Clean source exhaustion ends the loop. A connection error backs off
        (ROB-285 jittered exponential) and re-invokes the factory; once
        consecutive failures reach the unhealthy threshold the error is
        re-raised for the operator/supervisor above to handle.
        """
        consecutive_failures = 0
        while True:
            try:
                await self.run(source_factory, on_trigger=on_trigger)
                return
            except (ConnectionError, OSError) as exc:
                consecutive_failures += 1
                if is_unhealthy(consecutive_failures):
                    logger.error(
                        "WS daemon unhealthy after %d consecutive failures: %s",
                        consecutive_failures,
                        exc,
                    )
                    raise
                delay = compute_backoff_delay(consecutive_failures - 1)
                logger.warning(
                    "WS stream error (%s); reconnecting in %.2fs", exc, delay
                )
                await sleep(delay)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -v`
Expected: PASS (5 tests total). Confirm `is_unhealthy(3)` is the threshold: failures 1 and 2 sleep, failure 3 trips unhealthy → 2 sleeps then raise.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/supervisor.py \
        tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py
git commit -m "$(cat <<'EOF'
feat(rob-317): supervisor reconnect/backoff loop

Slice 3. run_with_reconnect reuses ROB-285 jittered backoff + unhealthy
threshold; injectable sleep. Recovers across a transient drop; re-raises once
consecutive failures hit the unhealthy threshold.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `FuturesMarketStream` live round-trip (local websockets server)

Proves the real client connects, decodes, and yields against a local `websockets.serve` fixture (the `127.0.0.1` ws host override added in Task 1). No external network.

**Files:**
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_live_fixture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_market_stream_live_fixture.py`:

```python
"""ROB-317 — FuturesMarketStream round-trip over a local websockets server."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import websockets

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    FuturesMarketStream,
)

pytestmark = pytest.mark.asyncio


async def test_stream_yields_parsed_events_from_local_server() -> None:
    messages = [
        json.dumps({"stream": "xrpusdt@aggTrade", "data": {
            "e": "aggTrade", "s": "XRPUSDT", "p": "0.51", "q": "1",
            "T": 1716724800000, "m": False}}),
        json.dumps({"stream": "xrpusdt@kline_1m", "data": {
            "e": "kline", "s": "XRPUSDT", "k": {
                "t": 1716724740000, "T": 1716724799999, "s": "XRPUSDT",
                "i": "1m", "o": "0.50", "h": "0.52", "l": "0.49", "c": "0.515",
                "v": "1000", "q": "515", "n": 42, "x": True}}}),
    ]

    async def handler(ws) -> None:
        for m in messages:
            await ws.send(m)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/stream?streams=xrpusdt@aggTrade"
        out = []
        async with FuturesMarketStream(url=url) as stream:
            async for ev in stream.events(stop_after=2):
                out.append(ev)

    assert isinstance(out[0], AggTradeEvent)
    assert out[0].price == Decimal("0.51")
    assert isinstance(out[1], KlineEvent)
    assert out[1].close == Decimal("0.515")


async def test_stream_rejects_non_fstream_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        FuturesMarketStream(url="wss://fapi.binance.com/stream?streams=xrpusdt@aggTrade")
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_market_stream_live_fixture.py -v`
Expected: PASS (2 tests). `FuturesMarketStream` and the host guard already exist from Task 1, so this validates the connect/iterate path. If it fails on connection, confirm `websockets.serve(..., port=0)` picks a free port and the `127.0.0.1`+`ws` override in `_assert_host_allowed` is present.

- [ ] **Step 3: Commit**

```bash
git add tests/services/brokers/binance/demo_scalping_ws/test_market_stream_live_fixture.py
git commit -m "$(cat <<'EOF'
test(rob-317): FuturesMarketStream local websockets round-trip

Slice 3. Proves connect/decode/yield + host rejection against a local
websockets.serve fixture (127.0.0.1 ws override). No external network.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Wire the CLI active path to the supervisor (logging sink, no mutation)

When `daemon_active` is true, the CLI builds the supervisor + real fstream source and runs `run_with_reconnect` with an `on_trigger` that **only logs structured JSON**. No bridge, no risk re-check, no order — slice 4 swaps the sink. To keep tests network-free, the runnable loop accepts an injectable `source_factory`.

**Files:**
- Modify: `scripts/binance_demo_scalping_ws_daemon.py`
- Test: append to `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`:

```python
import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from app.services.brokers.binance.demo_scalping_ws.market_stream import FuturesWsEvent
from scripts.binance_demo_scalping_ws_daemon import run_daemon

_T0 = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC)


def _seq() -> list[FuturesWsEvent]:
    out: list[FuturesWsEvent] = [
        BookTickerEvent(
            symbol="XRPUSDT", bid_price=Decimal("0.50"), bid_qty=Decimal("1"),
            ask_price=Decimal("0.5001"), ask_qty=Decimal("1"), received_at=_T0,
        )
    ]
    for m in range(25):
        out.append(_cli_kline("0.50", "0.51", "0.49", m))
    out.append(_cli_kline("0.60", "0.60", "0.50", 25))
    return out


def _cli_kline(close: str, high: str, low: str, minute: int) -> KlineEvent:
    base = _T0 + dt.timedelta(minutes=minute)
    return KlineEvent(
        symbol="XRPUSDT", interval="1m", open_time=base,
        close_time=base + dt.timedelta(seconds=59), open=Decimal(close),
        high=Decimal(high), low=Decimal(low), close=Decimal(close),
        base_volume=Decimal("1000"), quote_volume=Decimal("515"),
        trade_count=42, is_closed=True,
    )


@pytest.mark.asyncio
async def test_run_daemon_logs_triggers_without_mutation(caplog) -> None:
    triggers = await run_daemon(
        symbols=["XRPUSDT"],
        source_factory=lambda: _source(_seq()),
        clock=lambda: _T0 + dt.timedelta(seconds=5),
    )
    assert triggers == 1  # one BUY breakout, logged only


async def _source(events: list[FuturesWsEvent]) -> AsyncIterator[FuturesWsEvent]:
    for ev in events:
        yield ev
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -k run_daemon -v`
Expected: FAIL — `ImportError: cannot import name 'run_daemon'`.

- [ ] **Step 3: Add `run_daemon` and wire `main`**

Add to `scripts/binance_demo_scalping_ws_daemon.py` (new imports + function; keep `build_summary`/`_parse_args` from slice 2):

```python
import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Callable

from app.services.brokers.binance.demo_scalping.contract import DEFAULT_ALLOWLIST
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    FuturesWsEvent,
    FuturesMarketStream,
    build_futures_stream_url,
)
from app.services.brokers.binance.demo_scalping_ws.supervisor import (
    ScalpingDaemonSupervisor,
    TriggerEvent,
)

logger = logging.getLogger("rob317.demo_scalping_ws_daemon")

_STREAMS = ("aggTrade", "bookTicker", "kline_1m")


def _real_source_factory(symbols: list[str]) -> Callable[[], AsyncIterator[FuturesWsEvent]]:
    url = build_futures_stream_url(symbols, streams=_STREAMS)

    async def _factory() -> AsyncIterator[FuturesWsEvent]:
        async with FuturesMarketStream(url=url) as stream:
            async for ev in stream.events():
                yield ev

    return _factory


async def run_daemon(
    *,
    symbols: list[str],
    source_factory: Callable[[], AsyncIterator[FuturesWsEvent]] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    """Run the trigger pipeline; on_trigger only logs (slice 3, no mutation).

    Returns the number of triggers emitted. ``source_factory`` is injectable
    so tests never open a socket; the default connects to fstream.
    """
    factory = source_factory or _real_source_factory(symbols)
    sup = ScalpingDaemonSupervisor(
        symbols=symbols, **({"clock": clock} if clock else {})
    )
    count = 0

    async def on_trigger(trigger: TriggerEvent) -> None:
        nonlocal count
        count += 1
        logger.info(
            "TRIGGER (log-only, no mutation) %s",
            json.dumps(
                {
                    "symbol": trigger.symbol,
                    "side": trigger.side,
                    "reasons": list(trigger.decision.reason_codes),
                    "data_age_seconds": trigger.data_age_seconds,
                    "emitted_at": trigger.emitted_at.isoformat(),
                },
                sort_keys=True,
            ),
        )

    await sup.run_with_reconnect(factory, on_trigger=on_trigger)
    return count
```

Then replace the `pending_supervisor` early-return in `main` so the active path actually runs (keep the disabled path exactly as-is):

```python
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    gates = WsDaemonGates.from_env()
    summary = build_summary(gates)
    print(json.dumps(summary, sort_keys=True))
    if not gates.daemon_active:
        return 0
    symbols = sorted(DEFAULT_ALLOWLIST)
    logger.info(
        "WS daemon active (mutation_allowed=%s) — slice 3 logs triggers only, "
        "no broker mutation",
        gates.mutation_allowed,
    )
    asyncio.run(run_daemon(symbols=symbols))
    return 0
```

Update `build_summary`'s active-branch `status` from `"pending_supervisor"` to `"running"` and adjust the slice-2 test `test_summary_pending_supervisor_when_gates_on` to assert `"running"` (rename it to `test_summary_running_when_gates_on`). The `subscribed` key stays in the summary but is now informational; keep it `False` since the summary prints before the socket opens.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -v`
Expected: PASS (disabled-path tests + the new `run_daemon` test + the renamed running-status test).

- [ ] **Step 5: Commit**

```bash
git add scripts/binance_demo_scalping_ws_daemon.py \
        tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
git commit -m "$(cat <<'EOF'
feat(rob-317): CLI active path runs supervisor (log-only triggers)

Slice 3. daemon_active builds the fstream source + supervisor and runs
run_with_reconnect; on_trigger logs structured JSON only — no risk re-check,
no broker mutation (slice 4). Injectable source keeps tests network-free.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Slice-wide verification

**Files:** none (verification only).

- [ ] **Step 1: Run all demo_scalping_ws + CLI + guard tests**

Run:
```bash
uv run pytest \
  tests/services/brokers/binance/demo_scalping_ws/ \
  tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py \
  tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
```
Expected: PASS (all). The import-guard must stay green — `demo_scalping_ws/` imports only `ws_client`, `demo_scalping.signal`/`contract`, `host_allowlist`, none of the banned mutation layers.

- [ ] **Step 2: Lint changed surfaces**

Run:
```bash
uv run ruff check \
  app/services/brokers/binance/demo_scalping_ws/ \
  scripts/binance_demo_scalping_ws_daemon.py \
  tests/services/brokers/binance/demo_scalping_ws/ \
  tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
```
Expected: no errors. If ruff flags fixables, run `uv run ruff format` on the same paths, re-check, and amend the relevant commit.

- [ ] **Step 3: Confirm the CLI is still inert by default**

Run: `env -u BINANCE_DEMO_SCALPING_ENABLED -u BINANCE_DEMO_SCALPING_WS_ENABLED -u BINANCE_DEMO_SCALPING_WS_CONFIRM uv run python -m scripts.binance_demo_scalping_ws_daemon`
Expected: `{"base_enabled": false, "status": "disabled", "subscribed": false, "ws_enabled": false}`, exit 0, no socket opened.

- [ ] **Step 4: Targeted regression sweep on the binance broker surface**

Run: `uv run pytest tests/services/brokers/binance/ -q`
Expected: PASS — slice-3 additions plus existing ROB-285/298/307 broker tests unaffected (the shared `ws_client.py` was not modified).

---

## Self-Review

**Spec coverage (design §13 slice-3 scope + relevant §10 test-list items):**
- `market_stream.py`: fstream client + aggTrade parser + host guard (design §3.2, §4) → Task 1, Task 6 ✓
- Event → state update + freshness-from-last-event (design §5) → Task 2 ✓
- Event-driven trigger reusing `evaluate_signal` (design §3.2, §6.1) → Task 3, Task 4 ✓
- Supervisor asyncio loop + debounce + heartbeat-style structured logs (design §6) → Task 4, Task 7 ✓
- Stale-data guard blocks trigger (design §5; test-list "stale stream/data guard") → Task 4 ✓
- Reconnect/backoff (design §6.3; test-list "reconnect/backoff") → Task 5 ✓
- "websocket event → state update → signal trigger path" (test-list) → Task 4 ✓
- Read-only import boundary stays green (design §3.1) → Task 8 ✓

**Deferred to slice 4 (NOT slice-3 gaps):** risk-gate re-check + reason-code path, `confirm=false` blocks mutation, one-open-lifecycle/in-flight duplicate guard, concurrency semaphore, executor bridge. These need the ledger + executor and are explicitly slice 4 (design §13).

**Placeholder scan:** No "TBD"/"implement later". Every code step ships complete code. The `_raises_immediately` async generator in Task 5 keeps its unreachable `yield` (marked `# pragma: no cover`) — that is the idiomatic way to make a `raise`-only coroutine an async generator, not a placeholder.

**Type consistency:**
- `parse_futures_message(raw, *, now)` returns `FuturesWsEvent | None`; `AggTradeEvent` fields (`symbol`, `price`, `qty`, `trade_time`, `is_buyer_maker`) consistent across Tasks 1, 2, 6.
- `apply_event(state, event)` (freshness from the event's own timestamp) and `kline_to_candle(event)` signatures consistent across Tasks 2, 4.
- `EventDrivenSignal(product=, symbol=, config=, max_candles=)` + `.ingest_kline()` + `.candle_count` consistent across Tasks 3, 4.
- `ScalpingDaemonSupervisor(symbols=, product=, limits=, signal_config=, debounce_seconds=, clock=)`, `.run(source_factory, *, on_trigger, stop_after)`, `.run_with_reconnect(source_factory, *, on_trigger, sleep)`, and `TriggerEvent(product, symbol, side, decision, bid_price, ask_price, data_age_seconds, emitted_at)` consistent across Tasks 4, 5, 7.
- `run_daemon(*, symbols, source_factory, clock)` returns trigger count; matches its test (Task 7).
- `build_summary` status string changes from `"pending_supervisor"` (slice 2) to `"running"`, with the slice-2 test updated in the same task (Task 7 Step 3) — no stale assertion left behind.

---

## Next slice (separate plan)

- **Slice 4:** `demo_scalping_exec/ws_bridge.py` — `on_trigger` that (1) re-loads the live `LedgerSnapshot` via `ledger_state`, (2) builds `MarketConditions` (spread from `TriggerEvent.bid_price`/`ask_price`, data age, spot base qty), (3) calls `evaluate_risk`, (4) only if allowed AND `WS_CONFIRM` true, acquires the per-symbol lock + global semaphore and calls `DemoScalpingExecutor`, (5) records analytics. Tests: `confirm=false` blocks mutation, risk-gate block logs reason codes, one-open-lifecycle/in-flight duplicate guard, semaphore caps concurrency. Plus the dedicated `docs/runbooks/binance-demo-ws-scalping.md` entry and the final handoff checklist (design §14).
