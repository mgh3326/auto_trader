# ROB-810 get_holdings Toss sellable cache opt-in + skip buying_power — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP `get_holdings` reuse the process-global 45s Toss sellable-quantity cache by default and stop firing the buying_power fanout it discards — cutting avg 12.5s → ~2s.

**Architecture:** Two independent changes on the same call chain. (1) Add `need_cash: bool = True` to `fetch_toss_portfolio_snapshot`; the get_holdings collector passes `need_cash=False` since it never reads cash. (2) Add `fresh_sellable: bool = False` to `get_holdings` and thread it down to `_collect_toss_api_positions`, which passes `sellable_cache=get_shared_sellable_cache()` unless fresh.

**Tech Stack:** Python 3.13, asyncio, pytest (`pytest.mark.unit` + `pytest.mark.asyncio`), FastMCP tool registration, Sentry spans.

## Global Constraints

- migration-0: no DB schema change, no new alembic revision, no new config key (reuse `toss_sellable_cache_enabled` / `toss_sellable_cache_ttl_seconds`).
- Runtime LLM ownership boundary: no in-process LLM provider imports (unaffected here).
- Sell **sizing** safety is unchanged — display sellable may be ≤45s stale; `toss_place_order`/`toss_preview_order` re-validate at the broker at submit.
- The existing `need_sellable=False` skip path (ROB-685) must stay untouched.
- Do NOT change get_holdings response shape (cash is not surfaced today).
- Run tests with `uv run pytest ... -p no:cacheprovider` per repo convention; markers `unit` + `asyncio` already applied via `pytestmark` in the sellable test module.

---

### Task 1: `need_cash` flag on `fetch_toss_portfolio_snapshot`

Skip the KRW+USD `buying_power` fanout when the caller does not consume cash.

**Files:**
- Modify: `app/services/toss_portfolio_service.py:134-288` (`fetch_toss_portfolio_snapshot`)
- Test: `tests/test_toss_portfolio_service.py`

**Interfaces:**
- Consumes: existing `TossPortfolioClient` protocol, `TossPortfolioSnapshot`.
- Produces: `fetch_toss_portfolio_snapshot(*, need_sellable=True, need_cash=True, sellable_cache=None, client=None) -> TossPortfolioSnapshot`. When `need_cash=False`, no `buying_power` call is made and `snapshot.cash_krw`/`cash_usd` are `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_toss_portfolio_service.py`:

```python
@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_skips_cash_when_not_needed() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client, need_cash=False)

    # ROB-810: the ACCOUNT-limited buying_power fanout is skipped entirely.
    assert client.buying_power_calls == []
    assert snapshot.cash_krw is None
    assert snapshot.cash_usd is None
    # Holdings + sellable still resolve normally.
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "BRK.B"
    assert snapshot.positions[0].sellable_quantity == Decimal("1.25")
    assert snapshot.errors == []


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_default_still_fetches_cash() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    # Default unchanged: buying_power still fetched (invest_home regression guard).
    assert client.buying_power_calls == ["KRW", "USD"]
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.cash_usd == Decimal("789.01")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_toss_portfolio_service.py::test_fetch_toss_portfolio_snapshot_skips_cash_when_not_needed -v -p no:cacheprovider`
Expected: FAIL with `TypeError: fetch_toss_portfolio_snapshot() got an unexpected keyword argument 'need_cash'`

- [ ] **Step 3: Write minimal implementation**

In `app/services/toss_portfolio_service.py`, change the signature (line ~134) and guard the cash task.

Signature — add `need_cash: bool = True`:

```python
async def fetch_toss_portfolio_snapshot(
    *,
    need_sellable: bool = True,
    need_cash: bool = True,
    sellable_cache: TossSellableCache | None = None,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
```

Replace the unconditional cash-task kickoff (line ~148) with a conditional one:

```python
    # ROB-707: the cash (buying-power) snapshot is independent of holdings, so
    # kick it off concurrently with the holdings/sellable chain instead of
    # awaiting it serially after the position loop. Output is unchanged; only
    # the wall-clock overlap changes. Drained/cancelled in the finally if the
    # holdings chain raises before we await it.
    # ROB-810: callers that discard cash (MCP get_holdings) pass need_cash=False
    # so the ACCOUNT 1-TPS buying_power fanout (~3.1s) is skipped entirely.
    cash_task = (
        asyncio.ensure_future(fetch_toss_cash_snapshot(client=active_client))
        if need_cash
        else None
    )
```

Replace the cash await near the end of the `try` block (lines ~268-276):

```python
        if cash_task is not None:
            cash_snapshot = await cash_task
            errors.extend(cash_snapshot.errors)
            cash_krw = cash_snapshot.cash_krw
            cash_usd = cash_snapshot.cash_usd
        else:
            cash_krw = None
            cash_usd = None

        return TossPortfolioSnapshot(
            positions=positions,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
```

Guard the `finally` drain (line ~283) against `None`:

```python
        if cash_task is not None and not cash_task.done():
            cash_task.cancel()
            with contextlib.suppress(BaseException):
                await cash_task
        if created_client:
            await active_client.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_toss_portfolio_service.py -v -p no:cacheprovider`
Expected: PASS — the two new tests plus all pre-existing snapshot/cache tests (including `test_fetch_toss_portfolio_snapshot_maps_holdings_sellable_and_cash` which asserts `buying_power_calls == ["KRW", "USD"]`) still green.

- [ ] **Step 5: Commit**

```bash
git add app/services/toss_portfolio_service.py tests/test_toss_portfolio_service.py
git commit -m "feat(ROB-810): add need_cash flag to fetch_toss_portfolio_snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: wire cache + `need_cash=False` into the Toss collector

`_collect_toss_api_positions` structurally discards cash and is the only path into the snapshot from MCP, so it always passes `need_cash=False`. Add `fresh_sellable` to select the shared cache vs. a fresh fanout, and thread `fresh_sellable` through `_collect_portfolio_positions`.

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py` — imports (~line 105), `_collect_toss_api_positions` (548-572), `_collect_portfolio_positions` (783-951)
- Test: `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`

**Interfaces:**
- Consumes: `fetch_toss_portfolio_snapshot(need_sellable, need_cash, sellable_cache, client)` from Task 1; `get_shared_sellable_cache()` from `app.services.toss_sellable_cache`.
- Produces:
  - `_collect_toss_api_positions(market_filter, *, need_sellable=True, fresh_sellable=False) -> tuple[list, list, bool]`
  - `_collect_portfolio_positions(..., need_sellable=True, fresh_sellable=False)` — new trailing kw-only `fresh_sellable`.

- [ ] **Step 1: Write the failing test**

Add to `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`:

```python
async def test_collect_toss_api_positions_uses_shared_cache_and_skips_cash(monkeypatch):
    seen: dict[str, Any] = {}
    sentinel_cache = object()

    async def fake_fetch(*, need_sellable=True, need_cash=True, sellable_cache=None):
        seen["need_sellable"] = need_sellable
        seen["need_cash"] = need_cash
        seen["sellable_cache"] = sellable_cache

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)
    monkeypatch.setattr(
        portfolio_holdings, "get_shared_sellable_cache", lambda: sentinel_cache
    )

    # Default: shared cache is used; cash fanout skipped.
    await portfolio_holdings._collect_toss_api_positions(None)
    assert seen["need_sellable"] is True
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is sentinel_cache

    # fresh_sellable=True bypasses the cache (fresh fanout), still skips cash.
    await portfolio_holdings._collect_toss_api_positions(None, fresh_sellable=True)
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is None


async def test_collect_portfolio_positions_forwards_fresh_sellable(monkeypatch):
    seen: list[bool] = []

    async def fake_collect_toss(market_filter, *, need_sellable=True, fresh_sellable=False):
        seen.append(fresh_sellable)
        return [], [], False

    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_toss_api_positions", fake_collect_toss
    )
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, fresh_sellable=True
    )

    assert seen == [False, True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py -v -p no:cacheprovider`
Expected: FAIL — `_collect_toss_api_positions` has no `fresh_sellable`/`get_shared_sellable_cache`; `_collect_portfolio_positions` has no `fresh_sellable`.

