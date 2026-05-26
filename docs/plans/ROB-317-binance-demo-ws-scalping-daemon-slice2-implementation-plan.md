# Binance Demo WS Scalping Daemon — Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the default-disabled foundation of the ROB-317 WebSocket scalping daemon — the read-only public-futures host allowlist, the 3-layer gate config, the pure in-memory market-state + freshness model, the health snapshot, a default-disabled CLI, and the AST import-guard for the new read-only package.

**Architecture:** This is slice 2 of 4 (slice 1 = the design doc `docs/plans/ROB-317-binance-demo-websocket-scalping-daemon.md`). It ships only complete, fully-tested, **inert** units — no WS streaming, no signal trigger, no broker bridge (those are slices 3–4). Everything here is pure/deterministic or gate-gated, so it is independently shippable and provably safe: with gates off the CLI subscribes to nothing and mutates nothing.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, stdlib `dataclasses`/`datetime`/`decimal`/`ast`. No new third-party deps. Mirrors existing ROB-298/ROB-307 patterns (`host_allowlist.py`, `_truthy` env gating, the `test_no_testnet_imports.py` AST guard).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/services/brokers/binance/host_allowlist.py` (modify) | Add read-only `PUBLIC_FUTURES_STREAM_HOSTS` + `assert_public_futures_stream_host()` |
| `app/services/brokers/binance/demo_scalping_ws/__init__.py` (create) | New read-only hot-path package marker |
| `app/services/brokers/binance/demo_scalping_ws/config.py` (create) | `WsDaemonGates` — 3-layer env gating |
| `app/services/brokers/binance/demo_scalping_ws/state.py` (create) | `MarketState` — per-symbol price/quote + freshness |
| `app/services/brokers/binance/demo_scalping_ws/health.py` (create) | `DaemonHealthSnapshot` — liveness JSON |
| `scripts/binance_demo_scalping_ws_daemon.py` (create) | Default-disabled CLI entrypoint |
| `tests/services/brokers/binance/test_public_futures_stream_allowlist.py` (create) | Allowlist + disjointness + signed-reject |
| `tests/services/brokers/binance/demo_scalping_ws/test_config.py` (create) | Gate parsing |
| `tests/services/brokers/binance/demo_scalping_ws/test_state.py` (create) | State + freshness |
| `tests/services/brokers/binance/demo_scalping_ws/test_health.py` (create) | Health snapshot JSON |
| `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py` (create) | CLI default-disabled behavior |
| `tests/services/brokers/binance/demo/test_no_testnet_imports.py` (modify) | Add `demo_scalping_ws/` boundary assertion |

**Deferred to later slices (do NOT create here):** `market_stream.py`, `signal.py`, `supervisor.py` (slice 3); `demo_scalping_exec/ws_bridge.py` + concurrency guard + confirm-gated executor wiring (slice 4).

---

### Task 1: Read-only public futures stream allowlist

`fstream.binance.com` is the only public futures WS host. It is read-only/unsigned and must be **disjoint from every signed mutation allowlist**; the futures-demo signed transport must still reject it (it already lives in that transport's `_LIVE_FUTURES_HOSTS` deny path). See design §2.

**Files:**
- Modify: `app/services/brokers/binance/host_allowlist.py`
- Test: `tests/services/brokers/binance/test_public_futures_stream_allowlist.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/test_public_futures_stream_allowlist.py`:

```python
"""ROB-317 — read-only public futures stream allowlist.

fstream.binance.com is read-allowed (unsigned market data) but
signed-blocked (the futures-demo signed transport rejects it). The two
purposes never share a host with any signed mutation allowlist.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.host_allowlist import (
    FUTURES_DEMO_HOSTS,
    assert_futures_demo_host,
)
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
    PUBLIC_HOSTS,
    assert_public_futures_stream_host,
)
from app.services.brokers.binance.spot_demo.host_allowlist import SPOT_DEMO_HOSTS


def test_only_fstream() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS == frozenset({"fstream.binance.com"})


def test_disjoint_from_signed_mutation_allowlists() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(FUTURES_DEMO_HOSTS)
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(SPOT_DEMO_HOSTS)


def test_disjoint_from_public_spot_stream_allowlist() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(PUBLIC_HOSTS)


def test_signed_futures_transport_still_rejects_fstream() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_futures_demo_host("fstream.binance.com")


def test_assert_accepts_fstream() -> None:
    assert_public_futures_stream_host("fstream.binance.com")  # no raise


@pytest.mark.parametrize(
    "host",
    [
        "fapi.binance.com",  # live signed futures
        "demo-fapi.binance.com",  # demo signed futures (mutation lane)
        "stream.binance.com",  # spot public stream
        "fstream.binance.com.evil.example",  # spoofed subdomain
    ],
)
def test_assert_rejects_non_fstream(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_public_futures_stream_host(host)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/test_public_futures_stream_allowlist.py -v`
Expected: FAIL — `ImportError: cannot import name 'PUBLIC_FUTURES_STREAM_HOSTS'`.

- [ ] **Step 3: Add the allowlist to `host_allowlist.py`**

Append to `app/services/brokers/binance/host_allowlist.py` (after the existing `assert_allowed_host`):

```python
# ROB-317 — read-only public USD-M futures WS stream host. Unsigned market
# data only. Intentionally ABSENT from every signed mutation allowlist
# (FUTURES_DEMO_HOSTS / SPOT_DEMO_HOSTS): fstream is read-allowed here but the
# futures-demo signed transport still rejects it (it is in that transport's
# _LIVE_FUTURES_HOSTS deny path). See ROB-317 design §2.
PUBLIC_FUTURES_STREAM_HOSTS: frozenset[str] = frozenset(
    {
        "fstream.binance.com",
    }
)


def assert_public_futures_stream_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if host is not the public futures stream host.

    Strict equality match — no suffix/wildcard, so subdomain spoofs like
    ``fstream.binance.com.evil.example`` are rejected.
    """
    if host not in PUBLIC_FUTURES_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Public futures stream host blocked: {host!r} not in "
            f"{sorted(PUBLIC_FUTURES_STREAM_HOSTS)}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/test_public_futures_stream_allowlist.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/host_allowlist.py \
        tests/services/brokers/binance/test_public_futures_stream_allowlist.py
