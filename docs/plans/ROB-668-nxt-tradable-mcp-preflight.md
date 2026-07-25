# NXT Tradability MCP Exposure + Session-Aware Order Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Surface per-symbol NXT tradability (`nxt_tradable` + source + asof) in read MCP tools and add an env-gated, warn-first, session-aware order preflight so an agent is flagged *before* the broker returns a blind 422 `market-not-supported-for-stock` during an NXT session.

**Architecture:** A pure verdict helper (`app/services/nxt_preflight.py`) maps `(KrTossSession) × (nxt_eligible, nxt_trading_suspended)` to an `ok|block` verdict with alternatives, fail-open when the session is unknown. A batchable read accessor in `kr_symbol_universe_service.py` returns the already-persisted `KRSymbolUniverse.nxt_eligible/nxt_trading_suspended/toss_master_updated_at` data — **no new storage**. The verdict + tradability are threaded into `get_quote`/`search_symbol`/`analyze_stock_batch` (read exposure, KR-only, US/crypto no-op) and into `toss_preview_order` (always warn) / `toss_place_order` (block only in `required` mode) behind a new `TOSS_NXT_PREFLIGHT_MODE` env gate.

**Tech Stack:** Python 3.13, uv, pytest (markers: unit), SQLAlchemy async, pydantic-settings, FastMCP tool registry.

## Global Constraints

Every task implicitly includes these:

- **Migration-0.** No new DB columns/enums. `KRSymbolUniverse.nxt_eligible` (NOT NULL bool), `nxt_trading_suspended` (nullable bool), `toss_master_updated_at`, `updated_at` already exist (`app/models/kr_symbol_universe.py:34,54,56,62`). Do NOT run `alembic revision`.
- **Fail-open on unknown session.** `evaluate_nxt_preflight(session=None, ...)` MUST return `block=False` (advisory only). A hard block when the Toss calendar is down would freeze all KR trading. `get_kr_toss_session_from_toss` already returns `None` when `TOSS_API_ENABLED` is off or the calendar is unavailable (`app/services/brokers/toss/market_calendar.py:226`).
- **Warn-first rollout.** `TOSS_NXT_PREFLIGHT_MODE ∈ {off, optional, warn, required}`, default `warn`. Only `required` may fail-close a live `place`. `off` disables all preflight. Mirror the existing `toss_approval_hash_mode` enum validator (`app/core/config.py:364`).
- **KR-only.** US/crypto branches of every touched tool are no-ops (no `nxt_*` fields, no preflight).
- **Field naming is deliberate — do NOT "fix" it back.** This plan exposes `nxt_tradable` / `nxt_tradable_source` / `nxt_tradable_asof` (+ `nxt_tradable_stale`), not the Linear issue's illustrative `nxt_flag_source` / `nxt_flag_asof`. The `nxt_tradable_*` prefix keeps the source/asof attached to the derived tradability bit the tools actually surface. Acceptance only requires a flag + source + asof triple (any consistent naming), so the executor MUST keep `nxt_tradable_*` verbatim across code, tests, and runbook and NOT rename to `nxt_flag_*`.
- **No broker mutation added.** The block is a local pre-`client.place_order` return; it is placed alongside the existing `_opposite_pending_error` / `_sell_loss_guard` guards inside `execute_order`.
- **Do NOT touch** `app/services/brokers/kis/live_order_expiry.py` or `app/mcp_server/tooling/kis_live_ledger.py` (ROB-671 territory).
- **route_request stays deterministic** — no session/time input added to `route_request_lanes.py` or its registration. Session-awareness is confined to `toss_preview_order` + `suggest_order_account`.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Commit after each task.

---

## File Structure

**Created**
- `app/services/nxt_preflight.py` — pure helper: `NxtTradability` dataclass (with `nxt_tradable` derivation + `is_stale` + `public_fields`), `NxtPreflightVerdict` dataclass, `evaluate_nxt_preflight`, alternative constants.
- `tests/services/test_nxt_preflight.py` — session × eligibility verdict matrix + staleness + fail-open.
- `tests/test_kr_nxt_tradability_accessor.py` — accessor + `search_kr_symbols` nxt fields (DB).
- `tests/test_config_nxt_preflight_mode.py` — env-gate enum validation.
- `tests/test_nxt_preflight_order_tools.py` — preview warns / place blocks (required) / 422 mapping / suggest_order_account advisory.

**Modified**
- `app/core/config.py` — add `toss_nxt_preflight_mode: str = "warn"` + extend the approval-hash-mode validator field list.
- `app/services/kr_symbol_universe_service.py` — add `get_kr_nxt_tradability` accessor + `_get_nxt_tradability_impl`; add nxt fields to `_search_kr_symbols_impl` rows.
- `app/mcp_server/tooling/market_data_quotes.py` — annotate KR `_get_quote_impl` quote with nxt public fields.
- `app/mcp_server/tooling/analysis_analyze.py` — annotate `_resolve_kr_quote` with nxt public fields.
- `app/mcp_server/tooling/analysis_tool_handlers.py` — copy nxt fields from quote into `_summarize_analysis_result`.
- `app/mcp_server/tooling/orders_toss_variants.py` — `_nxt_preflight_context` helper; preview warning + `nxt_preflight` field; place required-mode block; 422 mapping in `_toss_error_response`.
- `app/mcp_server/tooling/account_routing_tools.py` — advisory nxt fields + `nxt_preflight` on `suggest_order_account_impl` (KR only).
- `tests/test_mcp_quotes_tools.py` — assert KR `get_quote` carries nxt fields + US no-op.
- `docs/runbooks/toss-live-smoke.md` — staleness + fail-open + rollout-mode note.

---

## Task 1 — Pure NXT preflight helper (verdict matrix, tradability, staleness)

**Files:**
- Create `app/services/nxt_preflight.py`
- Test: `tests/services/test_nxt_preflight.py`

