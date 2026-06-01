# investment_report_delta_get Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only MCP tool `investment_report_delta_get` that, given a baseline report, returns three deterministic intraday deltas — target/stop touch, per-symbol holdings P/L delta, and index delta — for Hermes to pull and compose.

**Architecture:** A pure-function transform layer (no I/O) plus a thin `DeltaService` orchestrator that loads the baseline (report row + snapshot-bundle portfolio payload) and calls three injectable live-data fns. Every signal is computed in its own try/except (fail-open). No DB writes, no broker/watch mutation, no migration, no in-process LLM.

**Tech Stack:** Python 3.13, async SQLAlchemy, FastMCP tool registration, pytest. Reuses existing tools: `get_trade_journal(enrich_live=True)`, `_get_holdings_impl`, `handle_get_market_index`, `InvestmentReportQueryService.get_bundle`, `InvestmentSnapshotsRepository`.

**Spec:** `docs/superpowers/specs/2026-05-31-rob-376-report-delta-design.md`

---

## File Structure

- **Create** `app/services/investment_reports/delta_service.py` — pure helpers
  (`_levels_delta`, `_holdings_pnl_delta`, `_baseline_pnl_from_bundle_pairs`, `_index_delta`,
  `_baseline_indices`), the `DeltaService` orchestrator, and module-level default I/O fns.
- **Modify** `app/mcp_server/tooling/investment_reports_handlers.py` — add
  `investment_report_delta_get_impl`, register it, add its name to
  `INVESTMENT_REPORT_TOOL_NAMES` and `__all__`.
- **Create** `tests/services/investment_reports/test_delta_service.py` — pure-helper unit tests
  + orchestrator tests with injected fakes (no DB).
- **Create** `tests/services/investment_reports/test_delta_service_db.py` — one DB-integration
  test for the default baseline loader (seeds a report row only) + `baseline_not_found`.
- **Create** `tests/mcp_server/test_investment_report_delta_tool.py` — handler + registration test.

### Key data shapes (verified against current code)

- `get_trade_journal(enrich_live=True, account_type=..., market=...)` →
  `{"success": True, "entries": [{"symbol", "side", "target_price", "stop_loss",
  "current_price", "pnl_pct_live", "target_reached", "stop_reached", ...}], "summary": {...}}`.
- `_get_holdings_impl(market=..., include_current_price=True)` →
  `{"accounts": [{"positions": [{"symbol", "profit_rate", ...}]}], ...}`.
- `handle_get_market_index()` (no symbol) → `{"indices": [{"symbol", "current", "change_pct", ...}]}`.
- Report row `market_snapshot` = `{"provenance": {...}, "baseline": {"market", "from_date",
  "to_date", "indices": {"<sym>": {"change_percent", "name", "current"}}}}`
  **or** `{"status": "unavailable", "reason": ...}`.
- Snapshot-bundle `portfolio` payload `holdings[]` entries carry `ticker` + `pnl_rate`.
- `InvestmentReportQueryService.get_bundle(uuid)` → `{"report", "items", ...}` or `None`;
  `report.snapshot_bundle_uuid`, `report.market`, `report.market_snapshot`; each item has `.symbol`.
- `InvestmentSnapshotsRepository.get_bundle_by_uuid(uuid)` → bundle row (has `.id`) or `None`;
  `.list_bundle_items_with_snapshots(bundle_id)` → `[(item, snapshot), ...]`, each `snapshot`
  has `.snapshot_kind` and `.payload_json`.

---

## Task 1: Pure helper `_levels_delta`