git commit -m "$(cat <<'EOF'
feat(rob-317): read-only public futures stream allowlist (fstream)

Slice 2. Adds PUBLIC_FUTURES_STREAM_HOSTS + assert_public_futures_stream_host
for the WS daemon's read-only market data. Disjoint from every signed
mutation allowlist; the futures-demo signed transport still rejects fstream.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: WS daemon gate config (`WsDaemonGates`)

Three independent gates, all default false. The daemon does NOT reuse the scheduler's confirm flag. `mutation_allowed` requires `daemon_active`, so `WS_CONFIRM=true` alone (without `WS_ENABLED`) can never enable mutation. See design §7.

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/__init__.py`
- Create: `app/services/brokers/binance/demo_scalping_ws/config.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/__init__.py` (empty file) and `tests/services/brokers/binance/demo_scalping_ws/test_config.py`:

```python
"""ROB-317 — WS daemon gate config (default-disabled)."""

from __future__ import annotations

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def test_empty_env_is_fully_disabled() -> None:
    gates = WsDaemonGates.from_env({})
    assert gates.base_enabled is False
    assert gates.ws_enabled is False
    assert gates.ws_confirm is False
    assert gates.daemon_active is False
    assert gates.mutation_allowed is False


def test_daemon_active_requires_both_base_and_ws() -> None:
    assert WsDaemonGates.from_env(
        {"BINANCE_DEMO_SCALPING_ENABLED": "true"}
    ).daemon_active is False
    assert WsDaemonGates.from_env(
        {"BINANCE_DEMO_SCALPING_WS_ENABLED": "true"}
    ).daemon_active is False
    assert WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
        }
    ).daemon_active is True


def test_confirm_alone_never_enables_mutation() -> None:
    # Confirm true but ws_enabled false -> no mutation.
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_CONFIRM": "true",
        }
    )
    assert gates.mutation_allowed is False