**Interfaces:**
- Consumes: `KrTossSession` (`Literal["nxt_premarket","regular","nxt_after","closed"]`) from `app/services/brokers/toss/market_calendar.py:14`.
- Produces:
  - `NxtTradability(nxt_eligible: bool, nxt_trading_suspended: bool | None, asof: datetime | None, source: str = "kr_symbol_universe")` with `.nxt_tradable -> bool`, `.is_stale(now=None) -> bool`, `.public_fields(now=None) -> dict`.
  - `evaluate_nxt_preflight(session: KrTossSession | None, tradability: NxtTradability) -> NxtPreflightVerdict`.
  - `NxtPreflightVerdict(block, reason, session, alternatives, advisory)` with `.to_dict()`.
  - Constants `RETRY_AT_REGULAR = "retry_at_regular"`, `ROUTE_VIA_KIS = "route_via_kis"`.

Steps:

- [ ] Write failing test `tests/services/test_nxt_preflight.py`:
```python
from __future__ import annotations

import datetime as dt

import pytest

from app.services.nxt_preflight import (
    ROUTE_VIA_KIS,
    RETRY_AT_REGULAR,
    NxtTradability,
    evaluate_nxt_preflight,
)

_KST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 7, 3, 8, 30, tzinfo=_KST)


def _trad(eligible: bool, suspended: bool | None = None, asof=_NOW) -> NxtTradability:
    return NxtTradability(
        nxt_eligible=eligible, nxt_trading_suspended=suspended, asof=asof
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "session,eligible,suspended,expect_block,expect_reason",
    [
        ("nxt_premarket", False, None, True, "not_nxt_eligible"),
        ("nxt_after", False, None, True, "not_nxt_eligible"),
        ("nxt_premarket", True, True, True, "nxt_trading_suspended"),
        ("nxt_after", True, True, True, "nxt_trading_suspended"),
        ("nxt_premarket", True, None, False, None),
        ("nxt_after", True, False, False, None),
        ("regular", False, None, False, None),
        ("closed", False, None, False, None),
    ],
)
def test_verdict_matrix(session, eligible, suspended, expect_block, expect_reason):
    verdict = evaluate_nxt_preflight(session, _trad(eligible, suspended))
    assert verdict.block is expect_block
    assert verdict.reason == expect_reason
    if expect_block:
        assert verdict.alternatives == (RETRY_AT_REGULAR, ROUTE_VIA_KIS)
        assert verdict.advisory is False
    else:
        assert verdict.alternatives == ()


@pytest.mark.unit
def test_fail_open_when_session_unavailable():
    verdict = evaluate_nxt_preflight(None, _trad(False))
    assert verdict.block is False
    assert verdict.advisory is True
    assert verdict.reason == "nxt_session_unavailable"
    assert verdict.session is None


@pytest.mark.unit
def test_nxt_tradable_and_stale_and_public_fields():
    fresh = _trad(True, None, asof=_NOW)
    assert fresh.nxt_tradable is True
    assert fresh.is_stale(now=_NOW) is False
    stale = _trad(True, None, asof=_NOW - dt.timedelta(days=5))
    assert stale.is_stale(now=_NOW) is True
    missing = _trad(True, None, asof=None)
    assert missing.is_stale(now=_NOW) is True
    fields = fresh.public_fields(now=_NOW)
    assert fields == {
        "nxt_tradable": True,
        "nxt_tradable_source": "kr_symbol_universe",
        "nxt_tradable_asof": _NOW.isoformat(),
        "nxt_tradable_stale": False,
    }
    # suspended overrides eligible
    assert _trad(True, True).nxt_tradable is False
```

- [ ] Run it — fails: `uv run pytest tests/services/test_nxt_preflight.py -v` → expected `ModuleNotFoundError: No module named 'app.services.nxt_preflight'`.

- [ ] Minimal impl — create `app/services/nxt_preflight.py`:
```python
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.services.brokers.toss.market_calendar import KrTossSession

RETRY_AT_REGULAR = "retry_at_regular"
ROUTE_VIA_KIS = "route_via_kis"

_KST = dt.timezone(dt.timedelta(hours=9))
_NXT_SESSIONS: frozenset[str] = frozenset({"nxt_premarket", "nxt_after"})
# ROB-668: the toss_master_updated_at flag is refreshed by the operator sync
# (scripts/sync_kr_symbol_universe.py). Treat anything older than this as stale
# so the caller can decide whether to trust the eligibility bit.
NXT_FLAG_STALE_AFTER = dt.timedelta(days=2)


@dataclass(frozen=True)
class NxtTradability:
    nxt_eligible: bool
    nxt_trading_suspended: bool | None
    asof: dt.datetime | None
    source: str = "kr_symbol_universe"

    @property
    def nxt_tradable(self) -> bool:
        return self.nxt_eligible and self.nxt_trading_suspended is not True

    def is_stale(self, *, now: dt.datetime | None = None) -> bool:
        if self.asof is None:
            return True
        current = now or dt.datetime.now(_KST)
        asof = self.asof if self.asof.tzinfo is not None else self.asof.replace(
            tzinfo=_KST
        )
        return (current - asof) > NXT_FLAG_STALE_AFTER

    def public_fields(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        return {
            "nxt_tradable": self.nxt_tradable,
            "nxt_tradable_source": self.source,
            "nxt_tradable_asof": self.asof.isoformat() if self.asof is not None else None,
            "nxt_tradable_stale": self.is_stale(now=now),
        }


@dataclass(frozen=True)
class NxtPreflightVerdict:
    block: bool
    reason: str | None
    session: KrTossSession | None
    alternatives: tuple[str, ...]
    advisory: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "reason": self.reason,
            "session": self.session,
            "alternatives": list(self.alternatives),
            "advisory": self.advisory,
        }


def evaluate_nxt_preflight(
    session: KrTossSession | None,
    tradability: NxtTradability,
) -> NxtPreflightVerdict:
    """Map (session) × (nxt_eligible, nxt_trading_suspended) -> verdict.

    Fail-open: session None (Toss calendar unavailable) -> advisory, never block.
    regular/closed -> ok (KRX path handles routing). Block only when the current
    session is an NXT window AND the symbol is not NXT-tradable.
    """
    if session is None:
        return NxtPreflightVerdict(
            block=False,
            reason="nxt_session_unavailable",
            session=None,
            alternatives=(),
            advisory=True,
        )
    if session not in _NXT_SESSIONS:
        return NxtPreflightVerdict(
            block=False, reason=None, session=session, alternatives=(), advisory=False
        )
    if tradability.nxt_tradable:
        return NxtPreflightVerdict(
            block=False, reason=None, session=session, alternatives=(), advisory=False
        )
    reason = (
        "nxt_trading_suspended"
        if tradability.nxt_trading_suspended is True
        else "not_nxt_eligible"
    )
    return NxtPreflightVerdict(
        block=True,
        reason=reason,
        session=session,
        alternatives=(RETRY_AT_REGULAR, ROUTE_VIA_KIS),
        advisory=False,
    )
```

