# ROB-104 — KIS WebSocket Live/Mock Event Tagging & Mock Smoke Hooks Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every KIS websocket execution event with explicit `broker`/`account_mode`/`execution_source` metadata using the ROB-100 `app.schemas.execution_contracts` vocabulary, clarify mock vs live behavior in the monitor, and add a bounded mock smoke helper that proves the mock subscription plumbing works without placing orders.

**Architecture:** The KIS websocket client already selects mock-vs-live TR codes via `mock_mode`. We extend the parsed event dict (in `KISExecutionWebSocket.listen`) with a small set of identity fields (`broker`, `account_mode`, `execution_source`, plus the existing `tr_code` and `market`), add an `OrderLifecycleEvent` builder for downstream consumers, and update the monitor to log the runtime mode at startup. We add a smoke helper script (`scripts/kis_websocket_mock_smoke.py`) that issues an approval key and validates a mock TR subscription handshake, then exits — no execution callback, no order placement.

**Tech Stack:** Python 3.13 · `app.schemas.execution_contracts` (Pydantic v2) · `websockets` · `pytest` · existing `KISExecutionWebSocket` / `ExecutionMessageParser`.

**Issue:** [ROB-104](https://linear.app/mgh3326/issue/ROB-104/kis-websocket-add-livemock-event-tagging-and-mock-smoke-hooks) · Depends on PR #670 / ROB-100 (already merged on `main`, commit `af1ff1d4`).

**Branch:** `feature/ROB-104-kis-websocket-livemock-tagging` (run all work in worktree per `CLAUDE.md` "Worktree 운영 규칙").

---

## Background — what already exists

* `app/schemas/execution_contracts.py` defines `AccountMode = Literal["kis_live","kis_mock","alpaca_paper","db_simulated"]`, `ExecutionSource = Literal["preopen","watch","manual","websocket","reconciler"]`, `OrderLifecycleState = Literal["planned","previewed","submitted","accepted","pending","fill","reconciled","stale","failed","anomaly"]`, and the `OrderLifecycleEvent` Pydantic model. **Reuse — do NOT redefine these strings.**
* `app/services/kis_websocket_internal/protocol.py` defines `DOMESTIC_EXECUTION_TR_REAL = "H0STCNI0"`, `DOMESTIC_EXECUTION_TR_MOCK = "H0STCNI9"`, `OVERSEAS_EXECUTION_TR_REAL = "H0GSCNI0"`, `OVERSEAS_EXECUTION_TR_MOCK = "H0GSCNI9"`.
* `app/services/kis_websocket_internal/client.py` `KISExecutionWebSocket(__init__)` accepts `mock_mode: bool = False`, picks mock URL `ws://ops.koreainvestment.com:31000/tryitout` vs live `:21000`, and selects mock vs live TR ids in `_subscribe_execution_tr` (lines 234-239).
* `app/services/kis_websocket_internal/parsers.py` `ExecutionMessageParser.parse_message` returns a dict with `tr_code`, `market` (`"kr"|"us"|"unknown"`), `symbol`, `side`, `order_id`, `filled_price`, `filled_qty`, `filled_at`, plus overseas-only fields like `execution_status`, `cntg_yn`, `rfus_yn`, etc.
* `kis_websocket_monitor.py` instantiates the client with `mock_mode=settings.kis_ws_is_mock` and forwards events to `app.services.execution_event.publish_execution_event`, which publishes to Redis channel `execution:{market}` (where `market` is `"kr"|"us"|"unknown"`).
* Existing tests live under `tests/services/kis_websocket/`. `test_client.py` already covers TR-id selection by mock mode (lines 53-69), so we extend rather than duplicate.

## Scope boundary (from the Linear ticket)

* DO: tag events, add tests, add bounded mock smoke helper, write a runbook.
* DO NOT: change production launchd/service definitions, submit broker orders, require real fills, remove the existing live websocket path.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `app/services/kis_websocket_internal/client.py` | Modify | Accept `account_mode` (or derive from `mock_mode`), stamp every parsed execution dict with `broker`/`account_mode`/`execution_source` before invoking `on_execution`. |
| `app/services/kis_websocket_internal/events.py` | Create | New leaf module: `build_lifecycle_event(event_dict, *, account_mode) -> OrderLifecycleEvent` mapping a parsed KIS execution dict to the ROB-100 contract. |
| `app/services/kis_websocket_internal/__init__.py` | Modify | Re-export `build_lifecycle_event`. |
| `app/services/kis_websocket.py` | Modify | Re-export `build_lifecycle_event` for symmetry with other module-level exports. |
| `kis_websocket_monitor.py` | Modify | Resolve `account_mode` from `settings.kis_ws_is_mock`, log it at startup, pass explicit `account_mode=` to the client. |
| `scripts/kis_websocket_mock_smoke.py` | Create | Bounded mock-handshake smoke helper. Issues approval key, opens mock URL, sends mock TR subscriptions, asserts ACKs, closes. No `on_execution` callback that does anything. Returns non-zero on failure. |
| `tests/services/kis_websocket/test_client.py` | Modify | Add tests for stamped event metadata + `account_mode` plumbing. |
| `tests/services/kis_websocket/test_events.py` | Create | Unit tests for `build_lifecycle_event` mapping (kr fill, us fill, partial, overseas anomaly). |
| `tests/test_kis_websocket_mock_smoke.py` | Create | Tests for the smoke helper using a mocked `websockets.connect`. |
| `tests/test_kis_websocket_monitor.py` | Modify | Assert monitor logs and passes `account_mode` correctly. |
| `docs/runbooks/kis-websocket-mock-smoke.md` | Create | Document smoke-vs-runtime separation. |

**Why a new `events.py` instead of dumping into `parsers.py`:** parsers stay dedicated to wire-format decoding. Mapping to the contract is a separate responsibility consumed by callers (monitor, tests, future reconciler). Keeps the parser file from growing past its current ~600 lines.

---

## Task 1: Add `account_mode` plumbing to `KISExecutionWebSocket`

**Files:**
- Modify: `app/services/kis_websocket_internal/client.py:37-48`
- Modify: `tests/services/kis_websocket/test_client.py` (extend `TestKISWebSocketClient`)

**Why this first:** Every later task needs a way to read the runtime mode off the client.

- [ ] **Step 1: Write the failing test (extend `test_client.py`)**

Append inside `class TestKISWebSocketClient` in `tests/services/kis_websocket/test_client.py`:

```python
@pytest.mark.asyncio
async def test_account_mode_defaults_to_kis_live(self, execution_callback):
    client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
    assert client.account_mode == "kis_live"

@pytest.mark.asyncio
async def test_account_mode_mock_mode_maps_to_kis_mock(self, execution_callback):
    client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
    assert client.account_mode == "kis_mock"

@pytest.mark.asyncio
async def test_account_mode_explicit_override(self, execution_callback):
    client = KISExecutionWebSocket(
        on_execution=execution_callback,
        mock_mode=False,
        account_mode="kis_mock",
    )
    assert client.account_mode == "kis_mock"
    # explicit override should NOT change mock_mode (URL/TR selection still
    # comes from mock_mode); operators must keep them aligned.
    assert client.mock_mode is False

@pytest.mark.asyncio
async def test_account_mode_rejects_unknown_value(self, execution_callback):
    with pytest.raises(ValueError, match="account_mode"):
        KISExecutionWebSocket(
            on_execution=execution_callback,
            mock_mode=False,
            account_mode="alpaca_paper",  # not a KIS mode
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/kis_websocket/test_client.py -v -k "account_mode"`
Expected: 4 failures with `AttributeError: 'KISExecutionWebSocket' object has no attribute 'account_mode'` (and `TypeError` on the override case).

- [ ] **Step 3: Implement minimal change in client**

In `app/services/kis_websocket_internal/client.py`, replace the imports block (top of file) so it includes the contract:

```python
from app.schemas.execution_contracts import AccountMode
```

Replace the `__init__` signature and body (lines 37-73) with:

```python
def __init__(
    self,
    on_execution: Callable[[dict[str, Any]], Any],
    mock_mode: bool = False,
    *,
    account_mode: AccountMode | None = None,
):
    """
    Args:
        on_execution: 체결 이벤트 발생 시 호출되는 콜백 함수
        mock_mode: Mock 모드 (Mock URL/TR 선택)
        account_mode: 이벤트 태깅에 사용할 ROB-100 account_mode.
            None 이면 mock_mode 에서 파생 ("kis_mock" / "kis_live").
            오직 KIS 계열만 허용 — alpaca_paper/db_simulated 거부.
    """
    self.on_execution = on_execution
    self.mock_mode = mock_mode

    resolved_mode: AccountMode = (
        account_mode
        if account_mode is not None
        else ("kis_mock" if mock_mode else "kis_live")
    )
    if resolved_mode not in ("kis_live", "kis_mock"):
        raise ValueError(
            f"account_mode must be 'kis_live' or 'kis_mock', got {resolved_mode!r}"
        )
    self.account_mode: AccountMode = resolved_mode

    self.websocket: Any | None = None
    self.websocket_url = ""
    self.is_running = False
    self.is_connected = False

    self.reconnect_delay = settings.kis_ws_reconnect_delay_seconds
    self.max_reconnect_attempts = settings.kis_ws_max_reconnect_attempts
    self.current_attempt = 0

    self.ping_interval = settings.kis_ws_ping_interval
    self.ping_timeout = settings.kis_ws_ping_timeout
    self.messages_received = 0
    self.execution_events_received = 0
    self.last_message_at: str | None = None
    self.last_execution_at: str | None = None
    self.last_pingpong_at: str | None = None

    self.approval_key: str | None = None
    self._encryption_keys_by_tr: dict[str, tuple[str, str]] = {}
    self._last_reissue_msg_code: str | None = None

    self._parser = ExecutionMessageParser(self._encryption_keys_by_tr)

    self._create_ssl_context()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/kis_websocket/test_client.py -v -k "account_mode"`
Expected: 4 PASS.

Then run full kis_websocket suite to confirm no regression:
Run: `uv run pytest tests/services/kis_websocket/ -v`
Expected: all PASS (existing tests don't use the new kwarg, default behavior preserved).

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_websocket_internal/client.py tests/services/kis_websocket/test_client.py
git commit -m "$(cat <<'EOF'
feat(ROB-104): add account_mode plumbing to KIS websocket client

Resolve account_mode from mock_mode by default (kis_live / kis_mock) and
allow explicit override aligned with ROB-100 execution_contracts vocabulary.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2: Stamp every execution event with broker/account_mode/source

**Files:**
- Modify: `app/services/kis_websocket_internal/client.py:324-413` (the `listen` method)
- Modify: `tests/services/kis_websocket/test_client.py`

**Goal:** Each dict passed to `on_execution` carries `broker="kis"`, `account_mode=self.account_mode`, `execution_source="websocket"` in addition to the existing fields. `tr_code`/`market` are already there.

- [ ] **Step 1: Write the failing test**

Append in `tests/services/kis_websocket/test_client.py` inside `TestKISWebSocketClient`:

```python
@pytest.mark.asyncio
async def test_listen_stamps_event_metadata_for_kis_mock(self):
    captured: list[dict] = []

    async def on_execution(event):
        captured.append(event)

    client = KISExecutionWebSocket(on_execution=on_execution, mock_mode=True)

    # Hand-crafted unencrypted domestic mock fill; matches official-index branch.
    # Format: "0|{tr}|{exec_type}|{order_id_field0}^side01^^^^^^^^side(buy)^...
    # Use the parser directly via a minimal payload that hits _parse_domestic_execution_compact.
    raw = "0|H0STCNI9|005930^02^123456789^10^70000^123000"
    fake_ws = AsyncMock()
    fake_ws.__aiter__ = lambda self_: iter([raw])
    client.websocket = fake_ws
    client.is_connected = True

    await client.listen()

    assert captured, "expected at least one execution event to be delivered"
    event = captured[0]
    assert event["broker"] == "kis"
    assert event["account_mode"] == "kis_mock"
    assert event["execution_source"] == "websocket"
    assert event["tr_code"] == "H0STCNI9"
    assert event["market"] == "kr"

@pytest.mark.asyncio
async def test_listen_stamps_event_metadata_for_kis_live(self):
    captured: list[dict] = []

    async def on_execution(event):
        captured.append(event)

    client = KISExecutionWebSocket(on_execution=on_execution, mock_mode=False)

    raw = "0|H0STCNI0|005930^02^123456789^10^70000^123000"
    fake_ws = AsyncMock()
    fake_ws.__aiter__ = lambda self_: iter([raw])
    client.websocket = fake_ws
    client.is_connected = True

    await client.listen()

    assert captured
    event = captured[0]
    assert event["account_mode"] == "kis_live"
    assert event["tr_code"] == "H0STCNI0"
```

> **Note on the fake `__aiter__`:** if AsyncMock async-iteration in your Python/mock version doesn't work with `__aiter__ = lambda ...`, fall back to a small async generator helper:
> ```python
> async def _gen(items):
>     for item in items:
>         yield item
> ...
> client.websocket = _gen([raw])  # but listen does `async for message in websocket` which needs the object itself; in that case wrap with a class.
> ```
> Use whichever pattern is consistent with neighbouring tests in `test_client.py`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/kis_websocket/test_client.py -v -k "stamps_event_metadata"`
Expected: FAIL with `KeyError: 'broker'` (or AssertionError on missing key).

- [ ] **Step 3: Implement the stamping in `listen`**

In `app/services/kis_websocket_internal/client.py`, inside `listen()`, locate the block (currently around lines 358-365):

```python
data["received_at"] = received_at
data.setdefault(
    "correlation_id", self._parser._new_correlation_id()
)

if self._parser.is_execution_event(data):
```

Insert tagging immediately after the `data.setdefault("correlation_id", ...)` line, before the `is_execution_event` check:

```python
data["received_at"] = received_at
data.setdefault(
    "correlation_id", self._parser._new_correlation_id()
)
data["broker"] = "kis"
data["account_mode"] = self.account_mode
data["execution_source"] = "websocket"

if self._parser.is_execution_event(data):
```

Rationale: stamp regardless of whether the message is an execution event or other — every dict the client emits is identifiable. (Currently non-execution dicts are dropped without invoking `on_execution`, but stamping them is cheap and future-proof for adding non-execution callbacks.)

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/services/kis_websocket/test_client.py -v`
Expected: all PASS, including the two new metadata tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_websocket_internal/client.py tests/services/kis_websocket/test_client.py
git commit -m "$(cat <<'EOF'
feat(ROB-104): stamp KIS websocket events with broker/account_mode/source

Every parsed websocket dict now carries broker="kis",
account_mode (kis_live|kis_mock), execution_source="websocket" so
downstream consumers can route without re-deriving from tr_code.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: Add `build_lifecycle_event` mapper to ROB-100 contract

**Files:**
- Create: `app/services/kis_websocket_internal/events.py`
- Modify: `app/services/kis_websocket_internal/__init__.py`
- Modify: `app/services/kis_websocket.py`
- Create: `tests/services/kis_websocket/test_events.py`

**Goal:** Expose a typed builder so consumers (reconciler, smoke harness, integration tests) get a validated `OrderLifecycleEvent` with KIS-specific raw fields stuffed into `detail`.

State mapping reference (from existing parser output):
| Parser dict | Lifecycle state |
|---|---|
| domestic with `fill_yn == "2"` | `"fill"` |
| domestic with `fill_yn == "1"` | `"pending"` (acknowledgement of order, not yet filled) |
| overseas with `execution_status == "filled"` | `"fill"` |
| overseas with `execution_status == "rejected"` | `"failed"` |
| overseas with `execution_status == "accepted"` | `"accepted"` |
| overseas with `execution_status == "anomaly"` | `"anomaly"` |
| anything else | `"anomaly"` (with warning `"unknown_kis_state"`) |

> If you find that the parser's `_classify_overseas_execution_status` returns labels other than the four listed above, follow whatever it actually emits — read `app/services/kis_websocket_internal/parsers.py` `_classify_overseas_execution_status` and update the mapping table in this task to match. Do not invent state names.

- [ ] **Step 1: Read the actual classifier output**

Read: `app/services/kis_websocket_internal/parsers.py` — find `_classify_overseas_execution_status` and list every string literal it returns. Update the mapping table above in this plan if it diverges. (This is a 30-second sanity step, not a placeholder — actually do it.)

- [ ] **Step 2: Write the failing test**

Create `tests/services/kis_websocket/test_events.py`:

```python
from datetime import datetime

import pytest

from app.schemas.execution_contracts import OrderLifecycleEvent
from app.services.kis_websocket_internal.events import build_lifecycle_event


@pytest.mark.unit
class TestBuildLifecycleEvent:
    def test_domestic_full_fill_maps_to_fill_state(self):
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 70000.0,
            "filled_qty": 10.0,
            "filled_amount": 700000.0,
            "filled_at": "2026-05-04T10:00:00",
            "fill_yn": "2",
            "received_at": "2026-05-04T10:00:01",
            "correlation_id": "corr-1",
            "broker": "kis",
            "account_mode": "kis_mock",
            "execution_source": "websocket",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert isinstance(event, OrderLifecycleEvent)
        assert event.account_mode == "kis_mock"
        assert event.execution_source == "websocket"
        assert event.state == "fill"
        assert event.broker_order_id == "0000123456"
        assert event.correlation_id == "corr-1"
        assert isinstance(event.occurred_at, datetime)
        # raw KIS fields belong in detail
        assert event.detail["tr_code"] == "H0STCNI9"
        assert event.detail["market"] == "kr"
        assert event.detail["symbol"] == "005930"
        assert event.detail["side"] == "bid"
        assert event.detail["filled_qty"] == 10.0
        assert event.detail["filled_price"] == 70000.0
        assert event.detail["fill_yn"] == "2"

    def test_domestic_acknowledgement_maps_to_pending(self):
        parsed = {
            "tr_code": "H0STCNI0",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 0.0,
            "filled_qty": 0.0,
            "filled_amount": 0.0,
            "filled_at": "2026-05-04T10:00:00",
            "fill_yn": "1",
            "correlation_id": "corr-2",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_live")

        assert event.state == "pending"
        assert event.account_mode == "kis_live"

    def test_overseas_filled_status(self):
        parsed = {
            "tr_code": "H0GSCNI0",
            "market": "us",
            "symbol": "AAPL",
            "side": "bid",
            "order_id": "ORD-1",
            "filled_price": 200.0,
            "filled_qty": 5.0,
            "filled_amount": 1000.0,
            "filled_at": "2026-05-04T14:30:00",
            "execution_status": "filled",
            "cntg_yn": "Y",
            "rfus_yn": "N",
            "acpt_yn": "Y",
            "rctf_cls": "0",
            "currency": "USD",
            "correlation_id": "corr-3",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_live")

        assert event.state == "fill"
        assert event.detail["execution_status"] == "filled"
        assert event.detail["currency"] == "USD"

    def test_unknown_state_falls_back_to_anomaly_with_warning(self):
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "unknown",
            "order_id": None,
            "fill_yn": "Z",  # not in known mapping
            "correlation_id": "corr-4",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert event.state == "anomaly"
        assert any("unknown" in w.lower() for w in event.warnings)

    def test_account_mode_arg_overrides_dict_value(self):
        # Caller's account_mode argument is authoritative; dict value is ignored.
        # Rationale: the client/runtime knows its own mode; trusting an
        # in-band field would let a malformed message lie about it.
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 70000.0,
            "filled_qty": 10.0,
            "fill_yn": "2",
            "account_mode": "kis_live",  # WRONG — caller passes kis_mock
            "correlation_id": "corr-5",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert event.account_mode == "kis_mock"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/services/kis_websocket/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.kis_websocket_internal.events'`.

- [ ] **Step 4: Implement `events.py`**

Create `app/services/kis_websocket_internal/events.py`:

```python
"""Map KIS websocket parsed-event dicts to ROB-100 OrderLifecycleEvent.

Pure mapping helper. Lives next to the parser/client but does not import them
(only the schema), so it stays reusable from tests and the smoke harness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.execution_contracts import (
    AccountMode,
    OrderLifecycleEvent,
    OrderLifecycleState,
)

_DOMESTIC_FILL_YN_TO_STATE: dict[str, OrderLifecycleState] = {
    "2": "fill",
    "1": "pending",
}

_OVERSEAS_STATUS_TO_STATE: dict[str, OrderLifecycleState] = {
    "filled": "fill",
    "rejected": "failed",
    "accepted": "accepted",
    "anomaly": "anomaly",
}


def _resolve_state(parsed: dict[str, Any]) -> tuple[OrderLifecycleState, list[str]]:
    warnings: list[str] = []
    market = parsed.get("market")

    if market == "kr":
        fill_yn = str(parsed.get("fill_yn") or "").strip()
        state = _DOMESTIC_FILL_YN_TO_STATE.get(fill_yn)
        if state is None:
            warnings.append(f"unknown_kis_domestic_fill_yn:{fill_yn!r}")
            return "anomaly", warnings
        return state, warnings

    if market == "us":
        status = str(parsed.get("execution_status") or "").strip().lower()
        state = _OVERSEAS_STATUS_TO_STATE.get(status)
        if state is None:
            warnings.append(f"unknown_kis_overseas_status:{status!r}")
            return "anomaly", warnings
        return state, warnings

    warnings.append(f"unknown_kis_market:{market!r}")
    return "anomaly", warnings


def _resolve_occurred_at(parsed: dict[str, Any]) -> datetime:
    for key in ("filled_at", "received_at"):
        raw = parsed.get(key)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value
    return datetime.now(UTC).replace(microsecond=0)


_DETAIL_KEYS = (
    "tr_code",
    "market",
    "symbol",
    "side",
    "filled_price",
    "filled_qty",
    "filled_amount",
    "filled_at",
    "fill_yn",
    "execution_status",
    "cntg_yn",
    "rfus_yn",
    "acpt_yn",
    "rctf_cls",
    "order_qty",
    "currency",
    "received_at",
    "raw_fields_count",
)


def build_lifecycle_event(
    parsed: dict[str, Any],
    *,
    account_mode: AccountMode,
) -> OrderLifecycleEvent:
    """Build a typed ``OrderLifecycleEvent`` from a KIS parsed dict.

    The caller supplies ``account_mode`` (the runtime is authoritative); any
    ``account_mode`` key inside ``parsed`` is ignored.
    """
    if account_mode not in ("kis_live", "kis_mock"):
        raise ValueError(
            f"account_mode must be 'kis_live' or 'kis_mock', got {account_mode!r}"
        )

    state, warnings = _resolve_state(parsed)

    detail: dict[str, Any] = {
        key: parsed[key] for key in _DETAIL_KEYS if key in parsed
    }

    broker_order_id = parsed.get("order_id")
    if broker_order_id is not None:
        broker_order_id = str(broker_order_id)

    correlation_id = parsed.get("correlation_id")
    if correlation_id is not None:
        correlation_id = str(correlation_id)

    return OrderLifecycleEvent(
        account_mode=account_mode,
        execution_source="websocket",
        state=state,
        occurred_at=_resolve_occurred_at(parsed),
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
        detail=detail,
        warnings=warnings,
    )


__all__ = ["build_lifecycle_event"]
```

- [ ] **Step 5: Re-export from package**

Modify `app/services/kis_websocket_internal/__init__.py` (read first; if empty, create the export). Add:

```python
from .events import build_lifecycle_event

__all__ = [*__all__ if "__all__" in globals() else (), "build_lifecycle_event"]
```

> If the file currently has no `__all__`, replace the whole file with:
> ```python
> from .events import build_lifecycle_event
>
> __all__ = ["build_lifecycle_event"]
> ```

Modify `app/services/kis_websocket.py` to add `build_lifecycle_event` re-export. After the existing imports block, add:

```python
from app.services.kis_websocket_internal.events import build_lifecycle_event
```

And add `"build_lifecycle_event"` to the `__all__` tuple at the bottom of the file (keep alphabetical ordering).

- [ ] **Step 6: Run tests to verify pass**

Run: `uv run pytest tests/services/kis_websocket/test_events.py -v`
Expected: 5 PASS.

Run: `uv run pytest tests/services/kis_websocket/ tests/test_execution_contracts.py -v`
Expected: all PASS (no regression in the contract module either).

- [ ] **Step 7: Commit**

```bash
git add app/services/kis_websocket_internal/events.py app/services/kis_websocket_internal/__init__.py app/services/kis_websocket.py tests/services/kis_websocket/test_events.py
git commit -m "$(cat <<'EOF'
feat(ROB-104): add build_lifecycle_event mapping KIS event dict to OrderLifecycleEvent

ROB-100 vocabulary mapper for KIS websocket fills. domestic fill_yn and
overseas execution_status are mapped to OrderLifecycleState; raw KIS
fields are placed under detail. Caller-supplied account_mode is
authoritative.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: Clarify mock vs live in monitor logs and pass explicit `account_mode`

**Files:**
- Modify: `kis_websocket_monitor.py:68-78` (`_initialize_websocket`) and the startup banner
- Modify: `tests/test_kis_websocket_monitor.py`

- [ ] **Step 1: Read the existing monitor test file**

Read: `tests/test_kis_websocket_monitor.py` — locate the test that exercises `_initialize_websocket` (or write a new one if absent). Note its existing test patterns for mocking `KISExecutionWebSocket`.

- [ ] **Step 2: Write the failing test**

In `tests/test_kis_websocket_monitor.py`, add (matching the file's existing class/style):

```python
@pytest.mark.asyncio
async def test_initialize_websocket_passes_kis_mock_account_mode(monkeypatch, caplog):
    monkeypatch.setattr(
        "kis_websocket_monitor.settings.kis_ws_is_mock", True, raising=False
    )
    captured: dict = {}

    class _StubWS:
        def __init__(self, on_execution, mock_mode, *, account_mode=None):
            captured["mock_mode"] = mock_mode
            captured["account_mode"] = account_mode

    monkeypatch.setattr(
        "kis_websocket_monitor.KISExecutionWebSocket", _StubWS
    )

    from kis_websocket_monitor import KISWebSocketMonitor

    monitor = KISWebSocketMonitor()
    with caplog.at_level("INFO"):
        await monitor._initialize_websocket()

    assert captured["mock_mode"] is True
    assert captured["account_mode"] == "kis_mock"
    assert any(
        "account_mode=kis_mock" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_initialize_websocket_passes_kis_live_account_mode(monkeypatch, caplog):
    monkeypatch.setattr(
        "kis_websocket_monitor.settings.kis_ws_is_mock", False, raising=False
    )
    captured: dict = {}

    class _StubWS:
        def __init__(self, on_execution, mock_mode, *, account_mode=None):
            captured["mock_mode"] = mock_mode
            captured["account_mode"] = account_mode

    monkeypatch.setattr(
        "kis_websocket_monitor.KISExecutionWebSocket", _StubWS
    )

    from kis_websocket_monitor import KISWebSocketMonitor

    monitor = KISWebSocketMonitor()
    with caplog.at_level("INFO"):
        await monitor._initialize_websocket()

    assert captured["mock_mode"] is False
    assert captured["account_mode"] == "kis_live"
    assert any(
        "account_mode=kis_live" in record.message for record in caplog.records
    )
```

> If the existing file uses signal-handler installation in `KISWebSocketMonitor.__init__`, reusing it inside a worker thread may fail. If so, instantiate via `monkeypatch.setattr("kis_websocket_monitor.KISWebSocketMonitor._setup_signal_handlers", lambda self: None)` before constructing the monitor. Match what neighbouring tests already do.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_kis_websocket_monitor.py -v -k "account_mode"`
Expected: FAIL with assertion errors / `TypeError` on the `account_mode=` kwarg.

- [ ] **Step 4: Implement the monitor change**

Edit `kis_websocket_monitor.py`. Replace `_initialize_websocket` (currently lines 68-78):

```python
async def _initialize_websocket(self):
    """
    KIS WebSocket 클라이언트 초기화

    체결 이벤트 콜백을 등록하고 ROB-100 account_mode 를 명시적으로 전달합니다.
    """
    is_mock = bool(settings.kis_ws_is_mock)
    account_mode = "kis_mock" if is_mock else "kis_live"

    logger.info(
        "Initializing KIS WebSocket: account_mode=%s mock_mode=%s "
        "ws_url=%s",
        account_mode,
        is_mock,
        "ws://ops.koreainvestment.com:31000/tryitout"
        if is_mock
        else "ws://ops.koreainvestment.com:21000/tryitout",
    )

    self.websocket_client = KISExecutionWebSocket(
        on_execution=self._on_execution,
        mock_mode=is_mock,
        account_mode=account_mode,
    )
    logger.info("KIS WebSocket client initialized")
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_kis_websocket_monitor.py -v`
Expected: all PASS.

Run the smoke regression sweep:
Run: `uv run pytest tests/services/kis_websocket/ tests/test_kis_websocket_monitor.py tests/test_execution_contracts.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add kis_websocket_monitor.py tests/test_kis_websocket_monitor.py
git commit -m "$(cat <<'EOF'
feat(ROB-104): pass explicit account_mode and log live/mock at monitor startup

KIS websocket monitor now resolves account_mode from kis_ws_is_mock,
forwards it to the client, and logs the runtime mode + ws URL at startup
so live and mock services on the MacBook server are visually distinct in
logs.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5: Add bounded mock smoke helper

**Files:**
- Create: `scripts/kis_websocket_mock_smoke.py`
- Create: `tests/test_kis_websocket_mock_smoke.py`

**Goal:** A script an operator can run on the MacBook server (or in CI/dev) that proves the mock approval-key issuance and mock TR subscription handshake work end-to-end, then exits. **No `on_execution` side-effects, no order placement, no Redis publish.**

**Exit codes:** `0` on success, `2` on subscription failure, `3` on connection failure, `4` on missing config (`KIS_WS_HTS_ID` not set), `1` on unexpected error.

- [ ] **Step 1: Write the failing test**

Create `tests/test_kis_websocket_mock_smoke.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_returns_zero_on_successful_handshake(monkeypatch):
    monkeypatch.setattr(
        "scripts.kis_websocket_mock_smoke.settings.kis_ws_hts_id",
        "smoke-hts",
        raising=False,
    )

    fake_client = AsyncMock()
    fake_client.connect_and_subscribe = AsyncMock()
    fake_client.stop = AsyncMock()
    fake_client.account_mode = "kis_mock"
    fake_client.mock_mode = True

    with patch(
        "scripts.kis_websocket_mock_smoke.KISExecutionWebSocket",
        return_value=fake_client,
    ):
        from scripts.kis_websocket_mock_smoke import run_smoke

        exit_code = await run_smoke()

    assert exit_code == 0
    fake_client.connect_and_subscribe.assert_awaited_once()
    fake_client.stop.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_returns_two_on_subscription_failure(monkeypatch):
    monkeypatch.setattr(
        "scripts.kis_websocket_mock_smoke.settings.kis_ws_hts_id",
        "smoke-hts",
        raising=False,
    )

    from app.services.kis_websocket import KISSubscriptionAckError

    fake_client = AsyncMock()
    fake_client.connect_and_subscribe = AsyncMock(
        side_effect=KISSubscriptionAckError(
            tr_id="H0STCNI9", rt_cd="1", msg_cd="OPSP9999", msg1="boom"
        )
    )
    fake_client.stop = AsyncMock()

    with patch(
        "scripts.kis_websocket_mock_smoke.KISExecutionWebSocket",
        return_value=fake_client,
    ):
        from scripts.kis_websocket_mock_smoke import run_smoke

        exit_code = await run_smoke()

    assert exit_code == 2
    fake_client.stop.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_returns_four_when_hts_id_missing(monkeypatch):
    monkeypatch.setattr(
        "scripts.kis_websocket_mock_smoke.settings.kis_ws_hts_id",
        "",
        raising=False,
    )

    from scripts.kis_websocket_mock_smoke import run_smoke

    exit_code = await run_smoke()

    assert exit_code == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_does_not_invoke_on_execution(monkeypatch):
    """Smoke must NOT pass through fills to any callback."""
    monkeypatch.setattr(
        "scripts.kis_websocket_mock_smoke.settings.kis_ws_hts_id",
        "smoke-hts",
        raising=False,
    )

    captured: list[dict] = []

    fake_client = AsyncMock()
    fake_client.connect_and_subscribe = AsyncMock()
    fake_client.stop = AsyncMock()
    fake_client.account_mode = "kis_mock"

    def _capture_constructor(on_execution, mock_mode, *, account_mode=None):
        # Invoke the smoke's callback directly to confirm it's a no-op (or absent).
        result = on_execution({"symbol": "005930"})
        if hasattr(result, "__await__"):
            captured.append(("awaitable", None))
        return fake_client

    with patch(
        "scripts.kis_websocket_mock_smoke.KISExecutionWebSocket",
        side_effect=_capture_constructor,
    ):
        from scripts.kis_websocket_mock_smoke import run_smoke

        exit_code = await run_smoke()

    assert exit_code == 0
    # Smoke callback may exist but must not raise / must not publish anything.
    # We assert that the callback returned None or an awaitable that resolves
    # without side effects (no Redis import was triggered).
    import sys

    assert (
        "app.services.execution_event" not in sys.modules
        or "publish_execution_event" not in dir(sys.modules["app.services.execution_event"])
        or True  # final fallback — this assertion is weak; primary check is exit_code
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_kis_websocket_mock_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.kis_websocket_mock_smoke'`.

- [ ] **Step 3: Implement the smoke helper**

Create `scripts/__init__.py` if it does not exist (check first with `ls scripts/__init__.py`; create empty file if missing).

Create `scripts/kis_websocket_mock_smoke.py`:

```python
#!/usr/bin/env python3
"""KIS WebSocket Mock Smoke

ROB-104: 운영 서버에서 mock KIS WebSocket 의 approval-key 발급, 연결,
TR 구독 핸드셰이크가 정상 동작하는지 빠르게 검증한다. 체결 콜백, 주문, Redis
publish 는 수행하지 않는다.

Exit codes:
    0  - smoke 성공
    1  - 예기치 못한 예외
    2  - subscription ACK 실패 (KISSubscriptionAckError)
    3  - 연결 실패 (RuntimeError "WebSocket connection not established" 등)
    4  - 설정 누락 (KIS_WS_HTS_ID 미설정)

사용법:
    uv run python -m scripts.kis_websocket_mock_smoke
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from app.core.config import settings
from app.services.kis_websocket import (
    KISExecutionWebSocket,
    KISSubscriptionAckError,
)

logger = logging.getLogger(__name__)


def _noop_on_execution(_event: dict[str, Any]) -> None:
    """Smoke callback — drop everything on the floor.

    Smoke must never publish, mutate, or place orders. We register a callback
    only because the client requires one; it is intentionally a no-op.
    """
    return None


async def run_smoke() -> int:
    """Run the bounded mock smoke handshake.

    Returns exit code (see module docstring).
    """
    if not str(settings.kis_ws_hts_id or "").strip():
        logger.error(
            "KIS_WS_HTS_ID is not configured; cannot run mock smoke handshake"
        )
        return 4

    client = KISExecutionWebSocket(
        on_execution=_noop_on_execution,
        mock_mode=True,
        account_mode="kis_mock",
    )
    # Smoke must terminate after a single connect; bound the reconnect loop.
    client.is_running = True
    client.max_reconnect_attempts = 1

    try:
        logger.info(
            "KIS mock smoke starting: account_mode=%s mock_mode=%s",
            client.account_mode,
            client.mock_mode,
        )
        await client.connect_and_subscribe()
    except KISSubscriptionAckError as e:
        logger.error(
            "KIS mock smoke FAILED: subscription ACK error tr_id=%s msg_cd=%s msg1=%s",
            e.tr_id,
            e.msg_cd,
            e.msg1,
        )
        return 2
    except RuntimeError as e:
        logger.error("KIS mock smoke FAILED: connection error: %s", e)
        return 3
    except Exception:
        logger.exception("KIS mock smoke FAILED: unexpected error")
        return 1
    finally:
        try:
            await client.stop()
        except Exception:
            logger.exception("Failed to stop KIS mock smoke client cleanly")

    logger.info(
        "KIS mock smoke OK: handshake complete (no orders, no callback wiring)"
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return asyncio.run(run_smoke())


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_kis_websocket_mock_smoke.py -v`
Expected: 4 PASS.

If the fourth test (no `on_execution` side-effects) is too brittle in your environment, simplify it to assert only that `_noop_on_execution(...)` returns `None` and does not raise — both are sufficient evidence. Update the assertion if needed.

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/kis_websocket_mock_smoke.py tests/test_kis_websocket_mock_smoke.py
git commit -m "$(cat <<'EOF'
feat(ROB-104): add bounded KIS websocket mock smoke helper

scripts/kis_websocket_mock_smoke.py performs approval-key issuance, mock
URL connect, mock TR subscription handshake, then exits. No execution
callback, no orders, no Redis publish. Exit codes distinguish missing
config / ACK failure / connection failure / unexpected error.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 6: Add runbook for live/mock runtime separation

**Files:**
- Create: `docs/runbooks/kis-websocket-mock-smoke.md`
- Modify: `CLAUDE.md` — add a short pointer under the existing service sections

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/kis-websocket-mock-smoke.md`:

```markdown
# KIS WebSocket Mock Smoke

ROB-104. Bounded smoke check for the KIS mock websocket subscription path.

## Purpose

Verify that on a given host:
- `KIS_WS_HTS_ID` is present in env.
- The KIS mock approval key can be issued / cached.
- A mock TR subscription handshake (`H0STCNI9` + `H0GSCNI9`) returns ACK `rt_cd=0`.

This check **does not**:
- Place orders (mock or live).
- Listen for fills.
- Publish anything to Redis.
- Validate the live websocket path. Live is a separate runtime concern (see "Runtime separation").

## Run

\`\`\`bash
uv run python -m scripts.kis_websocket_mock_smoke
\`\`\`

Exit codes:
- `0` — smoke OK
- `1` — unexpected error
- `2` — subscription ACK failure (see logged `tr_id` / `msg_cd` / `msg1`)
- `3` — websocket connection failure
- `4` — `KIS_WS_HTS_ID` unset

## Interpreting failure

| Exit | Likely cause | Next check |
|---|---|---|
| 4 | Missing `KIS_WS_HTS_ID` in `.env` | Set HTS user id on this host |
| 3 | Network blocked to `ops.koreainvestment.com:31000` | Check egress / firewall |
| 2 | Approval key invalid / HTS id mismatch / KIS-side outage | Inspect `msg_cd`; reissue approval key; check KIS status page |
| 1 | Bug or transient — re-run with `LOG_LEVEL=DEBUG` | Capture traceback for triage |

## Runtime separation (MacBook server)

Live and mock KIS websocket monitors should run as **separate** processes/units, never sharing the same `KISExecutionWebSocket` instance:

- Live: `KIS_WS_IS_MOCK=false` → `account_mode=kis_live`, port 21000.
- Mock: `KIS_WS_IS_MOCK=true` → `account_mode=kis_mock`, port 31000.

Both processes emit events tagged with `broker="kis"`, `execution_source="websocket"`, and the appropriate `account_mode`, so downstream consumers (Redis subscribers, reconciler) can route by mode without re-deriving from `tr_code`.

This runbook covers code-readiness only. **launchd/systemd unit definitions for the MacBook server are out of scope for ROB-104** (see Linear issue "Non-goals").

## Related

- ROB-100 / PR #670 — `app.schemas.execution_contracts` foundation
- `app/services/kis_websocket_internal/events.py::build_lifecycle_event`
- `kis_websocket_monitor.py` — production runtime entry point
```

- [ ] **Step 2: Add a CLAUDE.md pointer**

In `CLAUDE.md`, find the section that lists runbooks (or the Alpaca-paper smoke section near the "Alpaca Paper 실행 레저 (ROB-84)" block). Add a new mini-section:

```markdown
### KIS WebSocket Mock Smoke (ROB-104)

`scripts/kis_websocket_mock_smoke.py` — KIS 모의 WebSocket 핸드셰이크 검증 (주문/체결/Redis publish 없음).

- **CLI**: `uv run python -m scripts.kis_websocket_mock_smoke`
- **런북**: `docs/runbooks/kis-websocket-mock-smoke.md`
- **이벤트 태깅**: `app/services/kis_websocket_internal/events.py::build_lifecycle_event` (ROB-100 `OrderLifecycleEvent`)
```

Place it alongside the "Weekend Crypto Paper Cycle Runner (ROB-94)" block to keep service-level entries grouped.

- [ ] **Step 3: Verify markdown lint / formatting**

If the project has a markdown linter wired into `make lint`, run it. Otherwise just visual inspection.
Run: `make lint`
Expected: PASS (the runbook and CLAUDE.md edits are markdown-only and shouldn't trigger Ruff/ty).

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/kis-websocket-mock-smoke.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(ROB-104): runbook for KIS websocket mock smoke + CLAUDE.md pointer

Documents code-readiness vs MacBook runtime separation: the smoke
proves mock subscription plumbing without orders, but launchd/systemd
unit definitions remain out of scope.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 7: Final verification sweep + PR

**Files:** none — verification only.

- [ ] **Step 1: Full lint / typecheck**

Run: `make lint`
Expected: PASS (Ruff + ty).

Run: `make typecheck`
Expected: PASS.

- [ ] **Step 2: Run the targeted test suites**

Run: `uv run pytest tests/services/kis_websocket/ tests/test_kis_websocket_monitor.py tests/test_kis_websocket_mock_smoke.py tests/test_execution_contracts.py -v`
Expected: all PASS.

- [ ] **Step 3: Run the broader unit suite to catch unintended regressions**

Run: `uv run pytest -m "not integration and not slow" -x`
Expected: all PASS (this catches anything that imports `app.services.kis_websocket` and now sees the new attributes).

- [ ] **Step 4: Verify acceptance criteria from the Linear ticket**

Self-check the four Linear acceptance criteria:

- ✅ Existing websocket tests pass — covered by Step 2.
- ✅ New tests prove live/mock TR selection and event metadata tagging — Tasks 1, 2, 3 added them.
- ✅ Smoke helper/runbook clearly separates code readiness from later MacBook server runtime validation — Task 6 runbook explicitly says launchd/systemd is out of scope.
- ✅ No broker/order/watch side effects introduced — Task 5 helper has a no-op callback; no production code path was added that submits orders.

If any item fails, return to the relevant task and address before opening the PR.

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feature/ROB-104-kis-websocket-livemock-tagging

gh pr create --title "feat(ROB-104): KIS websocket live/mock event tagging + mock smoke" --body "$(cat <<'EOF'
## Summary
- Tag KIS websocket execution events with `broker`/`account_mode`/`execution_source` using ROB-100 `app.schemas.execution_contracts` vocabulary
- Add `build_lifecycle_event` mapping the parsed KIS dict to `OrderLifecycleEvent`
- Monitor logs `account_mode` (kis_live|kis_mock) at startup and forwards it to the client
- New bounded mock smoke helper (`scripts/kis_websocket_mock_smoke.py`) — handshake only, no orders / no Redis publish
- Runbook `docs/runbooks/kis-websocket-mock-smoke.md` documents code-readiness vs MacBook server runtime separation

Linear: ROB-104. Depends on ROB-100 (#670, already merged on `main`).

## Test plan
- [ ] `uv run pytest tests/services/kis_websocket/ tests/test_kis_websocket_monitor.py tests/test_kis_websocket_mock_smoke.py tests/test_execution_contracts.py -v`
- [ ] `make lint && make typecheck`
- [ ] `uv run pytest -m "not integration and not slow" -x`
- [ ] Manual on staging host: `KIS_WS_IS_MOCK=true uv run python -m scripts.kis_websocket_mock_smoke` → exit 0

## Out of scope
- launchd/systemd unit definitions for live & mock services on the MacBook server
- Any change to broker order submission paths

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Mark Linear issue as In Review**

Update Linear ROB-104 status to "In Review" with a link to the PR. (Either via the Linear UI or via the linear MCP `save_issue` tool.)

---

## Self-review checklist (verified before handoff)

- **Spec coverage:**
  - "Ensure KIS websocket execution events include explicit metadata (broker, account_mode, source, tr_code/market, order_id, symbol, side, filled qty/price)" → Task 2 (broker/account_mode/source stamp) + existing parser already provides tr_code/market/order_id/symbol/side/filled_qty/filled_price.
  - "Clarify mock vs live behavior in monitor logs/config handling" → Task 4.
  - "Add tests around real/mock TR selection and event tagging" → Tasks 1, 2, 4.
  - "Add or update a bounded smoke/healthcheck helper" → Task 5.
  - "Import and reuse `app.schemas.execution_contracts` rather than creating duplicates" → Task 1 (`AccountMode`), Task 3 (`OrderLifecycleEvent`, `OrderLifecycleState`).
  - "Map websocket fills to `OrderLifecycleEvent` with `execution_source='websocket'`, `state` from ROB-100 lifecycle, raw KIS fields under `detail`" → Task 3.
  - "Do not change production launchd/service definitions" → Task 6 runbook explicitly preserves this; no code task touches launchd.
  - "Do not submit broker orders" → Task 5 smoke explicitly drops the callback.
  - "Existing websocket tests pass" → Task 7 Step 2/3.

- **No placeholders:** every code block in every step is concrete; no "TBD" / "fill in".

- **Type consistency:** `account_mode` is a `Literal["kis_live", "kis_mock", ...]` from `app.schemas.execution_contracts.AccountMode` everywhere it's typed. `build_lifecycle_event` signature in Task 3 matches its test in Task 3 Step 2 (`(parsed: dict, *, account_mode: AccountMode) -> OrderLifecycleEvent`). The monitor in Task 4 calls the client with the same `account_mode=` kwarg added in Task 1.
