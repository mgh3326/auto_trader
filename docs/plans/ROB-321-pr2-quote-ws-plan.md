# ROB-321 PR2 — Live KIS quote/orderbook WebSocket (read-only) Implementation Plan

> Sibling of `docs/plans/ROB-321-kis-mock-scalping-loop-plan.md`. Executes that plan's PR2. Steps use `- [ ]` checkboxes.

**Goal:** A read-only KIS quote/orderbook WebSocket market-data feed for the mock scalping loop — tick (체결가) and orderbook (호가) frames parsed into typed snapshots and tracked per symbol. **Zero order surface** (the WS client has no order method; orders are a separate REST/mock transport handled by PR1/PR4).

**Architecture:** New package `app/services/brokers/kis/mock_scalping_ws/`. A pure parser layer (`quote_protocol.py` + `quote_parsers.py`) converts plaintext KIS real-time frames into `QuoteTick`/`OrderBookSnapshot`. A per-symbol `MarketState` tracks bid/ask/last + freshness. A read-only `KISQuoteWebSocket` client reuses the reconnect/backoff/heartbeat shape of `KISExecutionWebSocket` but subscribes to quote TRs only and asserts a host allowlist. The order safety boundary is structural: this package contains no order code, and an AST import guard forbids it from importing any execution/order/ledger module.

**Tech Stack:** Python 3.13, asyncio, `websockets`, pytest. Frame format: `0|TR_ID|count|f0^f1^...` — leading `0` = plaintext (quotes are never AES-encrypted; a leading `1` is rejected).

---

## Key facts established from the existing code (`app/services/kis_websocket_internal/`)

- WS host is `ops.koreainvestment.com`, port `21000` (live) / `31000` (mock). Same domain, port differs. (`client.py:233-239`)
- Existing `KISExecutionWebSocket` is **execution-only** (TRs `H0STCNI0/9`, `H0GSCNI0/9`), AES-encrypted payloads. We do **not** modify it.
- Frame envelope split on `|`; payload fields split on `^`; leading token `0`=plain / `1`=encrypted. (`parsers.py:76-106`)
- Approval-key handshake + subscription request shape (`tr_type:"1"`, `custtype:"P"`) reusable. (`client.py:288-305`)
- Orders never traverse WS — they go via REST (`kis_mock_base_url`). So a quote WS client is read-only by construction.

**OPEN QUESTION (resolved in Task 4, the smoke task — not Task 1-3):** does the KIS **mock** WS (`:31000`) serve real-time quotes, or must quotes come from the **live** WS (`:21000`)? Design accommodates both via a resolved host constant + allowlist; the smoke script confirms empirically and the runbook records the answer. Task 1 (pure parser) is host-independent and proceeds regardless.

---

## File Structure

- Create: `app/services/brokers/kis/mock_scalping_ws/__init__.py`
- Create: `app/services/brokers/kis/mock_scalping_ws/quote_protocol.py` — quote TR codes + field-index maps.
- Create: `app/services/brokers/kis/mock_scalping_ws/quote_parsers.py` — `QuoteTick`, `OrderBookSnapshot`, `parse_quote_frame()`.
- Create: `app/services/brokers/kis/mock_scalping_ws/state.py` — `MarketState`.
- Create: `app/services/brokers/kis/mock_scalping_ws/market_stream.py` — read-only `KISQuoteWebSocket` + host allowlist.
- Modify: `app/core/config.py` — add `kis_mock_scalping_ws_enabled: bool = False`.
- Create: `scripts/kis_mock_scalping_ws_smoke.py` — read-only tick smoke (no orders), default-disabled.
- Create tests: `tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py`, `test_state.py`, `test_market_stream.py`, `test_import_guard.py`.

---

## Task 1: Quote protocol + pure parser  *(detailed — implement now)*

**Files:**
- Create: `app/services/brokers/kis/mock_scalping_ws/__init__.py`
- Create: `app/services/brokers/kis/mock_scalping_ws/quote_protocol.py`
- Create: `app/services/brokers/kis/mock_scalping_ws/quote_parsers.py`
- Test: `tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py`