- [ ] Run it — passes: `uv run pytest tests/services/test_nxt_preflight.py -v` → all tests green.

- [ ] Commit: `git commit -am "feat(ROB-668): pure NXT preflight helper (verdict matrix + tradability + staleness)"`

---

## Task 2 — Env gate `TOSS_NXT_PREFLIGHT_MODE`

**Files:**
- Modify `app/core/config.py:247` (near `toss_approval_hash_mode`) and `app/core/config.py:364` (validator field list)
- Test: `tests/test_config_nxt_preflight_mode.py`

**Interfaces:**
- Produces: `settings.toss_nxt_preflight_mode: str` (default `"warn"`), validated to `{off, optional, warn, required}`.

Steps:

- [ ] Write failing test `tests/test_config_nxt_preflight_mode.py`:
```python
from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_default_mode_is_warn():
    assert Settings().toss_nxt_preflight_mode == "warn"


@pytest.mark.unit
def test_mode_normalized_case_insensitive():
    assert Settings(toss_nxt_preflight_mode=" Required ").toss_nxt_preflight_mode == (
        "required"
    )


@pytest.mark.unit
def test_invalid_mode_fails_loud():
    with pytest.raises(ValueError, match="approval hash mode"):
        Settings(toss_nxt_preflight_mode="blocky")
```

- [ ] Run it — fails: `uv run pytest tests/test_config_nxt_preflight_mode.py -v` → `test_default_mode_is_warn` fails with `AttributeError`/default `optional` mismatch (attribute absent).

- [ ] Minimal impl — in `app/core/config.py`, after line 251 (`order_approval_hash_mode: str = "optional"`) add:
```python
    # ROB-668 — session-aware NXT order preflight rollout level.
    # off | optional | warn | required. Default warn-first: preview always warns,
    # place blocks a live send only when set to 'required'.
    toss_nxt_preflight_mode: str = "warn"
```
Then extend the validator field list at `app/core/config.py:364`:
```python
    @field_validator(
        "toss_approval_hash_mode",
        "order_approval_hash_mode",
        "toss_nxt_preflight_mode",
        mode="before",
    )
```
(The existing `_validate_approval_hash_mode` body already enforces `{off, optional, warn, required}` and normalizes case/whitespace — reuse it verbatim.)

> **Note (intentional shared message):** the shared `_validate_approval_hash_mode` raises `ValueError("approval hash mode must be one of ...")` even for a bad `TOSS_NXT_PREFLIGHT_MODE` value. That wording is generic-but-accurate (the enum is identical), so `test_invalid_mode_fails_loud` intentionally matches the shared substring `"approval hash mode"` rather than a field-specific string. Do NOT split the validator or rewrite the message for a per-field string — reusing the one validator is the whole point; the shared message is acceptable.

- [ ] Run it — passes: `uv run pytest tests/test_config_nxt_preflight_mode.py -v` → green.

- [ ] Commit: `git commit -am "feat(ROB-668): TOSS_NXT_PREFLIGHT_MODE env gate (default warn)"`

---

## Task 3 — Batchable tradability accessor + `search_kr_symbols` nxt fields

**Files:**
- Modify `app/services/kr_symbol_universe_service.py` (add accessor after `is_nxt_eligible` at line 379; extend `_search_kr_symbols_impl` row dict at line 420; extend `__all__` at line 503)
- Test: `tests/test_kr_nxt_tradability_accessor.py`

**Interfaces:**
- Consumes: `NxtTradability` from `app/services/nxt_preflight.py`; `KRSymbolUniverse` columns `nxt_eligible`, `nxt_trading_suspended`, `toss_master_updated_at`, `updated_at`.
- Produces:
  - `get_kr_nxt_tradability(symbols: list[str], db: AsyncSession | None = None) -> dict[str, NxtTradability]`.
  - `_search_kr_symbols_impl` rows now include `nxt_tradable`, `nxt_tradable_source`, `nxt_tradable_asof`, `nxt_tradable_stale`.

Steps:

- [ ] Write failing test `tests/test_kr_nxt_tradability_accessor.py`:
```python
from __future__ import annotations

import datetime as dt

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.kr_symbol_universe_service import (
    get_kr_nxt_tradability,
    search_kr_symbols,
)

_KST = dt.timezone(dt.timedelta(hours=9))


@pytest.mark.asyncio
async def test_accessor_returns_tradability(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol="777001",
            name="엔엑스티가능",
            exchange="KOSPI",
            is_active=True,
            nxt_eligible=True,
            nxt_trading_suspended=False,
            toss_master_updated_at=dt.datetime(2026, 7, 3, 6, 0, tzinfo=_KST),
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="777002",
            name="엔엑스티불가",
            exchange="KOSDAQ",
            is_active=True,
            nxt_eligible=False,
            nxt_trading_suspended=None,
        )
    )
    await db_session.flush()

    result = await get_kr_nxt_tradability(["777001", "777002", "777999"], db=db_session)
    assert result["777001"].nxt_tradable is True
    assert result["777001"].source == "kr_symbol_universe"
    assert result["777001"].asof is not None
    assert result["777002"].nxt_tradable is False
    assert "777999" not in result  # missing symbol omitted
    await db_session.rollback()


@pytest.mark.asyncio
async def test_search_rows_carry_nxt_fields(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol="777003",
            name="검색엔엑스티",
            exchange="KOSPI",
            is_active=True,
            nxt_eligible=True,
            nxt_trading_suspended=False,
            toss_master_updated_at=dt.datetime(2026, 7, 3, 6, 0, tzinfo=_KST),
        )
    )
    await db_session.flush()
    rows = await search_kr_symbols("검색엔엑스티", 10, db=db_session)
    assert rows
    row = next(r for r in rows if r["symbol"] == "777003")
    assert row["nxt_tradable"] is True
    assert row["nxt_tradable_source"] == "kr_symbol_universe"
    assert "nxt_tradable_asof" in row
    assert "nxt_tradable_stale" in row
    await db_session.rollback()
```

