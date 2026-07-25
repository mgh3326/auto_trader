# Toss `sellable_quantity` N+1 Fanout — Caller-Scoped Skip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Kill the dominant latency bottleneck across four Sentry transactions (`/invest/api/home`, `/invest/api/account-panel`, `get_holdings`, `analyze_stock_batch`) by making `fetch_toss_portfolio_snapshot()` **not** fan out one `GET /api/v1/sellable-quantity` per holding when the caller does not consume the value. Today every one of ~47 holdings issues a `TossApiGroup.ORDER_INFO` request whose process-global rate limiter caps at 6 TPS (3 TPS during 09:00–09:10 KST), so the `asyncio.gather` serializes to ~6/sec → 13–17 s of pure wall-time. In the default config (`TOSS_LIVE_ORDER_MUTATIONS_ENABLED` off) the invest_home reader computes `_toss_sellable_quantity()` → `0.0` and **discards** the fetched value, and the `analyze_stock_batch` position index never reads sellable at all — so the fanout is wasted work. We thread a `need_sellable` flag (**default `True`**) through the shared fetch chain and pass `False` from exactly the callers that provably do not consume the value, without ever reading a global feature flag inside the shared function.

**Architecture:** Current (slow) flow — `fetch_toss_portfolio_snapshot()` (`app/services/toss_portfolio_service.py:131`) unconditionally builds `[active_client.sellable_quantity(symbol=item.symbol) for item in holdings.items]` and `asyncio.gather`s it inside the `invest.home.toss_api.sellable_quantity` span (`toss_portfolio_service.py:148-167`); each call routes through `TossReadClient.sellable_quantity` (`app/services/brokers/toss/client.py:310-319`, `group=TossApiGroup.ORDER_INFO`) whose shared `TossRateLimiter.acquire` (`app/services/brokers/toss/rate_limiter.py:57-73`) admits only `_BASE_LIMITS[ORDER_INFO]=6` per sliding second (`rate_limiter.py:35`), dropping to `3` in the 09:00–09:10 peak window (`rate_limiter.py:52-55`). Two hot callers reach this function: the invest_home reader `TossApiHomeReader.fetch` (`app/services/invest_home_readers.py:554`) which then throws the value away via `_toss_sellable_quantity(position, mutations_enabled)` returning `0.0` when mutations are off (`invest_home_readers.py:516-524`), and the MCP `_collect_toss_api_positions` → `_collect_portfolio_positions` chain (`app/mcp_server/tooling/portfolio_holdings.py:557,876`), one branch of which feeds `analyze_stock_batch`'s `_build_batch_position_index` (`app/mcp_server/tooling/analysis_tool_handlers.py:642-693`) that indexes only `account/qty/avg_buy_price/pnl_pct/order_routable` and never reads `sellable_quantity`.

Target flow — `fetch_toss_portfolio_snapshot(*, need_sellable: bool = True, client=None)` branches: when `need_sellable` is `True` it behaves exactly as today (gather + span + zip); when `False` it skips the `ORDER_INFO` fanout entirely and builds every `TossPortfolioPosition` with `sellable_quantity=None`. The flag is threaded caller-scoped: `_collect_toss_api_positions(market_filter, *, need_sellable=True)` → `_collect_portfolio_positions(..., need_sellable=True)` forward it (both default `True`), `_build_batch_position_index` passes `need_sellable=False` (never reads sellable), and `TossApiHomeReader.fetch` passes `need_sellable=mutations_enabled` (i.e. `False` when the flag is off, since the value would be discarded). The MCP `get_holdings`/`_toss_api_position_to_mcp` path and every action_report/recommend consumer keep the default `True`, so sellable stays in MCP output and sell classification is untouched. Net: home + account-panel (mutations off) and `analyze_stock_batch` drop the 13–17 s serial prefix; the Toss 6-TPS cap is never touched.