**Files:**
- Create: `app/services/investment_reports/delta_service.py`
- Test: `tests/services/investment_reports/test_delta_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_reports/test_delta_service.py
from __future__ import annotations

from app.services.investment_reports.delta_service import _levels_delta


def test_levels_delta_filters_to_symbols_and_projects_flags():
    journal = {
        "entries": [
            {"symbol": "AAPL", "side": "buy", "target_price": 230.0, "stop_loss": 200.0,
             "current_price": 231.0, "pnl_pct_live": 4.1,
             "target_reached": True, "stop_reached": False},
            {"symbol": "MSFT", "side": "buy", "target_price": 500.0, "stop_loss": 400.0,
             "current_price": 401.0, "pnl_pct_live": -1.0,
             "target_reached": False, "stop_reached": False},
            {"symbol": "ZZZZ", "side": "buy", "target_price": 1.0, "stop_loss": 0.5,
             "current_price": 0.9, "pnl_pct_live": 0.0,
             "target_reached": False, "stop_reached": False},
        ]
    }
    out = _levels_delta(journal, {"AAPL", "MSFT"}, near_pct=1.0)
    syms = [e["symbol"] for e in out["entries"]]
    assert syms == ["AAPL", "MSFT"]  # ZZZZ filtered out
    aapl = out["entries"][0]
    assert aapl["target_reached"] is True
    assert aapl["pnl_pct_live"] == 4.1
    # MSFT current 401 is within 1% of stop 400 -> near_stop True
    msft = out["entries"][1]
    assert msft["near_stop"] is True
    assert msft["near_target"] is False
    assert out["summary"] == {
        "near_target": 0, "near_stop": 1, "target_hit": 1, "stop_hit": 0
    }


def test_levels_delta_empty_symbols_keeps_all_entries():
    journal = {"entries": [
        {"symbol": "AAPL", "side": "buy", "target_price": None, "stop_loss": None,
         "current_price": 10.0, "pnl_pct_live": 1.0,
         "target_reached": None, "stop_reached": None},
    ]}
    out = _levels_delta(journal, set(), near_pct=1.0)
    assert [e["symbol"] for e in out["entries"]] == ["AAPL"]
    assert out["entries"][0]["near_target"] is False  # no target -> not near
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: FAIL with `ImportError` / `cannot import name '_levels_delta'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/investment_reports/delta_service.py
"""ROB-376 — read-only intraday delta computation for investment reports.

Deterministic baseline-vs-live deltas (target/stop touch, per-symbol holdings P/L,
index move) for the next/intraday report. No DB writes, no broker/watch mutation,
no in-process LLM. Every signal is fail-open: one signal's failure never kills the
others.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _near(current: Any, level: Any, near_pct: float) -> bool:
    """True when ``current`` is within ``near_pct`` percent of ``level``."""
    if not _is_finite_number(current) or not _is_finite_number(level) or level == 0:
        return False
    return abs(current - level) / abs(level) * 100 <= near_pct


def _levels_delta(
    journal_result: Mapping[str, Any],
    symbols: set[str],
    *,
    near_pct: float,
) -> dict[str, Any]:
    """Project the journal's already-computed live enrichment into a delta block.

    Reuses ``target_reached`` / ``stop_reached`` / ``pnl_pct_live`` (computed by
    ``get_trade_journal(enrich_live=True)``); computes per-entry ``near_*`` flags
    here. When ``symbols`` is empty, all entries are kept.
    """
    entries: list[dict[str, Any]] = []
    near_target = near_stop = target_hit = stop_hit = 0
    for entry in journal_result.get("entries") or []:
        symbol = entry.get("symbol")
        if symbols and symbol not in symbols:
            continue
        current = entry.get("current_price")
        target = entry.get("target_price")
        stop = entry.get("stop_loss")
        is_near_target = _near(current, target, near_pct)
        is_near_stop = _near(current, stop, near_pct)
        is_target_reached = bool(entry.get("target_reached"))
        is_stop_reached = bool(entry.get("stop_reached"))
        near_target += int(is_near_target)
        near_stop += int(is_near_stop)
        target_hit += int(is_target_reached)
        stop_hit += int(is_stop_reached)
        entries.append({
            "symbol": symbol,
            "side": entry.get("side"),
            "target_price": target,
            "stop_loss": stop,
            "current_price": current,
            "pnl_pct_live": entry.get("pnl_pct_live"),
            "target_reached": entry.get("target_reached"),
            "stop_reached": entry.get("stop_reached"),
            "near_target": is_near_target,
            "near_stop": is_near_stop,
        })
    return {
        "entries": entries,
        "summary": {
            "near_target": near_target,
            "near_stop": near_stop,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/delta_service.py tests/services/investment_reports/test_delta_service.py
git commit -m "feat(ROB-376): _levels_delta pure helper for report delta tool

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Pure helpers `_holdings_pnl_delta` + `_baseline_pnl_from_bundle_pairs`

**Files:**
- Modify: `app/services/investment_reports/delta_service.py`
- Test: `tests/services/investment_reports/test_delta_service.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/services/investment_reports/test_delta_service.py
import types

from app.services.investment_reports.delta_service import (
    _baseline_pnl_from_bundle_pairs,
    _holdings_pnl_delta,
)


def _snap(kind, payload):
    return types.SimpleNamespace(snapshot_kind=kind, payload_json=payload)


def test_baseline_pnl_from_bundle_pairs_reads_portfolio_holdings():
    pairs = [
        (object(), _snap("market", {"indices": {}})),
        (object(), _snap("portfolio", {"holdings": [
            {"ticker": "AAPL", "pnl_rate": 1.0},
            {"ticker": "MSFT", "pnl_rate": -2.0},
            {"ticker": "NOPNL"},  # missing pnl_rate -> skipped, not fabricated
        ]})),
    ]
    assert _baseline_pnl_from_bundle_pairs(pairs) == {"AAPL": 1.0, "MSFT": -2.0}


def test_baseline_pnl_from_bundle_pairs_none_when_no_portfolio_kind():
    pairs = [(object(), _snap("market", {"indices": {}}))]
    assert _baseline_pnl_from_bundle_pairs(pairs) is None


def test_holdings_pnl_delta_joins_baseline_and_live_missing_not_zero():
    baseline = {"AAPL": 1.0, "MSFT": -2.0, "ONLYBASE": 5.0}
    live = {
        "accounts": [
            {"positions": [
                {"symbol": "AAPL", "profit_rate": 4.1},
                {"symbol": "MSFT", "profit_rate": -1.0},
                {"symbol": "ONLYLIVE", "profit_rate": 9.0},
                {"symbol": "NORATE"},  # missing profit_rate -> skipped
            ]},
        ]
    }
    out = _holdings_pnl_delta(baseline, live)
    by_symbol = {e["symbol"]: e for e in out["entries"]}
    assert set(by_symbol) == {"AAPL", "MSFT"}  # only symbols in BOTH
    assert by_symbol["AAPL"]["delta_pp"] == 3.1
    assert by_symbol["MSFT"]["delta_pp"] == 1.0
    assert out["summary"] == {
        "symbols_compared": 2, "symbols_baseline_only": 1, "symbols_live_only": 1
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: FAIL with `cannot import name '_holdings_pnl_delta'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to app/services/investment_reports/delta_service.py

def _baseline_pnl_from_bundle_pairs(
    pairs: list[tuple[Any, Any]],
) -> dict[str, float] | None:
    """Extract ``{ticker: pnl_rate}`` from the bundle's ``portfolio`` snapshot.

    Returns ``None`` when no ``portfolio`` snapshot is present (so the caller can
    record ``baseline_snapshot_absent`` rather than fabricating an empty baseline).
    Holdings without a finite ``pnl_rate`` are skipped (missing != zero).
    """
    for _item, snapshot in pairs:
        if getattr(snapshot, "snapshot_kind", None) != "portfolio":
            continue
        payload = getattr(snapshot, "payload_json", None) or {}
        out: dict[str, float] = {}
        for holding in payload.get("holdings") or []:
            ticker = holding.get("ticker")
            rate = holding.get("pnl_rate")
            if ticker is not None and _is_finite_number(rate):
                out[str(ticker)] = float(rate)
        return out
    return None


def _live_pnl_by_symbol(holdings_result: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for account in holdings_result.get("accounts") or []:
        for position in account.get("positions") or []:
            symbol = position.get("symbol")
            rate = position.get("profit_rate")
            if symbol is not None and _is_finite_number(rate):
                out[str(symbol)] = float(rate)
    return out


def _holdings_pnl_delta(
    baseline_pnl: Mapping[str, float],
    holdings_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-symbol live P/L vs baseline P/L. Only symbols present on BOTH sides get
    an entry (missing != zero); one-sided symbols are counted in the summary."""
    live_pnl = _live_pnl_by_symbol(holdings_result)
    baseline_keys = set(baseline_pnl)
    live_keys = set(live_pnl)
    both = baseline_keys & live_keys
    entries: list[dict[str, Any]] = []
    for symbol in sorted(both):
        base = baseline_pnl[symbol]
        live = live_pnl[symbol]
        entries.append({
            "symbol": symbol,
            "baseline_pnl_pct": base,
            "live_pnl_pct": live,
            "delta_pp": live - base,
        })
    return {
        "entries": entries,
        "summary": {
            "symbols_compared": len(both),
            "symbols_baseline_only": len(baseline_keys - live_keys),
            "symbols_live_only": len(live_keys - baseline_keys),
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/delta_service.py tests/services/investment_reports/test_delta_service.py
git commit -m "feat(ROB-376): holdings P/L delta helpers (bundle baseline vs live)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Pure helpers `_index_delta` + `_baseline_indices`

**Files:**
- Modify: `app/services/investment_reports/delta_service.py`
- Test: `tests/services/investment_reports/test_delta_service.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/services/investment_reports/test_delta_service.py
from app.services.investment_reports.delta_service import (
    _baseline_indices,
    _index_delta,
)


def test_baseline_indices_extracts_dict_or_none():
    ok = {"provenance": {}, "baseline": {"indices": {"^GSPC": {"current": 5500.0}}}}
    assert _baseline_indices(ok) == {"^GSPC": {"current": 5500.0}}
    assert _baseline_indices({"status": "unavailable", "reason": "x"}) is None
    assert _baseline_indices({"baseline": {}}) is None  # no indices key
    assert _baseline_indices({}) is None


def test_index_delta_change_pct_and_null_guards():
    baseline = {
        "^GSPC": {"current": 5500.0},
        "^VIX": {"current": 0.0},      # baseline 0 -> change_pct null, no div-by-zero
        "MISSINGLIVE": {"current": 100.0},
    }
    live = {"indices": [
        {"symbol": "^GSPC", "current": 5533.0},
        {"symbol": "^VIX", "current": 15.0},
        # MISSINGLIVE absent from live
    ]}
    out = _index_delta(baseline, live)
    by_symbol = {e["index_symbol"]: e for e in out["entries"]}
    assert round(by_symbol["^GSPC"]["change_pct"], 4) == 0.6
    assert by_symbol["^VIX"]["change_pct"] is None     # baseline 0 -> null
    assert by_symbol["MISSINGLIVE"]["live_value"] is None
    assert by_symbol["MISSINGLIVE"]["change_pct"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: FAIL with `cannot import name '_index_delta'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to app/services/investment_reports/delta_service.py

def _baseline_indices(market_snapshot: Any) -> dict[str, Any] | None:
    """Return the frozen ``baseline.indices`` dict (keyed by index symbol), or
    ``None`` when the snapshot is the ``unavailable`` shape or lacks indices."""
    if not isinstance(market_snapshot, Mapping):
        return None
    if market_snapshot.get("status") == "unavailable":
        return None
    baseline = market_snapshot.get("baseline")
    if not isinstance(baseline, Mapping):
        return None
    indices = baseline.get("indices")
    if not isinstance(indices, Mapping):
        return None
    return dict(indices)


def _index_delta(
    baseline_indices: Mapping[str, Any],
    market_index_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Live index value vs frozen baseline value, per index symbol. ``change_pct``
    is computed only when both values are finite and baseline != 0; otherwise it is
    ``null`` (never fabricated). Indices absent from the live response carry a
    ``null`` ``live_value``."""
    live_by_symbol: dict[str, Any] = {}
    for index in market_index_result.get("indices") or []:
        symbol = index.get("symbol")
        if symbol is not None:
            live_by_symbol[str(symbol)] = index.get("current")
    entries: list[dict[str, Any]] = []
    for symbol, baseline in baseline_indices.items():
        baseline_value = baseline.get("current") if isinstance(baseline, Mapping) else None
        live_value = live_by_symbol.get(symbol)
        change_pct: float | None = None
        if (
            _is_finite_number(baseline_value)
            and _is_finite_number(live_value)
            and baseline_value != 0
        ):
            change_pct = (live_value - baseline_value) / baseline_value * 100
        entries.append({
            "index_symbol": symbol,
            "baseline_value": baseline_value,
            "live_value": live_value,
            "change_pct": change_pct,
        })
    return {"entries": entries}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/delta_service.py tests/services/investment_reports/test_delta_service.py
git commit -m "feat(ROB-376): index delta helpers (frozen baseline vs live, null guards)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: `DeltaService.compute_delta` orchestrator (injected fakes)

**Files:**
- Modify: `app/services/investment_reports/delta_service.py`
- Test: `tests/services/investment_reports/test_delta_service.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/services/investment_reports/test_delta_service.py
import pytest

from app.services.investment_reports.delta_service import DeltaService


def _baseline(*, market="us", symbols=None, market_snapshot=None, baseline_pnl=None):
    return {
        "market": market,
        "symbols": symbols if symbols is not None else {"AAPL"},
        "market_snapshot": market_snapshot if market_snapshot is not None
        else {"baseline": {"indices": {"^GSPC": {"current": 5500.0}}}},
        "baseline_pnl": baseline_pnl if baseline_pnl is not None else {"AAPL": 1.0},
    }


def _service(*, baseline, journal=None, holdings=None, index=None):
    async def loader(_uuid):
        return baseline

    async def journal_fn(*, account_type, market):
        if journal is None:
            raise RuntimeError("journal boom")
        return journal

    async def holdings_fn(*, market):
        if holdings is None:
            raise RuntimeError("holdings boom")
        return holdings

    async def index_fn():
        if index is None:
            raise RuntimeError("index boom")
        return index

    return DeltaService(
        session=None,
        baseline_loader=loader,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    )


@pytest.mark.asyncio
async def test_compute_delta_happy_path_all_three():
    svc = _service(
        baseline=_baseline(),
        journal={"entries": [{"symbol": "AAPL", "side": "buy", "target_price": 230.0,
                              "stop_loss": 200.0, "current_price": 231.0,
                              "pnl_pct_live": 4.1, "target_reached": True,
                              "stop_reached": False}]},
        holdings={"accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 4.1}]}]},
        index={"indices": [{"symbol": "^GSPC", "current": 5533.0}]},
    )
    out = await svc.compute_delta(
        "11111111-1111-1111-1111-111111111111", computed_at_kst="2026-06-01T13:00:00+09:00"
    )
    assert out["success"] is True
    assert out["market"] == "us"
    assert out["computed_at_kst"] == "2026-06-01T13:00:00+09:00"
    assert out["levels_delta"]["summary"]["target_hit"] == 1
    assert out["holdings_pnl_delta"]["entries"][0]["delta_pp"] == 3.1
    assert round(out["index_delta"]["entries"][0]["change_pct"], 4) == 0.6
    assert "unavailable" not in out


@pytest.mark.asyncio
async def test_compute_delta_fail_open_isolates_each_signal():
    # journal raises; holdings + index still populate
    svc = _service(
        baseline=_baseline(),
        journal=None,  # -> RuntimeError
        holdings={"accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 2.0}]}]},
        index={"indices": [{"symbol": "^GSPC", "current": 5533.0}]},
    )
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out["success"] is True
    assert out["levels_delta"] is None
    assert out["unavailable"]["levels"]  # reason string present
    assert out["holdings_pnl_delta"]["entries"][0]["delta_pp"] == 1.0
    assert out["index_delta"]["entries"][0]["live_value"] == 5533.0


@pytest.mark.asyncio
async def test_compute_delta_baseline_absent_marks_unavailable():
    svc = _service(
        baseline=_baseline(
            market_snapshot={"status": "unavailable", "reason": "not_collected"},
            baseline_pnl=None,
        ),
        journal={"entries": []},
        holdings={"accounts": []},
        index={"indices": []},
    )
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out["success"] is True
    assert out["holdings_pnl_delta"] is None
    assert out["unavailable"]["holdings"] == "baseline_snapshot_absent"
    assert out["index_delta"] is None
    assert out["unavailable"]["index"] == "baseline_snapshot_absent"


@pytest.mark.asyncio
async def test_compute_delta_baseline_not_found():
    async def loader(_uuid):
        return None

    svc = DeltaService(session=None, baseline_loader=loader)
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out == {"success": False, "error": "baseline_not_found"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: FAIL with `cannot import name 'DeltaService'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to app/services/investment_reports/delta_service.py
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class DeltaService:
    """Orchestrates the three read-only deltas. I/O is injectable so the logic is
    unit-testable without a DB or live network. Defaults wire the real loaders/tools."""

    def __init__(
        self,
        session: Any,
        *,
        baseline_loader: Callable[[UUID], Awaitable[dict[str, Any] | None]] | None = None,
        journal_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        holdings_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        market_index_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._session = session
        self._baseline_loader = baseline_loader
        self._journal_fn = journal_fn
        self._holdings_fn = holdings_fn
        self._market_index_fn = market_index_fn

    async def compute_delta(
        self,
        report_uuid: UUID | str,
        *,
        near_pct: float = 1.0,
        account_type: str = "live",
        computed_at_kst: str | None = None,
    ) -> dict[str, Any]:
        parsed = report_uuid if isinstance(report_uuid, UUID) else UUID(str(report_uuid))
        loader = self._baseline_loader or self._default_baseline_loader
        baseline = await loader(parsed)
        if baseline is None:
            return {"success": False, "error": "baseline_not_found"}

        market = baseline["market"]
        symbols = baseline["symbols"]
        market_snapshot = baseline["market_snapshot"]
        baseline_pnl = baseline["baseline_pnl"]
        unavailable: dict[str, str] = {}

        levels_delta: dict[str, Any] | None = None
        try:
            journal_fn = self._journal_fn or _default_journal_fn
            journal_result = await journal_fn(account_type=account_type, market=market)
            levels_delta = _levels_delta(journal_result, symbols, near_pct=near_pct)
        except Exception as exc:  # noqa: BLE001 — fail-open per signal
            logger.info("levels_delta failed: %r", exc)
            unavailable["levels"] = _reason(exc)

        holdings_pnl_delta: dict[str, Any] | None = None
        if baseline_pnl is None:
            unavailable["holdings"] = "baseline_snapshot_absent"
        else:
            try:
                holdings_fn = self._holdings_fn or _default_holdings_fn
                holdings_result = await holdings_fn(market=market)
                holdings_pnl_delta = _holdings_pnl_delta(baseline_pnl, holdings_result)
            except Exception as exc:  # noqa: BLE001 — fail-open per signal
                logger.info("holdings_pnl_delta failed: %r", exc)
                unavailable["holdings"] = _reason(exc)

        index_delta: dict[str, Any] | None = None
        baseline_indices = _baseline_indices(market_snapshot)
        if baseline_indices is None:
            unavailable["index"] = "baseline_snapshot_absent"
        else:
            try:
                market_index_fn = self._market_index_fn or _default_market_index_fn
                index_result = await market_index_fn()
                index_delta = _index_delta(baseline_indices, index_result)
            except Exception as exc:  # noqa: BLE001 — fail-open per signal
                logger.info("index_delta failed: %r", exc)
                unavailable["index"] = _reason(exc)

        out: dict[str, Any] = {
            "success": True,
            "baseline_report_uuid": str(parsed),
            "market": market,
            "levels_delta": levels_delta,
            "holdings_pnl_delta": holdings_pnl_delta,
            "index_delta": index_delta,
        }
        if computed_at_kst is not None:
            out["computed_at_kst"] = computed_at_kst
        if unavailable:
            out["unavailable"] = unavailable
        return out

    async def _default_baseline_loader(self, report_uuid: UUID) -> dict[str, Any] | None:
        from app.services.investment_reports.query_service import (
            InvestmentReportQueryService,
        )
        from app.services.investment_snapshots.repository import (
            InvestmentSnapshotsRepository,
        )

        query_service = InvestmentReportQueryService(self._session)
        bundle = await query_service.get_bundle(report_uuid)
        if bundle is None:
            return None
        report = bundle["report"]
        symbols = {
            item.symbol
            for item in (bundle.get("items") or [])
            if getattr(item, "symbol", None)
        }
        baseline_pnl: dict[str, float] | None = None
        bundle_uuid = getattr(report, "snapshot_bundle_uuid", None)
        if bundle_uuid is not None:
            snapshots_repo = InvestmentSnapshotsRepository(self._session)
            snapshot_bundle = await snapshots_repo.get_bundle_by_uuid(bundle_uuid)
            if snapshot_bundle is not None:
                pairs = await snapshots_repo.list_bundle_items_with_snapshots(
                    snapshot_bundle.id
                )
                baseline_pnl = _baseline_pnl_from_bundle_pairs(pairs)
        return {
            "market": report.market,
            "symbols": symbols,
            "market_snapshot": report.market_snapshot or {},
            "baseline_pnl": baseline_pnl,
        }


async def _default_journal_fn(*, account_type: str, market: str) -> dict[str, Any]:
    from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

    return await get_trade_journal(
        enrich_live=True, account_type=account_type, market=market
    )


async def _default_holdings_fn(*, market: str) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl

    return await _get_holdings_impl(market=market, include_current_price=True)


async def _default_market_index_fn() -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals._market_index import (
        handle_get_market_index,
    )

    return await handle_get_market_index()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_delta_service.py -q`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/delta_service.py tests/services/investment_reports/test_delta_service.py
git commit -m "feat(ROB-376): DeltaService orchestrator with injectable I/O + fail-open

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: DB-integration test for the default baseline loader

**Files:**
- Test: `tests/services/investment_reports/test_delta_service_db.py`

This exercises the real `_default_baseline_loader` against a seeded report row (no bundle),
verifying market/symbols extraction, `baseline_pnl=None` when no bundle, and `baseline_not_found`.
Per the ROB-375 follow-up, investment-report DB tests serialize under the cleanup-lock fixture to
avoid the xdist shared-Postgres deadlock.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_reports/test_delta_service_db.py
"""ROB-376 — default baseline loader against a seeded report row (no bundle)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.delta_service import DeltaService
from app.services.investment_reports.repository import InvestmentReportsRepository

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


@pytest.mark.asyncio
async def test_default_loader_reads_report_market_and_marks_pnl_absent(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rob376:delta:1",
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="HERMES_ADVISOR",
        title="baseline",
        summary="s",
        status="published",
        report_metadata={},
        market_snapshot={"baseline": {"indices": {"^GSPC": {"current": 5500.0}}}},
        portfolio_snapshot={},
    )
    await session.commit()

    # Inject only the live fns so no network is hit; loader is the real default.
    async def journal_fn(*, account_type, market):
        return {"entries": []}

    async def holdings_fn(*, market):
        return {"accounts": []}

    async def index_fn():
        return {"indices": [{"symbol": "^GSPC", "current": 5533.0}]}

    svc = DeltaService(
        session=session,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    )
    out = await svc.compute_delta(report.report_uuid)
    assert out["success"] is True
    assert out["market"] == "us"
    # No snapshot_bundle_uuid on the seeded row -> per-symbol P/L baseline absent.
    assert out["holdings_pnl_delta"] is None
    assert out["unavailable"]["holdings"] == "baseline_snapshot_absent"
    # Index baseline IS present on the row -> index delta computed.
    assert round(out["index_delta"]["entries"][0]["change_pct"], 4) == 0.6


@pytest.mark.asyncio
async def test_default_loader_baseline_not_found(session: AsyncSession) -> None:
    svc = DeltaService(session=session)
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out == {"success": False, "error": "baseline_not_found"}
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/services/investment_reports/test_delta_service_db.py -q`
Expected: PASS (2 passed) — the implementation from Task 4 already supports this. If the
`session` fixture or `investment_reports_cleanup_lock` fixture name differs, check
`tests/conftest.py` and adjust the import/fixture name. (Both are confirmed present in
`tests/conftest.py`.)

- [ ] **Step 3: Commit**

```bash
git add tests/services/investment_reports/test_delta_service_db.py
git commit -m "test(ROB-376): DB-integration test for default delta baseline loader

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: MCP handler + registration

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (add impl ~after line 448;
  register in `register_investment_report_tools` ~line 760; add name to
  `INVESTMENT_REPORT_TOOL_NAMES` line 50-58 and to `__all__` line 766+)
- Test: `tests/mcp_server/test_investment_report_delta_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp_server/test_investment_report_delta_tool.py
"""ROB-376 — investment_report_delta_get handler + registration."""

from __future__ import annotations

import pytest

import app.mcp_server.tooling.investment_reports_handlers as handlers


def test_delta_tool_name_registered():
    assert "investment_report_delta_get" in handlers.INVESTMENT_REPORT_TOOL_NAMES


def test_register_investment_report_tools_includes_delta():
    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)
            return lambda fn: fn

    handlers.register_investment_report_tools(_FakeMCP())
    assert "investment_report_delta_get" in registered


@pytest.mark.asyncio
async def test_delta_impl_invalid_uuid_returns_error():
    out = await handlers.investment_report_delta_get_impl(report_uuid="not-a-uuid")
    assert out == {"success": False, "error": "invalid_report_uuid"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_investment_report_delta_tool.py -q`
Expected: FAIL — `investment_report_delta_get` not in the name set / no
`investment_report_delta_get_impl` attribute.

- [ ] **Step 3: Write minimal implementation**

In `app/mcp_server/tooling/investment_reports_handlers.py`:

(3a) Add the name to the set (line 50-58):

```python
INVESTMENT_REPORT_TOOL_NAMES: set[str] = {
    "investment_report_create",
    "investment_report_list",
    "investment_report_get",
    "investment_report_decide_item",
    "investment_report_activate_watch",
    "investment_report_context_get",
    "investment_report_delta_get",
    "investment_report_generate_from_bundle",
}
```

(3b) Add the handler (immediately after `investment_report_context_get_impl`, ~line 449).
`now_kst` is the project KST clock used elsewhere in the tooling layer:

```python
# ---------------------------------------------------------------------------
# investment_report_delta_get (ROB-376)
# ---------------------------------------------------------------------------
async def investment_report_delta_get_impl(
    report_uuid: str,
    near_pct: float = 1.0,
    account_type: str = "live",
) -> dict:
    from app.core.timezone import now_kst
    from app.services.investment_reports.delta_service import DeltaService

    try:
        parsed = UUID(report_uuid)
    except (ValueError, AttributeError, TypeError):
        return {"success": False, "error": "invalid_report_uuid"}

    async with AsyncSessionLocal() as db:
        service = DeltaService(db)
        return await service.compute_delta(
            parsed,
            near_pct=near_pct,
            account_type=account_type,
            computed_at_kst=now_kst().isoformat(),
        )
```

> CONFIRMED: `now_kst` lives at `app/core/timezone.py:14`; `trade_journal_tools.py:15` imports it
> as `from app.core.timezone import now_kst`. Use exactly that import.

(3c) Register it (in `register_investment_report_tools`, after the `context_get` block ~line 759):

```python
    mcp.tool(
        name="investment_report_delta_get",
        description=(
            "Read-only intraday delta vs a baseline report. Given report_uuid "
            "(the open/prior report), returns three deterministic deltas for Hermes "
            "to compose: levels_delta (journal target/stop touch x live), "
            "holdings_pnl_delta (per-symbol live P/L vs the baseline snapshot "
            "bundle's portfolio P/L), and index_delta (live index vs the report's "
            "frozen market baseline). Per-signal fail-open: a degraded signal is "
            "null with a reason under 'unavailable'; missing data is never coerced "
            "to zero. No broker/order/watch mutation."
        ),
    )(investment_report_delta_get_impl)
```

(3d) Add to `__all__` (line 766+), keeping alphabetical-ish order near the other impls:

```python
    "investment_report_delta_get_impl",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_investment_report_delta_tool.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_delta_tool.py
git commit -m "feat(ROB-376): register investment_report_delta_get MCP tool

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Lint, format, typecheck, full-suite gate, final commit

**Files:** none (verification only).

- [ ] **Step 1: Format + lint (exact CI commands)**

Run:
```bash
uv run ruff format app/ tests/
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
```
Expected: `ruff format --check` reports "X files already formatted"; `ruff check` reports
"All checks passed!". Fix any reported issue and re-run.

- [ ] **Step 2: Typecheck the new module**

Run: `uv run ty check app/services/investment_reports/delta_service.py`
Expected: no errors. (If `ty` flags the injected-callable defaults, ensure the type hints match
the `Callable[..., Awaitable[...]]` signatures above.)

- [ ] **Step 3: Run the full new-test set**

Run:
```bash
uv run pytest tests/services/investment_reports/test_delta_service.py \
  tests/services/investment_reports/test_delta_service_db.py \
  tests/mcp_server/test_investment_report_delta_tool.py -q
```
Expected: all pass (16 total).

- [ ] **Step 4: Import-guard sanity (no in-process LLM / broker mutation pulled in)**

Run: `uv run python -c "import app.services.investment_reports.delta_service"`
Expected: imports cleanly with no heavy/broker/LLM side-effect (defaults import lazily inside fns).

- [ ] **Step 5: Final commit (if Step 1 reformatted anything)**

```bash
git add -A
git commit -m "chore(ROB-376): format + lint pass for delta tool

Co-Authored-By: Paperclip <noreply@paperclip.ing>" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** levels_delta (Task 1), holdings_pnl_delta from bundle baseline (Task 2),
  index_delta from report baseline (Task 3), orchestrator + fail-open + baseline_not_found
  (Task 4), default loader DB path (Task 5), MCP tool surface + registration + invalid_uuid
  (Task 6), CI gates (Task 7). All §3 contract fields and §5 error rows are covered.
- **Non-goals honored:** no `intraday_update` policy, no screener/news signals, no new migration,
  no persisted per-report level snapshot, MCP-only (no HTTP).
- **Type consistency:** `_levels_delta(journal_result, symbols, *, near_pct)`,
  `_holdings_pnl_delta(baseline_pnl, holdings_result)`,
  `_baseline_pnl_from_bundle_pairs(pairs)`, `_index_delta(baseline_indices, market_index_result)`,
  `_baseline_indices(market_snapshot)`, `DeltaService.compute_delta(report_uuid, *, near_pct,
  account_type, computed_at_kst)` — names/signatures consistent across tasks and tests.
- **All imports pinned:** `now_kst` = `app.core.timezone` (confirmed); the `session` and
  `investment_reports_cleanup_lock` fixtures are globally registered via
  `tests/conftest.py` `pytest_plugins = [..., "tests._investment_reports_helpers"]` (confirmed).
  No open confirmations remain.