- [ ] **Step 3: Write minimal implementation**

Add the import near line 105 of `portfolio_holdings.py` (with the other `app.services.toss_*` imports):

```python
from app.services.toss_sellable_cache import get_shared_sellable_cache
```

Rewrite `_collect_toss_api_positions` (548-572):

```python
async def _collect_toss_api_positions(
    market_filter: str | None,
    *,
    need_sellable: bool = True,
    fresh_sellable: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not bool(getattr(settings, "toss_api_enabled", False)):
        return [], [], False
    if market_filter == "crypto":
        return [], [], False

    # ROB-810: reuse the process-global 45s sellable cache (shared with the
    # /invest home reader) so repeated get_holdings calls collapse the
    # ORDER_INFO (6 TPS) /sellable-quantity fanout to 0 within the TTL. Sell
    # sizing is re-validated at the broker on submit, so display staleness is
    # bounded and safe (ROB-701 tradeoff). fresh_sellable=True forces a fresh
    # per-symbol re-fetch. need_cash=False: this path never reads cash, so skip
    # the ACCOUNT-limited buying_power fanout it would otherwise discard.
    sellable_cache = None if fresh_sellable else get_shared_sellable_cache()
    try:
        snapshot = await fetch_toss_portfolio_snapshot(
            need_sellable=need_sellable,
            need_cash=False,
            sellable_cache=sellable_cache,
        )
    except Exception as exc:
        return (
            [],
            [{"source": "toss_api", "error": str(exc), "degraded": True}],
            False,
        )

    positions = [
        _toss_api_position_to_mcp(position)
        for position in snapshot.positions
        if market_filter is None or position.instrument_type == market_filter
    ]
    return positions, snapshot.errors, True
```

In `_collect_portfolio_positions`, add the kw-only param to the signature (after `need_sellable: bool = True`, ~line 791):

```python
    need_sellable: bool = True,
    fresh_sellable: bool = False,
```

And forward it at the `_collect_toss_api_positions` call (lines ~879-881):

```python
        (
            toss_api_positions,
            toss_api_errors,
            toss_api_succeeded,
        ) = await _collect_toss_api_positions(
            market_filter, need_sellable=need_sellable, fresh_sellable=fresh_sellable
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py -v -p no:cacheprovider`
Expected: PASS — new tests plus the pre-existing `test_collect_portfolio_positions_forwards_need_sellable_to_toss` and `test_collect_toss_api_positions_defaults_need_sellable_true` (its `fake_fetch` uses `**` via keyword-only `need_sellable`; verify it still accepts the added `need_cash`/`sellable_cache` kwargs — if it fails, widen its `fake_fetch` signature to `**_` in the same commit).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py tests/mcp_server/tooling/test_toss_sellable_need_flag.py
git commit -m "feat(ROB-810): wire shared sellable cache + skip cash into Toss collector

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: expose `fresh_sellable` on the `get_holdings` MCP tool