def test_mutation_allowed_requires_all_three() -> None:
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_CONFIRM": "true",
        }
    )
    assert gates.mutation_allowed is True


def test_does_not_read_scheduler_confirm_flag() -> None:
    # The scheduler's confirm flag must not enable the daemon's mutation.
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM": "true",
        }
    )
    assert gates.ws_confirm is False
    assert gates.mutation_allowed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.brokers.binance.demo_scalping_ws'`.

- [ ] **Step 3: Create the package and config**

Create `app/services/brokers/binance/demo_scalping_ws/__init__.py`:

```python
"""ROB-317 — Binance Demo WebSocket scalping daemon (read-only hot path).

This package holds the read-only hot-path units: market-data stream
decoding, in-memory state, and the event-driven trigger. It MUST NOT import
any signed execution client, the demo_scalping_exec package, or the demo
ledger writer — that boundary is AST-enforced by
``tests/services/brokers/binance/demo/test_no_testnet_imports.py``. Only the
exec-side ws_bridge (slice 4) may reach mutation layers. See ROB-317 design
§3.
"""
```

Create `app/services/brokers/binance/demo_scalping_ws/config.py`:

```python
"""ROB-317 — WS scalping daemon gate configuration (default-disabled).

Three independent gates, all default false:

* ``BINANCE_DEMO_SCALPING_ENABLED``     — master capability (shared)
* ``BINANCE_DEMO_SCALPING_WS_ENABLED``  — long-running WS daemon gate
* ``BINANCE_DEMO_SCALPING_WS_CONFIRM``  — real Demo order-mutation gate

The daemon does NOT reuse the scheduler confirm flag
(``BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM``). Daemon and scheduler are gated
independently so enabling one never silently enables the other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_BASE_ENV = "BINANCE_DEMO_SCALPING_ENABLED"
_WS_ENV = "BINANCE_DEMO_SCALPING_WS_ENABLED"
_WS_CONFIRM_ENV = "BINANCE_DEMO_SCALPING_WS_CONFIRM"


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class WsDaemonGates:
    """Resolved daemon gate state. Defaults to fully disabled."""

    base_enabled: bool
    ws_enabled: bool
    ws_confirm: bool

    @property
    def daemon_active(self) -> bool:
        """True only when the daemon may subscribe and evaluate triggers."""
        return self.base_enabled and self.ws_enabled

    @property
    def mutation_allowed(self) -> bool:
        """True only when real Demo order mutation is permitted (all three on)."""
        return self.base_enabled and self.ws_enabled and self.ws_confirm

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WsDaemonGates":
        source = dict(os.environ) if env is None else env
        return cls(
            base_enabled=_truthy(source.get(_BASE_ENV)),
            ws_enabled=_truthy(source.get(_WS_ENV)),
            ws_confirm=_truthy(source.get(_WS_CONFIRM_ENV)),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_config.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/__init__.py \
        app/services/brokers/binance/demo_scalping_ws/config.py \
        tests/services/brokers/binance/demo_scalping_ws/__init__.py \
        tests/services/brokers/binance/demo_scalping_ws/test_config.py
git commit -m "$(cat <<'EOF'
feat(rob-317): WS daemon 3-layer gate config (default-disabled)

Slice 2. WsDaemonGates with daemon_active (base+ws) and mutation_allowed
(base+ws+confirm). Does not read the scheduler confirm flag; confirm alone
never enables mutation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Per-symbol market state + freshness

Pure in-memory state. No network/broker/DB. Freshness is measured from the last event **received**, not from socket liveness ("connection alive != data fresh"). See design §5.

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/state.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_state.py`:

```python
"""ROB-317 — per-symbol market state + freshness guard."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping_ws.state import MarketState

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def test_new_state_has_no_events_and_is_stale() -> None:
    state = MarketState(symbol="XRPUSDT")
    assert state.last_event_at() is None
    assert state.is_stale(now=_NOW, max_age_seconds=120) is True


def test_last_event_at_is_max_across_streams() -> None:
    state = MarketState(symbol="XRPUSDT")
    state.book_ticker_at = _NOW - dt.timedelta(seconds=10)
    state.agg_trade_at = _NOW - dt.timedelta(seconds=3)
    assert state.last_event_at() == _NOW - dt.timedelta(seconds=3)


def test_fresh_within_max_age() -> None:
    state = MarketState(symbol="XRPUSDT", bid_price=Decimal("0.5"))
    state.agg_trade_at = _NOW - dt.timedelta(seconds=30)
    assert state.is_stale(now=_NOW, max_age_seconds=120) is False


def test_stale_beyond_max_age() -> None:
    state = MarketState(symbol="XRPUSDT")
    state.agg_trade_at = _NOW - dt.timedelta(seconds=200)
    assert state.is_stale(now=_NOW, max_age_seconds=120) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'MarketState'`.

- [ ] **Step 3: Create `state.py`**

```python
"""ROB-317 — per-symbol in-memory market state + freshness.

Pure data structures: no network, no broker, no DB. The supervisor (slice 3)
mutates these from decoded WS events; the trigger (slice 3) reads them.
Freshness is measured from the last event RECEIVED — a half-dead socket can
stay "open" while delivering nothing, so connection liveness is not a
freshness signal. See ROB-317 design §5.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True)
class MarketState:
    """Latest quote/trade for one symbol, with per-stream receipt timestamps."""

    symbol: str
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_trade_price: Decimal | None = None
    book_ticker_at: dt.datetime | None = None
    agg_trade_at: dt.datetime | None = None

    def last_event_at(self) -> dt.datetime | None:
        """Most recent receipt across all streams, or None if no data yet."""
        stamps = [t for t in (self.book_ticker_at, self.agg_trade_at) if t is not None]
        return max(stamps) if stamps else None

    def is_stale(self, *, now: dt.datetime, max_age_seconds: float) -> bool:
        """True when no event arrived within ``max_age_seconds`` (or ever)."""
        last = self.last_event_at()
        if last is None:
            return True
        return (now - last).total_seconds() > max_age_seconds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_state.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/state.py \
        tests/services/brokers/binance/demo_scalping_ws/test_state.py
git commit -m "$(cat <<'EOF'
feat(rob-317): per-symbol MarketState + last-event freshness guard

Slice 2. Pure in-memory state; freshness measured from last event received,
not socket liveness.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Daemon health snapshot

Pure data + JSON for Hermes/Prefect liveness polling. No secrets. See design §8.

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_ws/health.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_health.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_ws/test_health.py`:

```python
"""ROB-317 — daemon health snapshot JSON."""

from __future__ import annotations

import datetime as dt
import json

from app.services.brokers.binance.demo_scalping_ws.health import (
    DaemonHealthSnapshot,
    SymbolHealth,
)

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def test_snapshot_serializes_to_json() -> None:
    snap = DaemonHealthSnapshot(
        generated_at=_NOW,
        connected=True,
        daemon_active=True,
        mutation_allowed=False,
        symbols=(
            SymbolHealth(
                symbol="XRPUSDT",
                fresh=True,
                last_event_at=_NOW - dt.timedelta(seconds=2),
                age_seconds=2.0,
            ),
        ),
    )
    payload = json.loads(snap.to_json())
    assert payload["connected"] is True
    assert payload["mutation_allowed"] is False
    assert payload["symbols"][0]["symbol"] == "XRPUSDT"
    assert payload["symbols"][0]["fresh"] is True
    assert payload["symbols"][0]["age_seconds"] == 2.0


def test_snapshot_handles_symbol_with_no_events() -> None:
    snap = DaemonHealthSnapshot(
        generated_at=_NOW,
        connected=False,
        daemon_active=False,
        mutation_allowed=False,
        symbols=(
            SymbolHealth(
                symbol="DOGEUSDT", fresh=False, last_event_at=None, age_seconds=None
            ),
        ),
    )
    payload = json.loads(snap.to_json())
    assert payload["symbols"][0]["last_event_at"] is None
    assert payload["symbols"][0]["age_seconds"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'DaemonHealthSnapshot'`.