- [ ] Run it — fails: `uv run pytest tests/test_kr_nxt_tradability_accessor.py -v` → `ImportError: cannot import name 'get_kr_nxt_tradability'`.

- [ ] Minimal impl — in `app/services/kr_symbol_universe_service.py`:
  1. Add import near line 15: `from app.services.nxt_preflight import NxtTradability`
  2. After `is_nxt_eligible` (line 379) add:
```python
async def _get_nxt_tradability_impl(
    db: AsyncSession,
    symbols: list[str],
) -> dict[str, NxtTradability]:
    unique = sorted({s for s in symbols if s})
    if not unique:
        return {}
    stmt = select(
        KRSymbolUniverse.symbol,
        KRSymbolUniverse.nxt_eligible,
        KRSymbolUniverse.nxt_trading_suspended,
        KRSymbolUniverse.toss_master_updated_at,
        KRSymbolUniverse.updated_at,
    ).where(
        KRSymbolUniverse.symbol.in_(unique),
        KRSymbolUniverse.is_active.is_(True),
    )
    rows = (await db.execute(stmt)).all()
    return {
        row.symbol: NxtTradability(
            nxt_eligible=bool(row.nxt_eligible),
            nxt_trading_suspended=row.nxt_trading_suspended,
            asof=row.toss_master_updated_at or row.updated_at,
        )
        for row in rows
    }


async def get_kr_nxt_tradability(
    symbols: list[str],
    db: AsyncSession | None = None,
) -> dict[str, NxtTradability]:
    """Return {symbol: NxtTradability} for the given KR symbol codes.

    Missing or inactive symbols are omitted. asof = toss_master_updated_at when
    present else updated_at. KR-only; callers no-op for US/crypto.
    """
    if not symbols:
        return {}
    if db is not None:
        return await _get_nxt_tradability_impl(db, symbols)
    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        return await _get_nxt_tradability_impl(session, symbols)
    finally:
        await session.close()
```
  3. In `_search_kr_symbols_impl` (line 420), replace the row dict comprehension with one that merges the public fields:
```python
    return [
        {
            "symbol": row.symbol,
            "name": row.name,
            "instrument_type": "equity_kr",
            "exchange": row.exchange,
            "is_active": row.is_active,
            **NxtTradability(
                nxt_eligible=bool(row.nxt_eligible),
                nxt_trading_suspended=row.nxt_trading_suspended,
                asof=row.toss_master_updated_at or row.updated_at,
            ).public_fields(),
        }
        for row in rows
    ]
```
  4. Add `"get_kr_nxt_tradability"` to `__all__` (line 503).

- [ ] Run it — passes: `uv run pytest tests/test_kr_nxt_tradability_accessor.py -v` → green.

- [ ] Commit: `git commit -am "feat(ROB-668): get_kr_nxt_tradability accessor + nxt fields in search_kr_symbols rows"`

---

## Task 4 — Expose nxt fields in `get_quote` (KR branch, US no-op)

**Files:**
- Modify `app/mcp_server/tooling/market_data_quotes.py` (import at ~line 62; KR branch of `_get_quote_impl` at lines 1216-1234)
- Test: extend `tests/test_mcp_quotes_tools.py`

**Interfaces:**
- Consumes: `get_kr_nxt_tradability` from `app.services.kr_symbol_universe_service`.
- Produces: KR `get_quote` payload gains `nxt_tradable`, `nxt_tradable_source`, `nxt_tradable_asof`, `nxt_tradable_stale`. US/crypto unchanged.

Steps:

- [ ] Write failing test — append to `tests/test_mcp_quotes_tools.py`:
```python
@pytest.mark.asyncio
async def test_get_quote_kr_exposes_nxt_tradable(monkeypatch):
    import datetime as dt

    from app.mcp_server.tooling import market_data_quotes
    from app.services.nxt_preflight import NxtTradability

    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    async def fake_tradability(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=dt.datetime(2026, 7, 3, 6, 0, tzinfo=dt.timezone.utc),
            )
        }

    monkeypatch.setattr(
        market_data_quotes, "get_kr_nxt_tradability", fake_tradability
    )

    result = await tools["get_quote"]("005930")
    assert result["nxt_tradable"] is True
    assert result["nxt_tradable_source"] == "kr_symbol_universe"
    assert result["nxt_tradable_asof"] is not None


@pytest.mark.asyncio
async def test_get_quote_us_has_no_nxt_fields(monkeypatch):
    tools = build_tools()

    async def fake_fast_info(_symbol):
        return {"close": 100.0, "previous_close": 99.0}

    monkeypatch.setattr(yahoo_service, "fetch_fast_info", fake_fast_info)
    result = await tools["get_quote"]("AAPL", "us")
    assert "nxt_tradable" not in result
```

- [ ] Run it — fails: `uv run pytest tests/test_mcp_quotes_tools.py -v -k nxt_tradable` → KeyError `nxt_tradable`.

- [ ] Minimal impl — in `app/mcp_server/tooling/market_data_quotes.py`:
  1. **Do NOT add a second import line** — `search_kr_symbols` is already imported at line 62 (`from app.services.kr_symbol_universe_service import search_kr_symbols`). Adding a fresh `from ... import get_kr_nxt_tradability, search_kr_symbols` would re-import `search_kr_symbols` and trip ruff `F811` (redefinition) / `make lint`. Instead, append `get_kr_nxt_tradability` to the **existing** line so it reads:
```python
from app.services.kr_symbol_universe_service import (
    get_kr_nxt_tradability,
    search_kr_symbols,
)
```
  2. In `_get_quote_impl`, in the `equity_kr` path (after `quote = await _fetch_quote_equity_kr(symbol)` at line 1223) annotate the quote before the NXT overlay/return, so both the overlay-return and the plain return carry it:
```python
        quote = await _fetch_quote_equity_kr(symbol)
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        session = await _nxt_quote_session(data_state)
```
  (Keep the rest of the branch unchanged — the overlay `quote.update(overlay)` and the two `return quote` statements now both include the nxt fields.)

