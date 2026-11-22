"""Pure frame parsing for the Spot Demo stream collector.

Fixtures in ``tests/services/brokers/binance/spot_demo_streams/fixtures.py``
follow the documented shapes:

  * partial book depth ``<symbol>@depth<levels>`` — Binance Spot WebSocket
    Streams, https://developers.binance.com/en/docs/products/spot/web-socket-streams
  * ``<symbol>@bookTicker``, ``<symbol>@aggTrade`` — same page
  * user data events (``executionReport``, ``outboundAccountPosition``,
    ``balanceUpdate``) — https://developers.binance.com/en/docs/products/spot/user-data-stream
  * WebSocket API envelope (``id``/``status``/``result``, pushed
    ``{"subscriptionId", "event"}``) —
    https://developers.binance.com/en/docs/products/spot/web-socket-api

Money values stay exact decimal STRINGS (never float). Anything malformed
raises ``FrameError``; callers count and drop it.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Final

# Event types forwarded to ``fills:binance_spot_demo``. Everything else on the
# user stream (listStatus, externalLockUpdate, ...) is counted and ignored.
FORWARDED_USER_EVENTS: Final[frozenset[str]] = frozenset(
    {"executionReport", "outboundAccountPosition", "balanceUpdate"}
)


class FrameError(ValueError):
    """A frame did not match the documented shape."""


def _dec(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FrameError(f"{field}: not a decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise FrameError(f"{field}: not a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise FrameError(f"{field}: not a finite non-negative decimal")
    return str(value)


def _levels(raw: Any, field: str) -> list[list[str]]:
    if not isinstance(raw, list):
        raise FrameError(f"{field}: not a list")
    out: list[list[str]] = []
    for level in raw:
        if not isinstance(level, list) or len(level) < 2:
            raise FrameError(f"{field}: bad level")
        out.append([_dec(level[0], f"{field}.price"), _dec(level[1], f"{field}.qty")])
    return out


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrameError(f"{field}: not an int")
    return value


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise FrameError(f"{field}: not a non-empty string")
    return value


def decode_json(raw: str | bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise FrameError("not JSON") from exc
    if not isinstance(obj, dict):
        raise FrameError("not a JSON object")
    return obj


def symbol_from_stream(stream: str) -> tuple[str, str]:
    """``"btcusdt@depth20"`` -> ``("BTCUSDT", "depth20")``."""
    name, sep, channel = stream.partition("@")
    if not sep or not name or not channel:
        raise FrameError("bad stream name")
    return name.upper(), channel


def parse_market_frame(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Combined-stream envelope -> ``(symbol, record)``.

    ``record["kind"]`` is ``depth``, ``bookticker`` or ``trade``.
    """
    stream = _str(obj.get("stream"), "stream")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise FrameError("data: not an object")
    symbol, channel = symbol_from_stream(stream)
    if channel.startswith("depth"):
        return symbol, {
            "kind": "depth",
            "symbol": symbol,
            "last_update_id": _int(data.get("lastUpdateId"), "lastUpdateId"),
            "bids": _levels(data.get("bids"), "bids"),
            "asks": _levels(data.get("asks"), "asks"),
        }
    if channel == "bookTicker":
        if _str(data.get("s"), "s").upper() != symbol:
            raise FrameError("bookTicker symbol mismatch")
        return symbol, {
            "kind": "bookticker",
            "symbol": symbol,
            "update_id": _int(data.get("u"), "u"),
            "bid": _dec(data.get("b"), "b"),
            "bid_qty": _dec(data.get("B"), "B"),
            "ask": _dec(data.get("a"), "a"),
            "ask_qty": _dec(data.get("A"), "A"),
        }
    if channel == "aggTrade":
        if _str(data.get("s"), "s").upper() != symbol:
            raise FrameError("aggTrade symbol mismatch")
        return symbol, {
            "kind": "trade",
            "symbol": symbol,
            "agg_trade_id": _int(data.get("a"), "a"),
            "price": _dec(data.get("p"), "p"),
            "qty": _dec(data.get("q"), "q"),
            "trade_time_ms": _int(data.get("T"), "T"),
            "buyer_is_maker": bool(data.get("m")),
        }
    raise FrameError(f"unsupported channel {channel!r}")


def parse_user_push(obj: dict[str, Any]) -> dict[str, Any] | None:
    """WS-API pushed user event ``{"subscriptionId", "event": {...}}``.

    Returns a normalized record for forwarded event types, ``None`` for other
    well-formed events (ignored), raises ``FrameError`` for malformed ones.
    """
    event = obj.get("event")
    if not isinstance(event, dict):
        raise FrameError("event: not an object")
    etype = _str(event.get("e"), "e")
    if etype not in FORWARDED_USER_EVENTS:
        return None
    event_time = _int(event.get("E"), "E")
    if etype == "executionReport":
        record: dict[str, Any] = {
            "kind": "execution_report",
            "symbol": _str(event.get("s"), "s"),
            "event_time_ms": event_time,
            "client_order_id": _str(event.get("c"), "c"),
            "side": _str(event.get("S"), "S"),
            "order_type": _str(event.get("o"), "o"),
            "execution_type": _str(event.get("x"), "x"),
            "order_status": _str(event.get("X"), "X"),
            "order_id": _int(event.get("i"), "i"),
            "order_qty": _dec(event.get("q"), "q"),
            "order_price": _dec(event.get("p"), "p"),
            "last_qty": _dec(event.get("l"), "l"),
            "cumulative_qty": _dec(event.get("z"), "z"),
            "last_price": _dec(event.get("L"), "L"),
            "trade_id": _int(event.get("t"), "t"),
            "transaction_time_ms": _int(event.get("T"), "T"),
        }
        return record
    if etype == "outboundAccountPosition":
        balances = event.get("B")
        if not isinstance(balances, list):
            raise FrameError("B: not a list")
        return {
            "kind": "account_position",
            "event_time_ms": event_time,
            "balances": [
                {
                    "asset": _str(b.get("a") if isinstance(b, dict) else None, "a"),
                    "free": _dec(b.get("f") if isinstance(b, dict) else None, "f"),
                    "locked": _dec(b.get("l") if isinstance(b, dict) else None, "l"),
                }
                for b in balances
            ],
        }
    return {
        "kind": "balance_update",
        "event_time_ms": event_time,
        "asset": _str(event.get("a"), "a"),
        "delta": str(event.get("d")) if event.get("d") is not None else "",
    }