- [ ] **Step 3: Create `health.py`**

```python
"""ROB-317 — daemon health/heartbeat snapshot (pure data + JSON).

Emitted for Hermes/Prefect liveness polling. Contains only operational
status — never credentials or order payloads. See ROB-317 design §8.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SymbolHealth:
    """Per-symbol freshness view for the health snapshot."""

    symbol: str
    fresh: bool
    last_event_at: dt.datetime | None
    age_seconds: float | None


@dataclass(frozen=True, slots=True)
class DaemonHealthSnapshot:
    """Point-in-time daemon liveness snapshot."""

    generated_at: dt.datetime
    connected: bool
    daemon_active: bool
    mutation_allowed: bool
    symbols: tuple[SymbolHealth, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "connected": self.connected,
            "daemon_active": self.daemon_active,
            "mutation_allowed": self.mutation_allowed,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "fresh": s.fresh,
                    "last_event_at": (
                        s.last_event_at.isoformat() if s.last_event_at else None
                    ),
                    "age_seconds": s.age_seconds,
                }
                for s in self.symbols
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_health.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/health.py \
        tests/services/brokers/binance/demo_scalping_ws/test_health.py
git commit -m "$(cat <<'EOF'
feat(rob-317): daemon health snapshot JSON (no secrets)

Slice 2. DaemonHealthSnapshot/SymbolHealth for Hermes/Prefect liveness polling.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Default-disabled CLI entrypoint

With gates off, the CLI subscribes to nothing and exits 0. With gates on, slice 2 has no supervisor yet, so it reports `pending_supervisor` and exits 0 **without subscribing** (no network). This satisfies the "disabled path never subscribes" criterion while staying honest about slice scope. See design §11.2.

**Files:**
- Create: `scripts/binance_demo_scalping_ws_daemon.py`
- Test: `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`:

```python
"""ROB-317 — WS daemon CLI default-disabled behavior."""

from __future__ import annotations

import json