Thread the escape hatch from the tool surface down to the collector.

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py` — `_get_holdings_impl` (1027-1052), `get_holdings` tool (1369-1421)
- Test: `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`

**Interfaces:**
- Consumes: `_collect_portfolio_positions(..., fresh_sellable=...)` from Task 2.
- Produces:
  - `_get_holdings_impl(..., fresh_sellable: bool = False)` forwards to `_collect_portfolio_positions`.
  - `get_holdings(..., fresh_sellable: bool = False)` tool param.

- [ ] **Step 1: Write the failing test**

Add to `tests/mcp_server/tooling/test_toss_sellable_need_flag.py`:

```python
async def test_get_holdings_impl_forwards_fresh_sellable(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        seen.update(kwargs)
        return [], [], None, None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    await portfolio_holdings._get_holdings_impl()
    assert seen["fresh_sellable"] is False

    await portfolio_holdings._get_holdings_impl(fresh_sellable=True)
    assert seen["fresh_sellable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py::test_get_holdings_impl_forwards_fresh_sellable -v -p no:cacheprovider`
Expected: FAIL — `_get_holdings_impl` does not pass `fresh_sellable`, so `seen` has no such key (`KeyError`).

- [ ] **Step 3: Write minimal implementation**

In `_get_holdings_impl` add the kw-only param (after `routing_account_mode: str = "kis_live",`, ~line 1035):

```python
    routing_account_mode: str = "kis_live",
    fresh_sellable: bool = False,
```

Forward it in the `_collect_portfolio_positions` call (~lines 1046-1052):

```python
    ) = await _collect_portfolio_positions(
        account=account,
        market=market,
        include_current_price=include_current_price,
        account_name=account_name,
        is_mock=is_mock,
        fresh_sellable=fresh_sellable,
    )
```

In the `get_holdings` tool function, add the param (after `account_type: str | None = None,`, ~line 1376):

```python
        account_type: str | None = None,
        fresh_sellable: bool = False,
```

Forward it into the `_get_holdings_impl` call (~lines 1392-1400):

```python
            await _get_holdings_impl(
                account=account,
                market=market,
                include_current_price=include_current_price,
                minimum_value=minimum_value,
                account_name=account_name,
                is_mock=routing.is_kis_mock,
                routing_account_mode=routing.account_mode,
                fresh_sellable=fresh_sellable,
            ),
```

Extend the tool `description` string (~line 1355-1367) with one sentence:

```
            "fresh_sellable=True bypasses the 45s Toss sellable-quantity cache "
            "and re-fetches per-symbol (default False reuses the shared cache). "
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_toss_sellable_need_flag.py -v -p no:cacheprovider`
Expected: PASS (all tests in the module).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py tests/mcp_server/tooling/test_toss_sellable_need_flag.py
git commit -m "feat(ROB-810): expose fresh_sellable escape hatch on get_holdings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: full-suite regression + lint

Confirm no collateral damage across holdings/home/cash paths.

**Files:** none (verification only)

- [ ] **Step 1: Run the affected suites**

Run:
```bash
uv run pytest tests/test_toss_portfolio_service.py tests/test_invest_home_readers.py \
  tests/test_mcp_portfolio_tools.py tests/test_mcp_holdings_rob562.py \
  tests/mcp_server/tooling/test_toss_sellable_need_flag.py \
  tests/mcp_server/tooling -v -p no:cacheprovider
```
Expected: all PASS. Pay attention to `test_invest_home_readers.py` (the `need_cash=True` default consumer) and any test asserting Toss cash in a holdings response.

- [ ] **Step 2: Lint + typecheck the touched files**

Run: `make lint` (Ruff + ty). Expected: clean. If ty flags the `cash_task: asyncio.Future | None`, add the annotation `cash_task: asyncio.Future[TossCashSnapshot] | None`.

- [ ] **Step 3: Grep for any missed caller**

Run: `grep -rn "fetch_toss_portfolio_snapshot" app/ --include="*.py"`
Expected: only `portfolio_holdings.py` (need_cash=False path) and `invest_home_readers.py` (default) — confirm neither now double-fetches or drops cash it needs.

- [ ] **Step 4: Commit (if lint made changes)**

```bash
git add -A
git commit -m "chore(ROB-810): lint/type cleanup

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Change 1 (sellable cache opt-in, `fresh_sellable`) → Tasks 2 + 3. ✅
- Change 2 (skip discarded buying_power via `need_cash`) → Task 1 + wired in Task 2. ✅
- Safety (submit re-validation, TTL staleness) → comments in Task 2; no sizing path touched. ✅
- Callers audited (invest_home keeps `need_cash=True`) → Task 1 regression test + Task 4 grep. ✅
- Deferred #3 (itemchartprice) → not in plan, matches spec. ✅
- migration-0, no new config → honored (reuses shared cache getter). ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✅

**Type consistency:** `fresh_sellable: bool` and `need_cash: bool` used identically across `fetch_toss_portfolio_snapshot` → `_collect_toss_api_positions` → `_collect_portfolio_positions` → `_get_holdings_impl` → `get_holdings`. `get_shared_sellable_cache` imported once and referenced by monkeypatch in tests. ✅
