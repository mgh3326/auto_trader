# ROB-725 analyze_stock_batch NXT Premarket Quote Overlay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `analyze_stock_batch`'s KR `current_price` reflect the live NXT price during premarket/after-hours sessions (instead of the stale prior KRX close), so support/resistance `distance_pct` and order-price anchors track the real market.

**Architecture:** The standalone `get_quote` tool already applies an NXT orderbook overlay during NXT sessions; the `analyze` KR quote path (`_fetch_kr_live_quote` → KIS `inquire_price(market="J")`) does not. Extract the overlay-application into a shared `_apply_nxt_quote_overlay` helper, refactor `get_quote` to use it (behavior-preserving), then wire it into `_resolve_kr_quote`. `distance_pct` self-corrects because `_recompute_intraday_support_resistance` re-anchors on `quote.price`. Surface `price_source`/`session`/`data_state`/`venue` in the compact batch formatter.

**Tech Stack:** Python 3.13, pytest (`pytest-asyncio`), `uv run`, existing MCP tooling in `app/mcp_server/tooling/`.

## Global Constraints

- **read-only:** no broker/order/watch mutation; `get_orderbook(venue="nxt")` is a read call. — verbatim from spec.
- **migration 0** — no DB/alembic changes.
- **fail-open:** overlay failure (non-NXT session, empty orderbook, exception) always degrades to the existing KIS/OHLCV quote path — never raises.
- **Behavior-preserving refactor:** existing `get_quote` NXT tests in `tests/test_mcp_quotes_tools.py` MUST stay green after Task 1.
- Test run command prefix: `uv run pytest`. Lint/format before commit: `make format && make lint`.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: Extract shared `_apply_nxt_quote_overlay` helper and refactor `get_quote`

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py` — add helper after `_fetch_nxt_quote_overlay` (ends at line 408); refactor `_get_quote_impl` KR branch (lines 1219-1240).
- Test: `tests/test_mcp_quotes_tools.py` (existing regression) + new unit test for the helper.

**Interfaces:**
- Produces: `async def _apply_nxt_quote_overlay(symbol: str, quote: dict[str, Any], *, data_state: str) -> bool` — mutates `quote` in place (sets `price`, `session`, `venue`, `venue_label`, `kis_market_code`, `source_endpoint`, `source_tr_id`, `price_source`, `regular_session_data_state`, `data_state`) and returns `True` when an NXT overlay was applied; returns `False` (no mutation) when not in an NXT session or the orderbook is empty. Never raises.
- Consumes: existing module functions `_nxt_quote_session`, `_fetch_nxt_quote_overlay`, constant `DATA_STATE_FRESH` (all already in this module).

- [ ] **Step 1: Write the failing unit test for the helper**

Add to `tests/test_mcp_quotes_tools.py` (reuses the existing `_nxt_quote_book` helper at line 385):

```python
@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_applies_in_premarket(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 173500.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_expected_price",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"symbol": "192820", "price": 168300.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price"] == 173500.0
    assert quote["price_source"] == "nxt_expected_price"
    assert quote["session"] == "nxt_premarket"
    assert quote["data_state"] == "fresh"
    assert quote["regular_session_data_state"] == "premarket_unavailable"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_noop_outside_session(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return None

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)

    quote = {"symbol": "192820", "price": 168300.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="fresh"
    )

    assert applied is False
    assert quote == {"symbol": "192820", "price": 168300.0, "source": "kis"}


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_noop_on_empty_book(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return None  # empty orderbook → _fetch_nxt_quote_overlay returns None

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"symbol": "192820", "price": 168300.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="premarket_unavailable"
    )

    assert applied is False
    assert quote == {"symbol": "192820", "price": 168300.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_quotes_tools.py -k apply_nxt_quote_overlay -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_apply_nxt_quote_overlay'`.

- [ ] **Step 3: Add the helper**

In `app/mcp_server/tooling/market_data_quotes.py`, immediately after `_fetch_nxt_quote_overlay` (after its `return overlay` at line 408), add:

```python
async def _apply_nxt_quote_overlay(
    symbol: str, quote: dict[str, Any], *, data_state: str
) -> bool:
    """Overlay an NXT-derived price onto ``quote`` during NXT sessions (ROB-725).

    In-place mutation: ``price`` becomes the NXT expected/mid/best price and
    ``price_source``/``session``/``venue``/``data_state`` are tagged. Returns
    ``True`` when applied. Returns ``False`` (no mutation) when not in an NXT
    session or the NXT orderbook is empty. Never raises — fail-open to the base
    quote.
    """
    session = await _nxt_quote_session(data_state)
    if session is None:
        return False
    overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
    if overlay is None:
        return False
    quote.update(overlay)
    quote["regular_session_data_state"] = data_state
    quote["data_state"] = DATA_STATE_FRESH
    return True
```

- [ ] **Step 4: Refactor `_get_quote_impl` to use the helper (behavior-preserving)**

Replace the KR branch in `_get_quote_impl` (current lines 1225-1240):

```python
        data_state = kr_market_data_state()
        quote = await _fetch_quote_equity_kr(symbol)
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        session = await _nxt_quote_session(data_state)
        if session is not None:
            overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
            if overlay is not None:
                quote.update(overlay)
                quote["regular_session_data_state"] = data_state
                quote["data_state"] = DATA_STATE_FRESH
                return quote

        quote["data_state"] = data_state
        return quote
```

with:

```python
        data_state = kr_market_data_state()
        quote = await _fetch_quote_equity_kr(symbol)
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        if await _apply_nxt_quote_overlay(symbol, quote, data_state=data_state):
            return quote

        quote["data_state"] = data_state
        return quote
```

- [ ] **Step 5: Run helper tests + full get_quote regression suite**

Run: `uv run pytest tests/test_mcp_quotes_tools.py -v`
Expected: PASS — new `_apply_nxt_quote_overlay` tests pass AND all existing `test_get_quote_korean_equity_premarket_*` / `*_after_hours_*` / `*_empty_nxt_book_*` regression tests still pass.

- [ ] **Step 6: Format, lint, commit**

```bash
make format && make lint
git add app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py
git commit -m "refactor(ROB-725): extract _apply_nxt_quote_overlay shared helper

Behavior-preserving extraction of the NXT overlay application from
_get_quote_impl so the analyze path can reuse it (Task 2).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire the NXT overlay into the analyze KR quote path

**Files:**
- Modify: `app/mcp_server/tooling/analysis_analyze.py` — imports (lines 36-41), `_resolve_kr_quote._annotate` (lines 113-117).
- Test: `tests/test_analyze_stock_kr_live_price.py` (existing file for `_resolve_kr_quote`).

**Interfaces:**
- Consumes: `_apply_nxt_quote_overlay` (Task 1), `kr_market_data_state` (from `app.mcp_server.tooling.market_session`), `now_kst` (already imported at line 14).
- Produces: `_resolve_kr_quote` now returns a quote whose `price` is NXT-derived during NXT sessions, with `is_stale_price=False` and `price_as_of=now_kst().isoformat()` on overlay; unchanged (KIS/OHLCV) otherwise.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_analyze_stock_kr_live_price.py`:

```python
@pytest.mark.asyncio
async def test_kr_quote_overlays_nxt_price_in_premarket(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,  # stale KRX prior close
            "source": "kis",
            "price_as_of": (today - timedelta(days=1)).isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        quote["price"] = 173500.0
        quote["price_source"] = "nxt_expected_price"
        quote["session"] = "nxt_premarket"
        quote["data_state"] = "fresh"
        return True

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(analysis_analyze, "kr_market_data_state", lambda *a, **k: "premarket_unavailable")

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 173500.0
    assert quote["price_source"] == "nxt_expected_price"
    assert quote["is_stale_price"] is False  # overlay price is fresh
    # price_as_of refreshed to the live NXT fetch time (today, not yesterday)
    assert quote["price_as_of"].startswith(str(today.date()))


@pytest.mark.asyncio
async def test_kr_quote_keeps_kis_price_when_no_overlay(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,
            "source": "kis",
            "price_as_of": today.isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        return False  # not an NXT session / empty book

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(analysis_analyze, "kr_market_data_state", lambda *a, **k: "fresh")

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 168300.0
    assert "price_source" not in quote
    assert quote["is_stale_price"] is False  # today's KIS as_of, unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_analyze_stock_kr_live_price.py -k "overlays_nxt or keeps_kis" -v`
Expected: FAIL — `AttributeError` on `analysis_analyze._apply_nxt_quote_overlay` / `kr_market_data_state` (not yet imported).

- [ ] **Step 3: Add imports**

In `app/mcp_server/tooling/analysis_analyze.py`, extend the `market_data_quotes` import block (lines 36-41) to include the helper:

```python
from app.mcp_server.tooling.market_data_quotes import (
    _apply_nxt_quote_overlay,
    _fetch_kr_live_quote,
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
```

Add a new import for the session classifier (place near the other `market_session`-adjacent imports, alphabetically within the tooling imports):

```python
from app.mcp_server.tooling.market_session import kr_market_data_state
```

- [ ] **Step 4: Apply overlay inside `_annotate`**

In `_resolve_kr_quote`, replace the nested `_annotate` (current lines 113-117):

```python
    async def _annotate(quote: dict[str, Any]) -> dict[str, Any]:
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        return quote
```

with:

```python
    async def _annotate(quote: dict[str, Any]) -> dict[str, Any]:
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        # ROB-725: during NXT premarket/after-hours the KRX regular quote is the
        # prior close — overlay the live NXT price so current_price + S/R
        # distance_pct track the real market.
        if await _apply_nxt_quote_overlay(
            symbol, quote, data_state=kr_market_data_state()
        ):
            quote["is_stale_price"] = False
            quote["price_as_of"] = now_kst().isoformat()
        return quote
```

- [ ] **Step 5: Run the new tests + the existing `_resolve_kr_quote` suite**

Run: `uv run pytest tests/test_analyze_stock_kr_live_price.py -v`
Expected: PASS — new overlay tests pass AND existing `test_kr_live_price_today_is_not_stale` / `test_kr_prev_day_quote_is_stale` still pass (they monkeypatch `_fetch_kr_live_quote`; the real `_apply_nxt_quote_overlay` runs with `kr_market_data_state()` returning a non-NXT state in the test env → no-op).

> Note: existing tests don't patch `_apply_nxt_quote_overlay`. If `kr_market_data_state()` in the test environment ever resolves to an NXT session and `_nxt_quote_session` hits the network, add `monkeypatch.setattr(analysis_analyze, "kr_market_data_state", lambda *a, **k: "market_closed")` to those two tests to keep them hermetic. Apply that only if the run in this step shows an unexpected overlay call.

- [ ] **Step 6: Format, lint, commit**

```bash
make format && make lint
git add app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_kr_live_price.py
git commit -m "fix(ROB-725): overlay live NXT price in analyze_stock_batch KR quote

_resolve_kr_quote now applies the shared NXT overlay so premarket/NXT
current_price reflects the real NXT market instead of the prior KRX
close. distance_pct self-corrects via _recompute_intraday_support_resistance.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Surface overlay provenance in the compact batch formatter

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` — after the `nxt_tradable` passthrough loop (ends line 792, before `if position_index is not None:` at line 794).
- Test: `tests/test_analyze_stock_batch_cache.py` (existing batch/formatter test file).

**Interfaces:**
- Consumes: `analysis["quote"]` fields set by Task 2 (`price_source`, `session`, `data_state`, `venue`).
- Produces: compact summary rows carry `price_source`/`session`/`data_state`/`venue` when present on the quote.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_analyze_stock_batch_cache.py`:

```python
def test_summarize_surfaces_nxt_price_provenance():
    from app.mcp_server.tooling.analysis_tool_handlers import (
        _summarize_analysis_result,
    )

    analysis = {
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {
            "price": 173500.0,
            "price_source": "nxt_expected_price",
            "session": "nxt_premarket",
            "data_state": "fresh",
            "venue": "nxt",
        },
        "support_resistance": {"supports": [], "resistances": []},
    }

    summary = _summarize_analysis_result("192820", analysis)

    assert summary["current_price"] == 173500.0
    assert summary["price_source"] == "nxt_expected_price"
    assert summary["session"] == "nxt_premarket"
    assert summary["data_state"] == "fresh"
    assert summary["venue"] == "nxt"


def test_summarize_omits_price_provenance_when_absent():
    from app.mcp_server.tooling.analysis_tool_handlers import (
        _summarize_analysis_result,
    )

    analysis = {
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {"price": 168300.0},
        "support_resistance": {"supports": [], "resistances": []},
    }

    summary = _summarize_analysis_result("192820", analysis)

    assert summary["current_price"] == 168300.0
    assert "price_source" not in summary
    assert "session" not in summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_analyze_stock_batch_cache.py -k "price_provenance" -v`
Expected: FAIL — `KeyError`/`assert` on `summary["price_source"]` (not yet surfaced).

- [ ] **Step 3: Add the passthrough loop**

In `app/mcp_server/tooling/analysis_tool_handlers.py`, immediately after the existing `nxt_tradable` passthrough loop (after line 792, before `if position_index is not None:`), add:

```python
    # ROB-725: surface NXT price provenance so the agent knows current_price is
    # an NXT-derived quote (not the stale KRX regular-session close).
    for _px_key in ("price_source", "session", "data_state", "venue"):
        if _px_key in quote:
            summary[_px_key] = quote[_px_key]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_analyze_stock_batch_cache.py -k "price_provenance" -v`
Expected: PASS.

- [ ] **Step 5: Format, lint, commit**

```bash
make format && make lint
git add app/mcp_server/tooling/analysis_tool_handlers.py tests/test_analyze_stock_batch_cache.py
git commit -m "feat(ROB-725): surface NXT price provenance in compact batch summary

price_source/session/data_state/venue passthrough so agents can tell an
NXT-derived current_price from the KRX regular close.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: End-to-end verification — distance_pct re-anchors on the NXT price

**Files:**
- Test: `tests/test_analyze_stock_batch_cache.py` (or the pipeline compat test file if better suited).

**Interfaces:**
- Consumes: the full `analyze_stock_impl` path with Task 2's overlay + `_recompute_intraday_support_resistance`.
- Produces: proof that `support_resistance.distance_basis_price` equals the NXT price and `distance_pct` is computed against it.

- [ ] **Step 1: Write the failing/verifying test**

Add to `tests/test_analyze_stock_batch_cache.py`. Verify the intraday re-sign uses the overlaid price by calling the real `_recompute_intraday_support_resistance` with an NXT-overlaid quote:

```python
def test_intraday_sr_reanchors_on_nxt_overlay_price():
    from app.mcp_server.tooling.analysis_analyze import (
        _recompute_intraday_support_resistance,
    )

    analysis = {
        "quote": {"price": 173500.0, "price_source": "nxt_expected_price"},
        "support_resistance": {
            # EOD levels computed against the stale 168300 close
            "supports": [{"price": 170000.0, "distance_pct": 1.01}],
            "resistances": [{"price": 171387.0, "distance_pct": 1.83}],
        },
    }

    _recompute_intraday_support_resistance(analysis, "equity_kr")

    sr = analysis["support_resistance"]
    assert sr["distance_basis_price"] == 173500.0
    assert sr["distance_basis"] == "live_quote"
    # 170000 and 171387 are BELOW the 173500 NXT price → both become supports
    support_prices = {s["price"] for s in sr["supports"]}
    assert support_prices == {170000.0, 171387.0}
    assert sr["resistances"] == []
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_analyze_stock_batch_cache.py -k "reanchors_on_nxt" -v`
Expected: PASS immediately (Task 2 already fixed the quote; this test documents the end-to-end contract that no distance-recompute code change was needed). If it FAILS, `_recompute_intraday_support_resistance` is not reading `quote.price` as expected — stop and investigate before continuing.

- [ ] **Step 3: Run the full affected suites**

Run:
```bash
uv run pytest tests/test_analyze_stock_kr_live_price.py tests/test_mcp_quotes_tools.py tests/test_analyze_stock_batch_cache.py tests/mcp_server/test_analyze_stock_pipeline_compat.py -v
```
Expected: PASS (all green).

- [ ] **Step 4: Commit**

```bash
git add tests/test_analyze_stock_batch_cache.py
git commit -m "test(ROB-725): assert intraday S/R distance re-anchors on NXT overlay price

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Spec §설계.1 (shared helper) → Task 1. ✓
- Spec §설계.2 (wire into `_resolve_kr_quote`, `is_stale_price=False`, `price_as_of=now`) → Task 2. ✓
- Spec §설계.2 (distance_pct auto-correction, no code change) → Task 4 verifies. ✓
- Spec §설계.3 (compact formatter passthrough of `price_source`/`session`/`data_state`/`venue`) → Task 3. ✓
- Spec §테스트 items 1-6 → Task 1 (helper apply/noop/empty = spec tests 1-3 at helper level), Task 2 (analyze wiring = spec tests 1-3 at `_resolve_kr_quote` level), Task 3 (spec test 5, compact surface), Task 4 (spec test 4, distance re-anchor), Task 1 Step 5 (spec test 6, `_get_quote_impl` regression). ✓
- Spec §안전 경계 (read-only, migration 0, fail-open, session-gated network) → Global Constraints + helper fail-open in Task 1. ✓
- Spec §범위 밖 (naver TTL, premarket_gap, quote_staleness_sec) → not planned. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `_apply_nxt_quote_overlay(symbol: str, quote: dict, *, data_state: str) -> bool` defined in Task 1 and called identically in Task 1 Step 4, Task 2 Step 4 (via `analysis_analyze` import), and monkeypatched with matching `(symbol, quote, *, data_state)` signature in Task 2 Step 1. `kr_market_data_state()` no-arg call consistent. `price_source`/`session`/`data_state`/`venue` key names consistent across Tasks 2, 3, 4. ✓