from scripts.binance_demo_scalping_ws_daemon import build_summary, main
from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def test_summary_disabled_when_gates_off() -> None:
    gates = WsDaemonGates(base_enabled=False, ws_enabled=False, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "disabled"
    assert summary["subscribed"] is False


def test_summary_pending_supervisor_when_gates_on() -> None:
    gates = WsDaemonGates(base_enabled=True, ws_enabled=True, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "pending_supervisor"
    assert summary["subscribed"] is False
    assert summary["mutation_allowed"] is False


def test_main_disabled_exits_zero_and_prints_json(capsys, monkeypatch) -> None:
    for key in (
        "BINANCE_DEMO_SCALPING_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_CONFIRM",
    ):
        monkeypatch.delenv(key, raising=False)
    rc = main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "disabled"
    assert out["subscribed"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.binance_demo_scalping_ws_daemon'`.

- [ ] **Step 3: Create the CLI**

Create `scripts/binance_demo_scalping_ws_daemon.py`:

```python
"""ROB-317 — operator CLI for the Binance Demo WebSocket scalping daemon.

Default-disabled. Behaviour is entirely env-gated (see WsDaemonGates):

* ``BINANCE_DEMO_SCALPING_ENABLED`` + ``BINANCE_DEMO_SCALPING_WS_ENABLED`` —
  both must be truthy for the daemon to subscribe/evaluate.
* ``BINANCE_DEMO_SCALPING_WS_CONFIRM`` — only when also truthy may real Demo
  orders be placed (slice 4 wires the bridge; this slice never mutates).

Slice 2 ships the gate plumbing only: with gates off it prints a disabled
summary and exits 0 without subscribing; with gates on it reports
``pending_supervisor`` (the streaming supervisor lands in slice 3) and still
does not open any socket. Demo hosts only; no live/testnet path; no secrets
printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def build_summary(gates: WsDaemonGates) -> dict[str, Any]:
    """Map resolved gates to a single-line JSON-able summary.

    ``subscribed`` is always False in slice 2 — no socket is ever opened here.
    """
    if not gates.daemon_active:
        return {
            "status": "disabled",
            "base_enabled": gates.base_enabled,
            "ws_enabled": gates.ws_enabled,
            "subscribed": False,
        }
    return {
        "status": "pending_supervisor",
        "base_enabled": gates.base_enabled,
        "ws_enabled": gates.ws_enabled,
        "mutation_allowed": gates.mutation_allowed,
        "subscribed": False,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-317 Binance Demo WebSocket scalping daemon. Default-disabled "
            "(zero side effects). Set BINANCE_DEMO_SCALPING_ENABLED=true and "
            "BINANCE_DEMO_SCALPING_WS_ENABLED=true to activate."
        )
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    gates = WsDaemonGates.from_env()
    summary = build_summary(gates)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/binance_demo_scalping_ws_daemon.py \
        tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
git commit -m "$(cat <<'EOF'
feat(rob-317): default-disabled WS daemon CLI entrypoint

Slice 2. Gates off -> disabled summary, exit 0, no subscribe. Gates on ->
pending_supervisor (streaming lands in slice 3), still no socket, no mutation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: AST import-guard for the read-only `demo_scalping_ws/` package

Extend the existing static guard so the read-only hot-path package can never import a signed execution client, the `demo_scalping_exec` package, or the demo ledger writer. Only the exec-side `ws_bridge` (slice 4) may reach those. See design §3.1.

**Files:**
- Modify: `tests/services/brokers/binance/demo/test_no_testnet_imports.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/brokers/binance/demo/test_no_testnet_imports.py`:

```python
def test_demo_scalping_ws_does_not_import_mutation_layers() -> None:
    """ROB-317 — demo_scalping_ws/ is the read-only scalping hot path.

    It computes triggers from public market data but must NOT import any
    signed execution client, the demo_scalping_exec/ package, or the demo
    ledger writer. Only the exec-side ws_bridge (slice 4) may reach those.
    Keeps the read-only boundary AST-verifiable (cf. ROB-307 signal/exec
    split).
    """
    ws_root = pathlib.Path("app/services/brokers/binance/demo_scalping_ws")
    banned_substrings = (
        "execution_client",
        "binance.demo_scalping_exec",
        "binance.demo.ledger",
    )
    offenders: list[str] = []
    for py in ws_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(b in node.module for b in banned_substrings):
                    offenders.append(f"{py}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(b in alias.name for b in banned_substrings):
                        offenders.append(f"{py}: import {alias.name}")
    assert not offenders, (
        "demo_scalping_ws/ (read-only hot path) must not import mutation "
        "layers (execution clients / demo_scalping_exec / demo.ledger). "
        "Offenders:\n" + "\n".join(offenders)
    )
```

- [ ] **Step 2: Run test to verify it passes (guard holds on current tree)**

The slice-2 package (`__init__.py`, `config.py`, `state.py`, `health.py`) imports none of the banned layers, so the guard passes immediately — it locks the boundary for slices 3–4.

Run: `uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py::test_demo_scalping_ws_does_not_import_mutation_layers -v`
Expected: PASS.

- [ ] **Step 3: Verify the guard actually bites (temporary negative check)**

Temporarily add `from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService  # noqa` to `app/services/brokers/binance/demo_scalping_ws/state.py`, re-run the test, and confirm it now FAILS listing `state.py` as an offender. Then **remove** the temporary import and confirm the test PASSES again. (Do not commit the temporary import.)

Run: `uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py::test_demo_scalping_ws_does_not_import_mutation_layers -v`
Expected: FAIL with the temporary import present; PASS after removal.

- [ ] **Step 4: Run the full guard module**

Run: `uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -v`
Expected: PASS (all existing guards + the new one).

- [ ] **Step 5: Commit**

```bash
git add tests/services/brokers/binance/demo/test_no_testnet_imports.py
git commit -m "$(cat <<'EOF'
test(rob-317): AST guard — demo_scalping_ws read-only boundary

Slice 2. Read-only hot path may not import signed execution clients,
demo_scalping_exec, or the demo ledger writer. Only the slice-4 exec-side
ws_bridge may.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Slice-wide verification

**Files:** none (verification only).

- [ ] **Step 1: Run all slice-2 tests**

Run:
```bash
uv run pytest \
  tests/services/brokers/binance/test_public_futures_stream_allowlist.py \
  tests/services/brokers/binance/demo_scalping_ws/ \
  tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py \
  tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
```
Expected: PASS (all).

- [ ] **Step 2: Lint changed surfaces**

Run:
```bash
uv run ruff check \
  app/services/brokers/binance/host_allowlist.py \
  app/services/brokers/binance/demo_scalping_ws/ \
  scripts/binance_demo_scalping_ws_daemon.py \
  tests/services/brokers/binance/ tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
```
Expected: no errors. If ruff reports fixable issues, run `uv run ruff format` on the same paths and re-run check, then amend the relevant commit.

- [ ] **Step 3: Confirm the CLI is genuinely inert by default**

Run: `uv run python -m scripts.binance_demo_scalping_ws_daemon`
Expected (sort_keys output): `{"base_enabled": false, "status": "disabled", "subscribed": false, "ws_enabled": false}`, exit 0. No network, no credential read, no order.

- [ ] **Step 4: Full import-guard + targeted regression sweep**

Run: `uv run pytest tests/services/brokers/binance/ -q`
Expected: PASS (slice-2 additions plus existing binance broker tests unaffected).

---

## Self-Review

**Spec coverage (slice-2 scope of design §13 item 2):**
- New read-only allowlist (design §2.2) → Task 1 ✓
- 3-layer env gates, confirm separate from scheduler (design §7) → Task 2 ✓
- In-memory state + freshness-from-last-event (design §5) → Task 3 ✓
- Health snapshot JSON (design §8) → Task 4 ✓
- Default-disabled CLI, never subscribes when off (design §7, §11.2) → Task 5 ✓
- AST import-guard for read-only package (design §3.1) → Task 6 ✓
- Disjointness + signed-reject test (design §2.2, test list) → Task 1 ✓
- CLI default-disabled test (test list) → Task 5 ✓

Deferred by design (NOT slice-2 gaps): WS event→state→trigger, stale-data trigger block, risk-gate path, one-open-lifecycle/duplicate-trigger, reconnect/backoff (slices 3–4, design §13). The slice-3/4 plans will cover these test-list items.

**Placeholder scan:** No "TBD"/"implement later"/vague-error steps. Every code step ships complete code. Task 6 Step 3 is an explicit (temporary, uncommitted) negative check, not a placeholder.

**Type consistency:** `WsDaemonGates(base_enabled, ws_enabled, ws_confirm)` + `.daemon_active`/`.mutation_allowed`/`.from_env` used identically in Tasks 2 and 5. `MarketState` fields/`is_stale`/`last_event_at` consistent (Task 3). `DaemonHealthSnapshot`/`SymbolHealth` field names consistent in Task 4. `build_summary` keys (`status`/`subscribed`/`mutation_allowed`) match the CLI test assertions in Task 5. `PUBLIC_FUTURES_STREAM_HOSTS`/`assert_public_futures_stream_host` consistent in Task 1.

---

## Next slices (separate plans, written when their turn comes)

- **Slice 3:** `market_stream.py` (reuse/extend `BinancePublicWSClient` for `fstream` + aggTrade parser, reconnect/backoff), `signal.py` (event-driven trigger reusing `demo_scalping/signal.py`), `supervisor.py` (asyncio queues, debounce, heartbeat). Fake-stream tests: event→state→trigger, stale-data block, reconnect/backoff.
- **Slice 4:** `demo_scalping_exec/ws_bridge.py` (live ledger risk re-check → `DemoScalpingExecutor`), two-layer concurrency guard (per-symbol lock + global semaphore), confirm gate wiring, analytics/review wiring, dedicated `docs/runbooks/` entry. Tests: confirm=false blocks mutation, risk-gate block, one-open-lifecycle/in-flight duplicate guard.