> **Field indices** below follow KIS real-time TR documentation (`H0STCNT0` 주식체결, `H0STASP0` 주식호가). They are encoded as named maps so the empirical check in Task 4 (smoke against a real frame) can adjust a single map if KIS's live layout differs. Tests build synthetic frames from these same maps, proving the parser reads the configured indices.

- [ ] **Step 1: Write failing parser tests**

Create `tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py`:

```python
"""Pure KIS quote-frame parser tests (ROB-321 PR2 Task 1)."""

from __future__ import annotations

import pytest

from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
    parse_quote_frame,
)
from app.services.brokers.kis.mock_scalping_ws.quote_protocol import (
    DOMESTIC_ORDERBOOK_TR,
    DOMESTIC_TRADE_TR,
)


def _trade_frame(symbol: str = "005930", time: str = "131502", price: str = "70500") -> str:
    # H0STCNT0: idx0 symbol, idx1 time(HHMMSS), idx2 last price. Pad to 20 fields.
    fields = [""] * 20
    fields[0] = symbol
    fields[1] = time
    fields[2] = price
    return f"0|{DOMESTIC_TRADE_TR}|001|" + "^".join(fields)


def _orderbook_frame(
    symbol: str = "005930", ask1: str = "70600", bid1: str = "70500",
    ask_qty1: str = "120", bid_qty1: str = "200",
) -> str:
    # H0STASP0: idx0 symbol, idx3 ASKP1, idx13 BIDP1, idx23 ASKP_RSQN1, idx33 BIDP_RSQN1.
    fields = [""] * 60
    fields[0] = symbol
    fields[3] = ask1
    fields[13] = bid1
    fields[23] = ask_qty1
    fields[33] = bid_qty1
    return f"0|{DOMESTIC_ORDERBOOK_TR}|001|" + "^".join(fields)


@pytest.mark.unit
def test_parse_trade_frame() -> None:
    result = parse_quote_frame(_trade_frame(price="70500"))
    assert isinstance(result, QuoteTick)
    assert result.symbol == "005930"
    assert result.last_price == 70500.0
    assert result.ts == "131502"


@pytest.mark.unit
def test_parse_orderbook_frame() -> None:
    result = parse_quote_frame(_orderbook_frame())
    assert isinstance(result, OrderBookSnapshot)
    assert result.symbol == "005930"
    assert result.ask == 70600.0
    assert result.bid == 70500.0
    assert result.ask_qty == 120.0
    assert result.bid_qty == 200.0


@pytest.mark.unit
def test_encrypted_frame_rejected() -> None:
    # Leading "1" = encrypted; quotes are never encrypted -> reject (return None).
    enc = _trade_frame().replace("0|", "1|", 1)
    assert parse_quote_frame(enc) is None


@pytest.mark.unit
def test_unknown_tr_returns_none() -> None:
    assert parse_quote_frame("0|H0STCNI0|001|005930^x^y") is None


@pytest.mark.unit
def test_malformed_frame_returns_none() -> None:
    assert parse_quote_frame("garbage") is None
    assert parse_quote_frame("") is None
    assert parse_quote_frame("0|H0STCNT0") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `quote_protocol.py`**

```python
"""KIS real-time quote/orderbook TR codes and field-index maps (read-only).

Indices follow KIS real-time TR docs (H0STCNT0 주식체결, H0STASP0 주식호가).
Encoded as named maps so a single map can be corrected if the smoke test
(PR2 Task 4) observes a different live layout.
"""

from __future__ import annotations

DOMESTIC_TRADE_TR = "H0STCNT0"  # 실시간 주식 체결가
DOMESTIC_ORDERBOOK_TR = "H0STASP0"  # 실시간 주식 호가

QUOTE_TR_CODES = frozenset({DOMESTIC_TRADE_TR, DOMESTIC_ORDERBOOK_TR})

# H0STCNT0 field indices
TRADE_FIELDS = {
    "symbol": 0,
    "time": 1,  # HHMMSS
    "last_price": 2,
}