**Tech Stack:** Python 3.13, uv, pytest (`@pytest.mark.asyncio`, marker `unit`), asyncio, SQLAlchemy async (unchanged here), httpx (Toss transport, unchanged), pydantic/dataclass DTOs, Sentry spans. Toss Open API `GET /api/v1/sellable-quantity` (`TossApiGroup.ORDER_INFO`, 6 TPS / 3 TPS peak).

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **CALLER-SCOPED SKIP ONLY — do NOT read the global TOSS_LIVE_ORDER_MUTATIONS_ENABLED inside the shared fetch_toss_portfolio_snapshot / _collect_toss_api_positions / _collect_portfolio_positions.** The MCP path _toss_api_position_to_mcp (portfolio_holdings.py ~543) AND sell classification (action_report auto_emit / action_verdict, portfolio_action_service: sellable_quantity>0 → sell_review) consume sellable_quantity UNCONDITIONALLY. The new need_sellable parameter MUST default True; only _build_batch_position_index and the invest_home readers (when mutations off) pass False. A global-flag skip would silently break sell classification and drop sellable from MCP holdings output.
- sellable-quantity is a read-only GET; no broker/order mutation is on this path.
- migration-0 (no DB change).
- **Never raise the ORDER_INFO 6 TPS cap** (3 TPS in the 09:00–09:10 KST peak window). It is a broker constraint; increasing it risks 429s. `app/services/brokers/toss/rate_limiter.py` is NOT edited by this plan.
- **The shared functions default `need_sellable=True`.** Behavior for every un-migrated caller (MCP `get_holdings`, `_toss_api_position_to_mcp`, `analysis_recommend`, `portfolio_allocation`, `fundamentals/_news`, `portfolio_rotation_service`) is byte-for-byte identical to today.
- Read-only path: no broker / order / watch / order-intent mutation is introduced or reached.
- Run tests with `uv run pytest <path> -v`. Lint with `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## Approach / decision note

- **This plan implements the skip only — no cache, no precompute.** For the mutations-ON future (where sellable *is* consumed and the fanout is real work), the lower-risk follow-up is **lazy per-symbol load on the sell modal / a Toss bulk endpoint**, NOT a stateful short-TTL Redis cache: sellable_quantity changes intraday as orders fill, so a cache invites staleness/invalidation bugs on a value that gates real sell sizing. That work is deferred to **ROB-686** and is explicitly out of scope here. (Recorded in key_decisions.)
- **`account_seq` / account resolution (ROB-687) is not in this plan's path.** The skip does not touch account resolution; `fetch_toss_portfolio_snapshot` still builds its client via `TossReadClient.from_settings()` exactly as today.
- **The MCP `get_holdings` tool keeps the fetch (default `True`).** Per the caller-scoped constraint, `_get_holdings_impl` and `_toss_api_position_to_mcp` continue to emit `sellable_quantity`, so the `get_holdings` Sentry transaction is intentionally NOT sped up by this change (its output and downstream sell classification genuinely read the value). The dominant win — `analyze_stock_batch` (3,882 sellable calls / 7d) plus home + account-panel — is captured. (Recorded in open_questions for reviewer sign-off.)

---

## File Structure

| File | Create/Modify | Responsibility (which Task) |
|------|---------------|-----------------------------|
| `app/services/toss_portfolio_service.py` | Modify | Task 1 — add `need_sellable: bool = True` to `fetch_toss_portfolio_snapshot`; skip the `ORDER_INFO` gather + span when `False`, build positions with `sellable_quantity=None`. |
| `app/mcp_server/tooling/portfolio_holdings.py` | Modify | Task 2 — thread `need_sellable` (default `True`) through `_collect_toss_api_positions` and `_collect_portfolio_positions`; forward to the toss fetch. |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | Modify | Task 2 — `_build_batch_position_index` passes `need_sellable=False`. |
| `app/services/invest_home_readers.py` | Modify | Task 3 — `TossApiHomeReader.fetch` passes `need_sellable=mutations_enabled` to the shared fetch. |
| `tests/test_toss_portfolio_service.py` | Modify | Task 1 tests (add skip cases to the existing file). |
| `tests/mcp_server/tooling/test_toss_sellable_need_flag.py` | Create | Task 2 tests (batch-index passes `False`; collect forwards the flag; default stays `True`). |
| `tests/test_invest_home_readers.py` | Modify | Task 3 tests (new assertions + update the 3 existing Toss-reader fakes to accept `need_sellable`). |

> **NOT touched:**
> - `app/services/brokers/toss/rate_limiter.py` and `.../client.py` — the 6-TPS `ORDER_INFO` cap and the `sellable_quantity` GET are unchanged; we reduce *how often* it is called, never *how fast*.
> - `app/mcp_server/tooling/portfolio_holdings.py::_toss_api_position_to_mcp` (~`:520-545`) and `_get_holdings_impl` (~`:1022`) — keep default `True`; MCP holdings output keeps `sellable_quantity`.
> - `app/services/action_report/**` (`auto_emit.py`, `action_verdict.py`), `app/services/portfolio_action_service.py`, `app/services/portfolio_action_classifier.py`, `app/mcp_server/tooling/analysis_recommend.py`, `app/mcp_server/tooling/portfolio_allocation.py`, `app/mcp_server/tooling/fundamentals/_news.py`, `app/services/portfolio_rotation_service.py` — all consume `_collect_portfolio_positions` at its default (`True`); sell classification (`sellable_quantity > 0 → sell_review`) is byte-for-byte unchanged.
> - `app/services/invest_home_service.py::_sellable_quantity` and `build_grouped_holdings` — the invest_home grouping already coerces toss sellable to `0.0` when mutations are off, so skipping the fetch changes nothing downstream of the reader.
> - No Redis cache / precompute (ROB-686), no `account_seq` change (ROB-687), no DB migration.

---

## Task 1 — `fetch_toss_portfolio_snapshot(need_sellable=True)` skips the ORDER_INFO fanout (migration-0)

**Files:**
- Modify `app/services/toss_portfolio_service.py` — `fetch_toss_portfolio_snapshot` def at `:131`; the sellable gather + span at `:148-167`; the position-build `zip` loop at `:169-203`.
- Test (modify) `tests/test_toss_portfolio_service.py` — add skip cases beside the existing `test_fetch_toss_portfolio_snapshot_*` (the `_FakeTossClient` records `sellable_calls`).

**Interfaces:**
- Produces: `async def fetch_toss_portfolio_snapshot(*, need_sellable: bool = True, client: TossPortfolioClient | None = None) -> TossPortfolioSnapshot`. When `need_sellable=False`: no `sellable_quantity` call is issued, the `invest.home.toss_api.sellable_quantity` span is skipped, and every returned `TossPortfolioPosition.sellable_quantity is None`. Holdings fetch, cash snapshot, KR/US mapping, and error semantics are unchanged.
- Consumes: `TossPortfolioClient.sellable_quantity(*, symbol)` (only when `need_sellable=True`), `active_client.holdings()`, `fetch_toss_cash_snapshot(client=...)`.

Steps:

- [ ] **Write failing test — skip issues zero sellable calls and yields `sellable_quantity=None`.** Append to `tests/test_toss_portfolio_service.py`:
```python
@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_skips_sellable_when_not_needed() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client, need_sellable=False)

    # ROB-685: the ORDER_INFO N+1 fanout is skipped entirely.
    assert client.sellable_calls == []
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].sellable_quantity is None
    # Holdings + cash still resolve normally.
    assert snapshot.positions[0].symbol == "BRK.B"
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.errors == []


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_default_still_fetches_sellable() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    # Default is unchanged: sellable is fetched and mapped.
    assert client.sellable_calls == ["BRK.B"]
    assert snapshot.positions[0].sellable_quantity == Decimal("1.25")
```

- [ ] **Run it — fails.** `uv run pytest tests/test_toss_portfolio_service.py -v -k "skips_sellable or default_still_fetches"`
  Expected: `test_..._skips_sellable_when_not_needed` FAILS — today `fetch_toss_portfolio_snapshot` has no `need_sellable` kwarg (`TypeError: unexpected keyword argument`). The `default_still_fetches` test passes today (locks the default so the refactor cannot regress it).

- [ ] **Minimal impl — branch on `need_sellable`.** In `app/services/toss_portfolio_service.py`, change the signature at `:131`:
```python
async def fetch_toss_portfolio_snapshot(
    *,
    need_sellable: bool = True,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
```
Replace the sellable gather span (`:148-167`) + the `zip(holdings.items, sellable_results, ...)` loop head (`:169-170`) with a branch that produces `(item, sellable_result)` pairs either way:
```python
        errors: list[dict[str, Any]] = []

        if need_sellable:
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                sellable_results = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=item.symbol)
                        for item in holdings.items
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(
                        1
                        for result in sellable_results
                        if isinstance(result, BaseException)
                    ),
                )
            paired: list[tuple[Any, Any]] = list(
                zip(holdings.items, sellable_results, strict=True)
            )
        else:
            # ROB-685: caller does not consume sellable_quantity — skip the
            # per-holding GET /sellable-quantity (ORDER_INFO, 6 TPS) fanout that
            # otherwise serializes to ~6/sec and dominates wall time.
            paired = [(item, None) for item in holdings.items]

        positions: list[TossPortfolioPosition] = []
        for item, sellable_result in paired:
            sellable_quantity: Decimal | None = None
            if isinstance(sellable_result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "sellable_quantity",
                        "symbol": item.symbol,
                        "error": str(sellable_result),
                    }
                )
            elif sellable_result is not None:
                sellable_quantity = sellable_result.sellable_quantity
            ...  # rest of the position append block is UNCHANGED
```
Leave the `TossPortfolioPosition(...)` construction, cash snapshot (`:205-206`), return (`:208-213`), and `finally` client-close (`:214-216`) exactly as-is.

- [ ] **Run it — passes.** `uv run pytest tests/test_toss_portfolio_service.py -v` → all pass (new skip cases + the 4 pre-existing cases, incl. `..._keeps_position_when_sellable_fails` which still exercises the `isinstance(..., BaseException)` branch under the default `True`).

- [ ] **Regression — phase-span + reader suites unaffected by the default.** `uv run pytest tests/test_invest_home_readers.py -v -k "toss"`
  Expected: `test_toss_portfolio_snapshot_emits_phase_spans` still asserts `invest.home.toss_api.sellable_quantity in started` (default `True` keeps the span). The 3 `test_toss_api_home_reader_*` still pass — they monkeypatch the reader's `fetch_toss_portfolio_snapshot`, which Task 3 (not yet applied) does not change.

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-685): fetch_toss_portfolio_snapshot skips ORDER_INFO sellable fanout when need_sellable=False"`

---

## Task 2 — Thread `need_sellable` through the MCP collect chain; `analyze_stock_batch` passes `False` (migration-0)

**Files:**
- Modify `app/mcp_server/tooling/portfolio_holdings.py` — `_collect_toss_api_positions` def at `:548` (fetch call at `:557`); `_collect_portfolio_positions` def at `:781` (toss branch at `:871-878`).
- Modify `app/mcp_server/tooling/analysis_tool_handlers.py` — `_build_batch_position_index` at `:642`, its `_collect_portfolio_positions(...)` call at `:659-663`.
- Test (create) `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`.

**Interfaces:**
- Produces: `async def _collect_toss_api_positions(market_filter: str | None, *, need_sellable: bool = True) -> tuple[list[dict], list[dict], bool]` — forwards `need_sellable` to `fetch_toss_portfolio_snapshot`.
- Produces: `_collect_portfolio_positions(*, account, market, include_current_price, account_name=None, user_id=_MCP_USER_ID, is_mock=False, need_sellable: bool = True)` — forwards to `_collect_toss_api_positions(market_filter, need_sellable=need_sellable)`.
- Consumes: `_build_batch_position_index` calls `_collect_portfolio_positions(account=None, market=market, include_current_price=False, need_sellable=False)`.
- `_build_batch_position_index` imports `_collect_portfolio_positions` lazily inside the function (`analysis_tool_handlers.py:651-655`), so tests monkeypatch the attribute on `portfolio_holdings` (mirrors `tests/mcp_server/tooling/test_get_holdings_news.py:141`).

Steps:

- [ ] **Write failing test — batch index passes `False`, collect forwards the flag, default is `True`.** Create `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`:
```python
from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import analysis_tool_handlers, portfolio_holdings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_build_batch_position_index_requests_no_sellable(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return [], [], kwargs.get("market"), None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    index, err = await analysis_tool_handlers._build_batch_position_index("kr")

    assert err is None
    assert index == {}
    # ROB-685: the batch index never reads sellable_quantity.
    assert captured["need_sellable"] is False
    assert captured["include_current_price"] is False


async def test_collect_portfolio_positions_forwards_need_sellable_to_toss(monkeypatch):
    calls: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True):
        calls.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch
    )

    # Isolate the sibling collectors — with market=None, _collect_portfolio_positions
    # otherwise fans out to the REAL _collect_kis_positions / _collect_upbit_positions
    # (live KIS/Upbit HTTP) and _collect_manual_positions (AsyncSessionLocal DB). That
    # makes this a slow, non-hermetic test, and _collect_upbit_positions can surface
    # UpbitSymbolUniverseLookupError, which _collect_portfolio_positions RE-RAISES
    # (portfolio_holdings.py:857-858) → the test would crash before asserting. Stub
    # all three to empty so only the toss forwarding path is exercised. They are
    # module globals resolved at call time, so setattr on the module patches them.
    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    # Default path keeps sellable (MCP / sell-classification contract).
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    # Explicit opt-out threads through.
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, need_sellable=False
    )

    assert calls == [True, False]


async def test_collect_toss_api_positions_defaults_need_sellable_true(monkeypatch):
    seen: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True):
        seen.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch
    )

    await portfolio_holdings._collect_toss_api_positions(None)
    await portfolio_holdings._collect_toss_api_positions(None, need_sellable=False)

    assert seen == [True, False]
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py -v`
  Expected: all three FAIL — `_collect_portfolio_positions`/`_collect_toss_api_positions` have no `need_sellable` kwarg (`TypeError`), and `_build_batch_position_index` does not pass one (`KeyError: 'need_sellable'`).

- [ ] **Minimal impl part A — `_collect_toss_api_positions`.** In `app/mcp_server/tooling/portfolio_holdings.py:548`, add the kwarg and forward it:
```python
async def _collect_toss_api_positions(
    market_filter: str | None,
    *,
    need_sellable: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not bool(getattr(settings, "toss_api_enabled", False)):
        return [], [], False
    if market_filter == "crypto":
        return [], [], False

    try:
        snapshot = await fetch_toss_portfolio_snapshot(need_sellable=need_sellable)
    except Exception as exc:
        ...
```

- [ ] **Minimal impl part B — `_collect_portfolio_positions`.** In `portfolio_holdings.py:781`, add `need_sellable: bool = True` to the keyword-only signature, and at the toss branch (`:871-876`) forward it:
```python
    if bool(getattr(settings, "toss_api_enabled", False)):
        (
            toss_api_positions,
            toss_api_errors,
            toss_api_succeeded,
        ) = await _collect_toss_api_positions(market_filter, need_sellable=need_sellable)
```

- [ ] **Minimal impl part C — `_build_batch_position_index` opts out.** In `app/mcp_server/tooling/analysis_tool_handlers.py:659`:
```python
        positions, _errors, _market, _account = await _collect_portfolio_positions(
            account=None,
            market=market,
            include_current_price=False,
            need_sellable=False,
        )
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py -v` → 3 passed.

- [ ] **Regression — MCP holdings + batch + recommend suites keep default behavior.** `uv run pytest tests/test_mcp_portfolio_tools.py tests/mcp_server/tooling/test_get_holdings_news.py tests/test_mcp_holdings_rob562.py tests/test_mcp_recommend_flow.py -q`
  Expected: all pass — every un-migrated caller (`get_holdings`, recommend, allocation) still calls `_collect_portfolio_positions` at the default `need_sellable=True`, so `_toss_api_position_to_mcp` still emits `sellable_quantity` and sell classification is unchanged.

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-685): analyze_stock_batch position index skips toss sellable fetch (need_sellable=False)"`

---

## Task 3 — invest_home Toss reader passes `need_sellable=mutations_enabled` (migration-0)

**Files:**
- Modify `app/services/invest_home_readers.py` — `TossApiHomeReader.fetch` at `:539`; `mutations_enabled` resolved at `:547-549`; the snapshot fetch at `:554`.
- Test (modify) `tests/test_invest_home_readers.py` — add a new assertion test AND update the 3 existing Toss-reader fakes (`test_toss_api_home_reader_maps_read_only_holdings_and_cash` `:1523`, `..._tradeable_when_mutations_enabled` `:1587`, `..._converts_us_holdings_to_krw` `:1635`) so their `fake_fetch_toss_snapshot` accepts the new kwarg.

**Interfaces:**
- Consumes: `settings.toss_live_order_mutations_enabled` (already read at `invest_home_readers.py:547-549` into `mutations_enabled`).
- Produces: the sole `fetch_toss_portfolio_snapshot()` call at `:554` becomes `fetch_toss_portfolio_snapshot(need_sellable=mutations_enabled)` — when mutations are off (default) the ORDER_INFO fanout is skipped; the reader already collapses toss sellable to `0.0` via `_toss_sellable_quantity(position, mutations_enabled)` (`:516-524`), so output is unchanged.

Steps:

- [ ] **Write failing test — reader passes `need_sellable` = the mutation flag.** Add to `tests/test_invest_home_readers.py`:
```python
@pytest.mark.asyncio
@pytest.mark.parametrize("mutations,expected_need", [(False, False), (True, True)])
async def test_toss_api_home_reader_gates_sellable_fetch_on_mutations(
    monkeypatch, mutations, expected_need
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    captured: dict[str, bool] = {}

    async def fake_fetch_toss_snapshot(*, need_sellable: bool = True):
        captured["need_sellable"] = need_sellable
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    # ROB-685: mutations off (default) => reader discards sellable anyway => skip fetch.
    assert captured["need_sellable"] is expected_need
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_home_readers.py -v -k "gates_sellable_fetch"`
  Expected: both params FAIL — today `TossApiHomeReader.fetch` calls `fetch_toss_portfolio_snapshot()` with no kwarg, so `captured["need_sellable"]` is always the default `True` (the `mutations=False` case fails on `True is not False`).

- [ ] **Minimal impl — pass the flag.** In `app/services/invest_home_readers.py`, the `mutations_enabled` bool is already computed at `:547-549`. Change the fetch at `:554`:
```python
                snapshot = await fetch_toss_portfolio_snapshot(
                    need_sellable=mutations_enabled
                )
```
(No other reader logic changes — `_toss_sellable_quantity` / `_toss_pending_sell_quantity` already return `0.0` when `mutations_enabled` is `False`, so a `None` sellable from the skipped fetch is never read on that branch.)

- [ ] **Update the 3 existing Toss-reader fakes to accept the kwarg.** In `tests/test_invest_home_readers.py`, change each `async def fake_fetch_toss_snapshot():` (at `:1523`, `:1587`, `:1635`) to `async def fake_fetch_toss_snapshot(*, need_sellable: bool = True):`. Behavior is unchanged; this only keeps them call-compatible with the new keyword. (The `test_toss_portfolio_snapshot_emits_phase_spans` test at `:1680` calls the real service with `client=...` and default `True`, so it is unaffected.)

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_home_readers.py -v -k "toss"` → all pass (new gating test both params + the 3 updated fakes + the phase-span test).

- [ ] **Regression — full reader suite.** `uv run pytest tests/test_invest_home_readers.py -q` → no failures.

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-685): invest_home toss reader skips sellable fetch when mutations disabled"`

---

## Done criteria

- `fetch_toss_portfolio_snapshot(need_sellable=False)` issues **zero** `GET /sellable-quantity` calls and returns positions with `sellable_quantity=None`; default `True` is byte-for-byte unchanged.
- `analyze_stock_batch` (`_build_batch_position_index`) and invest_home home/account-panel (mutations off) no longer pay the ORDER_INFO serial prefix.
- MCP `get_holdings` output still contains `sellable_quantity`; `sell_review` classification (`sellable_quantity > 0`) is unchanged — verified by the un-touched `tests/test_mcp_portfolio_tools.py` / action_report suites staying green.
- `make lint` clean; no alembic revision added.