- [ ] Run it — passes: `uv run pytest tests/test_mcp_quotes_tools.py -v -k nxt_tradable` → green. Full file regression: `uv run pytest tests/test_mcp_quotes_tools.py -v`.

- [ ] Commit: `git commit -am "feat(ROB-668): expose nxt_tradable+source+asof in get_quote KR branch"`

---

## Task 5 — Expose nxt fields in analyze (`_resolve_kr_quote` + `_summarize_analysis_result`)

**Files:**
- Modify `app/mcp_server/tooling/analysis_analyze.py` (`_resolve_kr_quote` at lines 105-130; import near line 39)
- Modify `app/mcp_server/tooling/analysis_tool_handlers.py` (`_summarize_analysis_result` at lines 766-784)
- Test: extend `tests/test_nxt_preflight_order_tools.py` (created in Task 7) OR a focused test here.

**Interfaces:**
- Consumes: `get_kr_nxt_tradability`.
- Produces: KR analyze quote carries nxt public fields; `_summarize_analysis_result` copies `nxt_tradable`/`nxt_tradable_source`/`nxt_tradable_asof`/`nxt_tradable_stale` from `analysis["quote"]` into the compact summary.

Steps:

- [ ] Write failing test `tests/test_analyze_nxt_exposure.py`:
```python
from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


@pytest.mark.unit
def test_summary_copies_nxt_fields_from_quote():
    analysis = {
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {
            "price": 70000,
            "nxt_tradable": True,
            "nxt_tradable_source": "kr_symbol_universe",
            "nxt_tradable_asof": "2026-07-03T06:00:00+09:00",
            "nxt_tradable_stale": False,
        },
    }
    summary = _summarize_analysis_result("005930", analysis)
    assert summary["nxt_tradable"] is True
    assert summary["nxt_tradable_source"] == "kr_symbol_universe"
    assert summary["nxt_tradable_asof"] == "2026-07-03T06:00:00+09:00"


@pytest.mark.unit
def test_summary_us_has_no_nxt_fields():
    analysis = {
        "market_type": "equity_us",
        "source": "yahoo",
        "quote": {"price": 100.0},
    }
    summary = _summarize_analysis_result("AAPL", analysis)
    assert "nxt_tradable" not in summary
```

- [ ] Run it — fails: `uv run pytest tests/test_analyze_nxt_exposure.py -v` → KeyError `nxt_tradable`.

- [ ] Minimal impl:
  1. In `app/mcp_server/tooling/analysis_analyze.py` add import near line 39: `from app.services.kr_symbol_universe_service import get_kr_nxt_tradability`. Then in `_resolve_kr_quote`, annotate both the live and fallback quote just before each `return`:
```python
async def _resolve_kr_quote(
    symbol: str, ohlcv_df: pd.DataFrame
) -> dict[str, Any] | None:
    trading_date = datetime.now(_KST).date()

    async def _annotate(quote: dict[str, Any]) -> dict[str, Any]:
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        return quote

    live = await _fetch_kr_live_quote(symbol)
    if live is not None:
        as_of_raw = live.get("price_as_of")
        as_of_dt = datetime.fromisoformat(as_of_raw) if as_of_raw else None
        live["is_stale_price"] = compute_is_stale(
            "price", as_of_dt, trading_date=trading_date
        )
        return await _annotate(live)

    fallback = _build_kr_quote_from_ohlcv(symbol, ohlcv_df)
    if fallback is None:
        return None
    last_idx = ohlcv_df.index[-1]
    as_of_dt = pd.Timestamp(last_idx).to_pydatetime()
    fallback["price_as_of"] = as_of_dt.isoformat()
    fallback["is_stale_price"] = compute_is_stale(
        "price", as_of_dt, trading_date=trading_date
    )
    return await _annotate(fallback)
```
  2. In `app/mcp_server/tooling/analysis_tool_handlers.py`, in `_summarize_analysis_result`, after the `summary` dict is built (line 777, before the `position_index` block) copy the nxt fields:
```python
    for _nxt_key in (
        "nxt_tradable",
        "nxt_tradable_source",
        "nxt_tradable_asof",
        "nxt_tradable_stale",
    ):
        if _nxt_key in quote:
            summary[_nxt_key] = quote[_nxt_key]
```

- [ ] Run it — passes: `uv run pytest tests/test_analyze_nxt_exposure.py -v` → green.

- [ ] Commit: `git commit -am "feat(ROB-668): thread nxt_tradable into analyze quote + compact summary"`

---

## Task 6 — Preview warning + suggest_order_account advisory (session-aware, non-blocking)

**Files:**
- Modify `app/mcp_server/tooling/orders_toss_variants.py` (add helper + imports near line 45; preview wiring in `toss_preview_order` at lines 767-818)
- Modify `app/mcp_server/tooling/account_routing_tools.py` (`suggest_order_account_impl` at lines 76-90; imports at top)
- Test: `tests/test_nxt_preflight_order_tools.py`

**Interfaces:**
- Consumes: `get_kr_toss_session_from_toss` (`app/services/brokers/toss/market_calendar.py:226`), `get_kr_nxt_tradability`, `evaluate_nxt_preflight`.
- Produces:
  - New `_nxt_preflight_context(symbol, market, *, now=None) -> tuple[NxtPreflightVerdict, NxtTradability] | None` in `orders_toss_variants.py` (returns `None` when market != "kr" or mode == "off").
  - `toss_preview_order` response gains `nxt_preflight` (verdict dict) and appends `"nxt_session_not_tradable"` to `order_warnings` when `verdict.block`.
  - `suggest_order_account_impl` KR result gains nxt public fields + `nxt_preflight` (advisory).

Steps:

- [ ] Write failing test `tests/test_nxt_preflight_order_tools.py`:
```python
from __future__ import annotations

import datetime as dt

import pytest

from app.mcp_server.tooling import orders_toss_variants as otv
from app.services.nxt_preflight import NxtTradability


@pytest.fixture
def _toss_enabled(monkeypatch):
    monkeypatch.setattr(
        otv, "validate_toss_api_config", lambda: [], raising=True
    )


@pytest.mark.asyncio
async def test_preview_warns_on_nxt_non_eligible(monkeypatch, _toss_enabled):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)

    async def fake_session(_moment):
        return "nxt_premarket"

    async def fake_trad(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=False, nxt_trading_suspended=None, asof=None
            )
        }

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)

    # Neutralize network-touching preview helpers.
    async def _no_price_ctx(client, symbol):
        return None, None, None

    monkeypatch.setattr(otv, "_preview_price_context", _no_price_ctx)

    class _Guard:
        ok = True
        warnings: list = []
        error_message = None

    async def _no_warnings(client, symbol, *, market, side):
        return _Guard()

    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)

    res = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="market", quantity=1
    )
    assert res["success"] is True
    assert "nxt_session_not_tradable" in res["order_warnings"]
    assert res["nxt_preflight"]["block"] is True
    assert "retry_at_regular" in res["nxt_preflight"]["alternatives"]


@pytest.mark.asyncio
async def test_preflight_context_none_when_off(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "off", raising=False)
    assert await otv._nxt_preflight_context("005930", "kr") is None


@pytest.mark.asyncio
async def test_preflight_context_none_for_us(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)
    assert await otv._nxt_preflight_context("AAPL", "us") is None


@pytest.mark.asyncio
async def test_preflight_fail_open_when_session_none(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)

    async def fake_session(_moment):
        return None

    async def fake_trad(symbols, db=None):
        return {symbols[0]: NxtTradability(False, None, None)}

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)
    verdict, _ = await otv._nxt_preflight_context("005930", "kr")
    assert verdict.block is False
    assert verdict.advisory is True


@pytest.mark.asyncio
async def test_suggest_order_account_kr_carries_nxt_advisory(monkeypatch):
    """suggest_order_account_impl (KR) exposes nxt public fields + an advisory
    nxt_preflight/session block; US path stays clean (covers the File Structure
    'suggest_order_account advisory' coverage claim)."""
    from app.mcp_server.tooling import account_routing_tools as art

    # Neutralize the pricing/capital/holdings dependencies.
    async def _fake_resolve_price(symbol, market, price):
        return 70000.0, "test"

    async def _fake_capital(*, include_manual=False):
        return {}

    async def _fake_holdings(*, market, include_current_price, minimum_value):
        return []

    async def _fake_user_setting(_key):
        return {}

    monkeypatch.setattr(art, "_resolve_price", _fake_resolve_price)
    monkeypatch.setattr(art, "get_available_capital_impl", _fake_capital)
    monkeypatch.setattr(art, "_get_holdings_impl", _fake_holdings)
    monkeypatch.setattr(art, "get_user_setting", _fake_user_setting)
    monkeypatch.setattr(
        art, "suggest_account_from_snapshot", lambda _inp: {"account_mode": "toss_live"}
    )

    async def _fake_session(_moment):
        return "nxt_premarket"

    async def _fake_trad(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=False, nxt_trading_suspended=None, asof=None
            )
        }

    monkeypatch.setattr(art, "get_kr_toss_session_from_toss", _fake_session)
    monkeypatch.setattr(art, "get_kr_nxt_tradability", _fake_trad)

    result = await art.suggest_order_account_impl(
        symbol="005930", market="kr", side="buy", quantity=1
    )
    assert result["nxt_tradable"] is False
    assert result["nxt_tradable_source"] == "kr_symbol_universe"
    assert result["nxt_preflight"]["block"] is True
    assert result["nxt_preflight"]["session"] == "nxt_premarket"

    # US path: no nxt fields, no preflight.
    us_result = await art.suggest_order_account_impl(
        symbol="AAPL", market="us", side="buy", quantity=1, usd_krw=1350.0
    )
    assert "nxt_tradable" not in us_result
    assert "nxt_preflight" not in us_result
```

- [ ] Run it — fails: `uv run pytest tests/test_nxt_preflight_order_tools.py -v` → `AttributeError: module ... has no attribute '_nxt_preflight_context'` (and `test_suggest_order_account_kr_carries_nxt_advisory` fails with `KeyError: 'nxt_preflight'` once the context helper exists but the account-routing wiring is absent).

- [ ] Minimal impl — in `app/mcp_server/tooling/orders_toss_variants.py`:
  1. Add imports near line 45:
```python
from app.services.brokers.toss.market_calendar import get_kr_toss_session_from_toss
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.nxt_preflight import (
    NxtPreflightVerdict,
    NxtTradability,
    evaluate_nxt_preflight,
)
```
  2. Add the helper (near the other module-level helpers, e.g. after `_infer_market`):
```python
async def _nxt_preflight_context(
    symbol: str,
    market: Literal["kr", "us"],
    *,
    now: datetime | None = None,
) -> tuple[NxtPreflightVerdict, NxtTradability] | None:
    """KR-only session-aware NXT preflight. None when market != 'kr' or mode off.

    Fail-open: get_kr_toss_session_from_toss returns None when the Toss calendar
    is unavailable -> evaluate_nxt_preflight yields an advisory (non-blocking)
    verdict.
    """
    if market != "kr":
        return None
    mode = getattr(settings, "toss_nxt_preflight_mode", "warn")
    if mode == "off":
        return None
    moment = now or now_kst()
    session = await get_kr_toss_session_from_toss(moment)
    tradability = (await get_kr_nxt_tradability([symbol])).get(symbol) or NxtTradability(
        nxt_eligible=False, nxt_trading_suspended=None, asof=None
    )
    verdict = evaluate_nxt_preflight(session, tradability)
    return verdict, tradability
```
  3. In `toss_preview_order`, after `order_warnings.extend(fill_warnings)` (line 767) compute the preflight and stash the payload:
```python
    nxt_preflight_payload: dict[str, Any] | None = None
    preflight = await _nxt_preflight_context(symbol, mkt)
    if preflight is not None:
        verdict, _ = preflight
        nxt_preflight_payload = verdict.to_dict()
        if verdict.block:
            order_warnings.append("nxt_session_not_tradable")
```
  Then add `"nxt_preflight": nxt_preflight_payload,` to the `response` dict (line 797-813 block, e.g. right after `"sector_concentration": sector_conc,`).
  4. In `app/mcp_server/tooling/account_routing_tools.py`, add imports:
```python
from app.core.timezone import now_kst
from app.services.brokers.toss.market_calendar import get_kr_toss_session_from_toss
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.nxt_preflight import evaluate_nxt_preflight
```
  and after `result["price_source"] = price_source` (line 89) add the KR-only advisory block:
```python
    if normalized_market == "kr":
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            result.update(tradability.public_fields())
            session = await get_kr_toss_session_from_toss(now_kst())
            result["nxt_preflight"] = evaluate_nxt_preflight(
                session, tradability
            ).to_dict()
    return result
```
  (Replace the existing `return result` at line 90 with the block above.)

- [ ] Run it — passes: `uv run pytest tests/test_nxt_preflight_order_tools.py -v` → green.

- [ ] Commit: `git commit -am "feat(ROB-668): NXT preflight warning in toss_preview_order + suggest_order_account advisory"`

---

## Task 7 — `place` required-mode fail-closed block + 422 error mapping

**Files:**
- Modify `app/mcp_server/tooling/orders_toss_variants.py` (`execute_order` inside `_toss_place_order_impl` at lines 1043-1131; `_toss_error_response` at lines 597-613)
- Test: extend `tests/test_nxt_preflight_order_tools.py`

**Interfaces:**
- Consumes: `_nxt_preflight_context`, `TossApiResponseError` (already imported).
- Produces:
  - `execute_order` returns `{success:false, error_code:"nxt_session_not_tradable", session, alternatives}` before `client.place_order` when mode == "required" AND `verdict.block`. In warn/optional it logs but proceeds; in off it never runs.
  - `_toss_error_response` maps a `TossApiResponseError` with `envelope.code == "market-not-supported-for-stock"` to add `error_code`, `alternatives`, and a `hint`.

Steps:

- [ ] Write failing test — append to `tests/test_nxt_preflight_order_tools.py`:
```python
from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope


@pytest.mark.asyncio
async def test_place_blocks_in_required_mode(monkeypatch, _toss_enabled):
    monkeypatch.setattr(
        otv.settings, "toss_nxt_preflight_mode", "required", raising=False
    )
    monkeypatch.setattr(
        otv.settings, "toss_live_order_mutations_enabled", True, raising=False
    )

    async def fake_session(_moment):
        return "nxt_after"

    async def fake_trad(symbols, db=None):
        return {symbols[0]: NxtTradability(False, None, None)}

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)

    placed = {"called": False}

    class _Client:
        async def place_order(self, payload):
            placed["called"] = True
            raise AssertionError("place_order must not be reached")

    # sell-loss/opposite guards are buy-side-skippable; drive a buy market order.
    async def _no_warnings(client, symbol, *, market, side):
        class _G:
            ok = True
            warnings: list = []
            error_message = None

        return _G()

    async def _no_opp(client, symbol, side, base):
        return None

    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)
    monkeypatch.setattr(otv, "_opposite_pending_error", _no_opp)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield _Client()

    monkeypatch.setattr(otv, "_client_context", _ctx)

    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="market",
        quantity=1,
        dry_run=False,
        confirm=True,
    )
    assert res["success"] is False
    assert res["error_code"] == "nxt_session_not_tradable"
    assert res["session"] == "nxt_after"
    assert "route_via_kis" in res["alternatives"]
    assert placed["called"] is False


@pytest.mark.unit
def test_error_response_maps_market_not_supported():
    envelope = TossErrorEnvelope(
        request_id="rq1",
        code="market-not-supported-for-stock",
        message="not supported",
        data=None,
    )
    exc = TossApiResponseError(envelope, status_code=422)
    out = otv._toss_error_response(exc, {"source": "toss"})
    assert out["error_code"] == "nxt_session_not_tradable"
    assert "route_via_kis" in out["alternatives"]
    assert "hint" in out
```

- [ ] Run it — fails: `uv run pytest tests/test_nxt_preflight_order_tools.py -v -k "required_mode or market_not_supported"` → the place order reaches `place_order` (AssertionError) / KeyError `error_code`.

- [ ] Minimal impl — in `app/mcp_server/tooling/orders_toss_variants.py`:
  1. In `execute_order`, after the sell-loss guard block (line 1071, before `res = None`) add:
```python
        # Guard: NXT session preflight. Required mode fail-closes before POST;
        # warn/optional log but proceed (fail-open on unknown session).
        preflight = await _nxt_preflight_context(symbol, mkt)
        if preflight is not None:
            verdict, _ = preflight
            if verdict.block:
                mode = getattr(settings, "toss_nxt_preflight_mode", "warn")
                if mode == "required":
                    return {
                        "success": False,
                        **base_response,
                        "error": (
                            f"NXT session {verdict.session!r} does not support "
                            f"{symbol} ({verdict.reason}); order not sent."
                        ),
                        "error_code": "nxt_session_not_tradable",
                        "session": verdict.session,
                        "alternatives": list(verdict.alternatives),
                    }
                logger.warning(
                    "NXT preflight advisory (mode=%s): symbol=%s session=%s "
                    "reason=%s — proceeding with live send",
                    mode,
                    symbol,
                    verdict.session,
                    verdict.reason,
                )
```
  2. Add a module constant near line 65 (`_PRICE_CONTEXT_UNAVAILABLE`):
```python
_MARKET_NOT_SUPPORTED_CODE = "market-not-supported-for-stock"
```
  3. Extend `_toss_error_response` (line 597) — in the `isinstance(exc, TossApiResponseError)` branch, build the dict into a variable, augment it, then return:
```python
def _toss_error_response(exc: Exception, base: dict[str, Any]) -> dict[str, Any]:
    if isinstance(exc, TossApiResponseError):
        payload = {
            "success": False,
            **base,
            "error": str(exc),
            "status_code": exc.status_code,
            "code": exc.envelope.code,
            "request_id": exc.envelope.request_id,
            "message": exc.envelope.message,
            "data": exc.envelope.data,
        }
        if exc.envelope.code == _MARKET_NOT_SUPPORTED_CODE:
            payload["error_code"] = "nxt_session_not_tradable"
            payload["alternatives"] = [RETRY_AT_REGULAR, ROUTE_VIA_KIS]
            payload["hint"] = (
                "Symbol is not tradable in the current NXT session. Retry during "
                "the KRX regular session, or route via KIS SOR."
            )
        return payload
    return {
        "success": False,
        **base,
        "error": f"{type(exc).__name__}: {exc}",
    }
```
  4. Add `RETRY_AT_REGULAR, ROUTE_VIA_KIS` to the `nxt_preflight` import added in Task 6:
```python
from app.services.nxt_preflight import (
    ROUTE_VIA_KIS,
    RETRY_AT_REGULAR,
    NxtPreflightVerdict,
    NxtTradability,
    evaluate_nxt_preflight,
)
```

- [ ] Run it — passes: `uv run pytest tests/test_nxt_preflight_order_tools.py -v` → all green. Regression: `uv run pytest tests/test_toss_live_order_mutation_safety.py -v`.

- [ ] Commit: `git commit -am "feat(ROB-668): required-mode NXT block before place + 422 market-not-supported mapping"`

---

## Task 8 — Runbook note (staleness + fail-open + rollout modes) & full regression

**Files:**
- Modify `docs/runbooks/toss-live-smoke.md` (append a `## ROB-668 NXT preflight` section)
- No test (docs); final full regression run.

Steps:

- [ ] Append to `docs/runbooks/toss-live-smoke.md`:
```markdown
## ROB-668 — NXT tradability exposure + session-aware preflight

- **Read exposure (KR only):** `get_quote`, `search_symbol`, `analyze_stock_batch`
  each carry `nxt_tradable` (bool), `nxt_tradable_source` (`kr_symbol_universe`),
  `nxt_tradable_asof` (ISO, `toss_master_updated_at` else `updated_at`), and
  `nxt_tradable_stale` (bool). US/crypto payloads carry none of these.
- **Staleness:** the flag is refreshed by the operator sync
  (`scripts/sync_kr_symbol_universe.py`). `nxt_tradable_stale=true` means asof is
  missing or older than 2 days (`NXT_FLAG_STALE_AFTER`); re-run the sync before
  trusting eligibility during an NXT window.
- **Rollout gate `TOSS_NXT_PREFLIGHT_MODE ∈ {off, optional, warn, required}`**
  (default `warn`):
  - `off` — no preflight anywhere.
  - `optional`/`warn` — `toss_preview_order` appends `nxt_session_not_tradable`
    to `order_warnings` and returns a structured `nxt_preflight`; `toss_place_order`
    logs but does NOT block (`warn` logs a live-send advisory).
  - `required` — `toss_place_order` fail-closes with
    `{success:false, error_code:"nxt_session_not_tradable", session, alternatives}`
    before `client.place_order`.
- **Fail-open:** when the Toss market calendar is unavailable
  (`TOSS_API_ENABLED` off or fetch failure), `get_kr_toss_session_from_toss`
  returns `None`, the verdict is advisory (`block=false, advisory=true`), and
  KR trading is never frozen.
- **Alternatives** on a block: `retry_at_regular` (KRX regular session) and
  `route_via_kis` (KIS domestic order sets `EXCG_ID_DVSN_CD='SOR'` for
  NXT-eligible symbols; see `app/services/brokers/kis/domestic_orders.py`).
- **Belt-and-suspenders:** any preflight miss still surfaces the broker 422
  `market-not-supported-for-stock` as a typed
  `error_code:"nxt_session_not_tradable"` with the same alternatives.
- `route_request` is intentionally NOT session-aware (deterministic contract
  preserved).
```

- [ ] Run full regression for touched surfaces:
```
uv run pytest tests/services/test_nxt_preflight.py tests/test_config_nxt_preflight_mode.py tests/test_kr_nxt_tradability_accessor.py tests/test_analyze_nxt_exposure.py tests/test_nxt_preflight_order_tools.py tests/test_mcp_quotes_tools.py -v
```
Expected: all pass.

- [ ] `make lint` → clean.

- [ ] Commit: `git commit -am "docs(ROB-668): NXT preflight runbook (staleness, fail-open, rollout modes)"`

---

## Self-Review

**Spec-coverage → task mapping (acceptance criteria):**

- **(a) NXT-session buy of a non-eligible symbol is flagged by MCP (warn or block per mode) BEFORE the broker 422, with alternatives:**
  - Verdict core → Task 1 (`evaluate_nxt_preflight` block matrix + alternatives).
  - Preview warn → Task 6 (`toss_preview_order` appends `nxt_session_not_tradable` + `nxt_preflight`).
  - Place block (required) → Task 7 (fail-closed envelope before `client.place_order`).
  - Belt-and-suspenders 422 mapping → Task 7 (`_toss_error_response`).
- **(b) get_quote / search_symbol / analyze_stock_batch expose nxt_tradable + source + asof for KR:**
  - Accessor + search rows → Task 3.
  - get_quote → Task 4.
  - analyze (quote + compact summary) → Task 5.
- **(c) staleness/stale handling of the flag documented:**
  - `is_stale` + `nxt_tradable_stale` field → Task 1/Task 3; documented → Task 8.
- **(d) fail-open when session unavailable:**
  - `evaluate_nxt_preflight(None, ...)` advisory → Task 1; `_nxt_preflight_context` fail-open path → Task 6; place proceeds (not required) / advisory → Task 7; documented → Task 8.

**Env gate** (`TOSS_NXT_PREFLIGHT_MODE`, default warn) → Task 2, consumed in Tasks 6 & 7.

## Out of scope

- **`route_request` session-awareness** — deliberately excluded to preserve the documented deterministic (no time input) contract of `route_request_lanes.py` / `route_request_registration.py`.
- **`kis_live_place_order` / KIS domestic order NXT block** — the KIS SOR path (`EXCG_ID_DVSN_CD='SOR'`) already exists (`app/services/brokers/kis/domestic_orders.py`) and is the `route_via_kis` alternative; adding a KIS-side preflight block is a follow-up.
- **`app/services/brokers/kis/live_order_expiry.py`, `app/mcp_server/tooling/kis_live_ledger.py`** — ROB-671 territory; not touched.
- **New DB columns / migrations** — none; all fields already persisted (migration-0).
- **US/crypto `nxt_*` exposure** — no-op by design (NXT is a KR venue).
- **Auto-refresh of the `kr_symbol_universe` NXT flags** — remains an operator-run sync (`scripts/sync_kr_symbol_universe.py`); this plan only reads and stale-tags.
- **Live-market smoke validation** of the block during a real NXT window — operator step after merge.