# H0STASP0 field indices (best level only)
ORDERBOOK_FIELDS = {
    "symbol": 0,
    "ask": 3,  # ASKP1
    "bid": 13,  # BIDP1
    "ask_qty": 23,  # ASKP_RSQN1
    "bid_qty": 33,  # BIDP_RSQN1
}
```

- [ ] **Step 4: Implement `quote_parsers.py`**

```python
"""Pure parser: KIS plaintext quote frame -> typed snapshot. No I/O, no state."""

from __future__ import annotations

from dataclasses import dataclass

from .quote_protocol import (
    DOMESTIC_ORDERBOOK_TR,
    DOMESTIC_TRADE_TR,
    ORDERBOOK_FIELDS,
    QUOTE_TR_CODES,
    TRADE_FIELDS,
)


@dataclass(frozen=True)
class QuoteTick:
    symbol: str
    last_price: float
    ts: str  # HHMMSS as reported by KIS


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_quote_frame(message: str | bytes) -> QuoteTick | OrderBookSnapshot | None:
    """Parse one plaintext KIS real-time quote frame.

    Returns None for: bytes-decode failure, empty/malformed frames, encrypted
    frames (leading '1'), or non-quote TR codes. Read-only; never raises on
    bad input.
    """
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except Exception:
            return None
    message = (message or "").strip()
    if not message:
        return None

    parts = message.split("|")
    if len(parts) < 4:
        return None

    encryption_flag, tr_code, _count, payload = parts[0], parts[1], parts[2], parts[3]
    if encryption_flag != "0":  # quotes are never encrypted
        return None
    if tr_code not in QUOTE_TR_CODES:
        return None

    fields = payload.split("^")

    def at(idx: int) -> str:
        return fields[idx] if idx < len(fields) else ""

    if tr_code == DOMESTIC_TRADE_TR:
        symbol = at(TRADE_FIELDS["symbol"])
        if not symbol:
            return None
        return QuoteTick(
            symbol=symbol,
            last_price=_to_float(at(TRADE_FIELDS["last_price"])),
            ts=at(TRADE_FIELDS["time"]),
        )

    if tr_code == DOMESTIC_ORDERBOOK_TR:
        symbol = at(ORDERBOOK_FIELDS["symbol"])
        if not symbol:
            return None
        return OrderBookSnapshot(
            symbol=symbol,
            bid=_to_float(at(ORDERBOOK_FIELDS["bid"])),
            ask=_to_float(at(ORDERBOOK_FIELDS["ask"])),
            bid_qty=_to_float(at(ORDERBOOK_FIELDS["bid_qty"])),
            ask_qty=_to_float(at(ORDERBOOK_FIELDS["ask_qty"])),
        )

    return None
```

Also create `app/services/brokers/kis/mock_scalping_ws/__init__.py` (empty).

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check app/ tests/ && uv run ruff format app/ tests/ && uv run ty check app/ --error-on-warning
git add app/services/brokers/kis/mock_scalping_ws/ tests/brokers/kis/mock_scalping_ws/test_quote_parsers.py
git commit -m "feat(rob-321): KIS quote-frame pure parser (read-only, PR2 task 1)"
```

---

## Task 2: `MarketState` per-symbol tracker  *(outline — detail before implementing)*

`state.py`: `MarketState(symbol)` with `update_from_tick(QuoteTick, now)`, `update_from_book(OrderBookSnapshot, now)`, `spread_bps() -> float | None`, `age_seconds(now) -> float`, `last_price`/`bid`/`ask` accessors. Pure (clock injected). Tests: tick updates last_price+ts; book updates bid/ask+spread_bps; age increases with `now`; spread None until a book seen.

## Task 2b: Account-mode aware WS approval key + two-layer host allowlist  *(DONE)*

Done ahead of the client (Task 3) so the quote client + mock execution WS both issue the *correct* approval key. Extended `kis_websocket_internal/approval_keys.py` (no new client):

- `get_approval_key/_issue_approval_key/_cache_approval_key/_get_cached_approval_key` take `account_mode="kis_live" | "kis_mock"` (default `kis_live` → backward compatible).
- live: `settings.kis_base_url` + `kis_app_key`/`kis_app_secret`, cache `kis:websocket:approval_key`.
- mock: `settings.kis_mock_base_url` + `kis_mock_app_key`/`kis_mock_app_secret`, cache `kis_mock:websocket:approval_key`; missing config → fail-closed via `validate_kis_mock_config()` (env var names only, never values).
- **Two-layer fail-closed host allowlists** in `kis_websocket_internal/constants.py`:
  1. `APPROVAL_ENDPOINT_HOSTS` — live `openapi.koreainvestment.com:9443`, mock `openapivts.koreainvestment.com:29443`
  2. `WEBSOCKET_ENDPOINT_HOSTS` — live `ops.koreainvestment.com:21000`, mock `ops.koreainvestment.com:31000`
- `KISExecutionWebSocket._issue_approval_key_if_needed()` + recoverable-ACK reissue pass `self.account_mode`; `_build_websocket_url` asserts the WS allowlist.
- Result: **mock execution WS now auto-issues a MOCK approval key** (previously always live — latent bug). Live order/mutation paths untouched. Tests in `tests/services/kis_websocket/test_approval_keys.py` (+ updated `test_client.py`): live/mock endpoint+creds, cache namespace, fail-closed, host-allowlist rejection, reissue path. 88 green.

## Task 3: `KISQuoteWebSocket` read-only client + host allowlist  *(DONE)*

`market_stream.py`: `get_approval_key(account_mode)` (Task 2b) → connect → subscribe `_SUBSCRIBE_TRS` (H0STCNT0/H0STASP0) per symbol → `listen()` dispatches `parse_quote_frame` results to `on_tick`/`on_book`. Bounded reconnect, pingpong echo. **No order method** (`test_has_no_order_surface`). `_build_url` built from + asserted against `WEBSOCKET_ENDPOINT_HOSTS[account_mode]` (fail-closed). 10 tests: unknown-mode reject, live/mock URL, sub-request shape, dispatch routing (tick/book/junk), fake-WS listen, pingpong echo, mock approval-key wiring.

## Task 4: Read-only smoke script + import guard + flag  *(DONE)*

`app/core/config.py` flag `kis_mock_scalping_ws_enabled` (default False, in the `kis_ws` block). `scripts/kis_mock_scalping_ws_smoke.py`: default-disabled (flag-off → no-op exit 0), bounded ticks/books print, no orders; exit 4 = no events on that host. **Resolves the OPEN QUESTION** via `--account-mode kis_mock|kis_live` (runbook records which serves quotes). `tests/brokers/kis/mock_scalping_ws/test_import_guard.py`: AST-asserts the package imports no order/ledger/execution-mutation module. `docs/runbooks/kis-mock-scalping-ws-smoke.md`. `test_smoke_cli.py`: default-disabled no-op + default account_mode.

**PR2 status: implementation complete.** 104 tests green (incl. live-unaffected regressions), ruff check/format + ty clean. Branch `rob-321-pr2` (4 feature commits + docs); not yet pushed. Field indices + mock-vs-live host remain to be confirmed by the operator smoke (runbook table).

---

## Self-Review

- **Spec coverage (master plan PR2):** quote/orderbook streaming (Task 1 parser + Task 3 client), reconnect/backoff/heartbeat (Task 3), host separation fail-closed (Task 3 allowlist + structural read-only), live/mock tagging (carried on parsed frames + client account_mode). ✅
- **Placeholder scan:** Task 1 is complete code; Tasks 2-4 are explicitly outlines to be detailed before implementing (not in-task placeholders).
- **Type consistency:** `QuoteTick(symbol, last_price, ts)`, `OrderBookSnapshot(symbol, bid, ask, bid_qty, ask_qty)`, `parse_quote_frame()` names match across protocol/parser/tests.
- **Open verification item:** field indices for `H0STCNT0`/`H0STASP0` are documented-but-unconfirmed-against-live; Task 4 smoke validates against a real frame and adjusts the single field map if needed.
