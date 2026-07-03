# /invest/api/market-parity — Parallelize Card Builders + Drop Unused KOSPI History Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Shave latency off the read-only `GET /invest/api/market-parity` endpoint (Sentry: avg ~3.6s, p95 ~6.6s; `http.client` ≈ 76% of server time — Naver KOSPI index + er-api FX). Two mechanical inefficiencies exist: (1) `build_market_parity` awaits the four independent card builders (index / stablecoin / kimchi / synthetic) **serially** instead of via `asyncio.gather`, and (2) `get_index_quote` calls the shared `handle_get_market_index(count=1)`, which for KOSPI fires **three** Naver HTTP calls (basic + a 1-row price page **inside** `_fetch_index_kr_current`, plus a full `_fetch_index_kr_history` page) even though market-parity only reads the current value — the history page is fetched and discarded. **Be honest: this is a Low-priority, low-ROI change.** An adversarial pass downgraded the expected win: because the calls *inside* each leaf handler are already `asyncio.gather`-ed, card-level parallelization overlaps the two network-bound cards (index + kimchi) and dropping the wasted history call cuts spans/upstream load — realistic best case is **avg ~3.6s → ~3.0s, not ~0.8s.** BUT the Sentry transaction avg (~3,573ms) being ≈ the *serial* sum of the HTTP calls is a clue that the within-handler gathers may not actually overlap in practice; if that is true, dropping the two wasted Naver `/price`+`/history` calls could instead save ~1.3s serially. **Task 1 is therefore a mandatory diagnostic** that reads the code + a representative trace to decide which mechanism actually pays, before writing any production code. Both fixes are unconditionally *correct* (they never change output); the diagnostic only calibrates the honest savings claim and confirms there is no reason not to ship them.

**Architecture:** Today `build_market_parity` (`app/services/invest_view_model/market_parity_service.py:513`) builds cards with sequential awaits: `_build_index_card` (`:527`), `_build_stablecoin_card` (`:539`), `_build_kimchi_card` (`:543`), then a `for` loop of `_build_synthetic_card` (`:561`–`:564`). In the default provider only the index card (KOSPI → Naver) and the kimchi card (Upbit + Binance + er-api FX, gathered inside `_fetch_kimchi_premium`, `app/mcp_server/tooling/fundamentals_sources_naver.py:224`) do real network I/O; stablecoin/synthetic legs return `None` immediately. Because the two network-bound cards run one-after-another, their HTTP time adds instead of overlapping. Separately, `DefaultMarketParityProvider.get_index_quote` (`:78`) calls `handle_get_market_index(symbol="KOSPI", period="day", count=1)` (`app/mcp_server/tooling/fundamentals/_market_index.py:62`); the naver branch (`:83`) does `asyncio.gather(_fetch_index_kr_current(...), _fetch_index_kr_history(...))`, and `_fetch_index_kr_current` (`app/mcp_server/tooling/fundamentals_sources_indices.py:65`) *itself* gathers a `/basic` and a `/price?pageSize=1` call (`:70`). Market-parity's `_first_index_row` reads only `row["current"]`/`row["open"]` from the `indices` list and discards `history` (grep confirms the service never references `history`). Target flow: (a) `build_market_parity` gathers the four builders concurrently while preserving exact card + warning ordering; (b) a new **current-only** helper `handle_get_market_index_current_only(symbol)` in `_market_index.py` reuses `_fetch_index_kr_current` + `_tag_kr_index_data_state` (so the ROB-464 `open`-based freshness override still fires) but omits the `_fetch_index_kr_history` call, and `get_index_quote` calls it instead of the shared handler. The shared `handle_get_market_index` is left **byte-for-byte unchanged** so its other callers keep their history payload.

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), `asyncio.gather`, httpx (Naver/er-api leaf fetchers), FastAPI read router, pydantic v2 (`InvestMarketParityResponse`/`InvestMarketParityCard`). No DB, no Redis, no broker/order/watch code paths.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **Scope fix #2 to get_index_quote's OWN fetch path — do NOT change the shared handle_get_market_index (other callers consume the history).**
- **A naive basic-only path drops the 'open' field that ROB-464's _is_fresh_clock_lagging_kr_index (_market_index.py:~38-59) uses to mark the KR index stale and set as_of — preserve it (silently disabling that freshness override is a regression).**
- **A 30-60s cache must cache the data_state-tagged payload to preserve ROB-464 freshness semantics. At current traffic (6 req/7d) cache hit rate is ~0, so the cache is a low-value follow-up, not the headline.**
- **Read-only /invest view; no broker/order/watch mutation. migration-0.**
- **Be honest about the low ROI in the Goal; this issue is Low priority.**
- **Migration-0.** No new DB column, no alembic revision, no schema change. This is a pure read-path latency refactor.
- Read-only path: no broker / order / watch / order-intent mutation is introduced or reachable from any changed line. No new network *sources* are added — only the removal of one already-fetched-and-discarded call and a reordering of existing awaits.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## Approach / decisions (read before Task 1)

- **Fix #2 mechanism = new sibling function, NOT a parameter on the shared handler.** The lowest-risk way to satisfy "do NOT change the shared `handle_get_market_index`" is to add a new `handle_get_market_index_current_only(symbol)` in `_market_index.py` that reuses the existing, already-tested leaf fetchers (`_fetch_index_kr_current`, `_fetch_index_crypto_current`, `_fetch_index_us_current`) and the `_tag_kr_index_data_state` tagger, and to point only `get_index_quote` at it. Adding an `include_history=False` kwarg to the shared handler was rejected: it edits the shared function's body/signature and risks other callers, which the constraint forbids.
- **The `open` field is preserved for free.** The ROB-464 freshness override reads `open` and `current`, both of which come from `_fetch_index_kr_current` (`/basic` + `/price?pageSize=1`). Only the *separate* `_fetch_index_kr_history` call is dropped. Keeping `_fetch_index_kr_current` intact and still calling `_tag_kr_index_data_state` on its result means the stale-tagging + `as_of` behavior is byte-identical to today.
- **Cache = deferred, not built.** Per the verbatim constraint, at ~6 requests / 7 days the hit rate is ~0, so a 30–60s cache is documented as a follow-up (see "Deferred / Non-goal") and intentionally NOT implemented in this plan. If it is ever added it MUST wrap the `data_state`-tagged payload (post-`_tag_kr_index_data_state`), never the raw pre-tag Naver JSON.
- **Both fixes are output-preserving.** Task 2 only reorders awaits (card order + warning order held constant); Task 3 only removes a discarded fetch. No test should observe a value change — only fewer upstream calls and concurrency.

---

## File Structure

| File | Create/Modify | Responsibility (which Task) |
|------|---------------|-----------------------------|
| `app/mcp_server/tooling/fundamentals/_market_index.py` | Modify | Task 3 — add `handle_get_market_index_current_only(symbol)` (current-row only; reuses `_tag_kr_index_data_state`). Shared `handle_get_market_index` untouched. |
| `app/services/invest_view_model/market_parity_service.py` | Modify | Task 2 — `build_market_parity` gathers the four card builders (order preserved). Task 3 — `get_index_quote` calls the new current-only helper + swaps the import. |
| `tests/test_invest_market_parity_service.py` | Modify | Task 2 — concurrency proof (index+kimchi overlap) + output-unchanged regressions. Task 3 — `get_index_quote` uses the current-only helper. |
| `tests/mcp_server/tooling/test_market_index_current_only.py` | Create | Task 3 — current-only helper drops history, preserves ROB-464 freshness tag; guard that shared `handle_get_market_index` still returns `history`. |

> **NOT touched:**
> - **The shared `handle_get_market_index` body/signature (`_market_index.py:62`)** and its default-batch path (`:104`–`:131`) — other callers (`get_market_index` MCP tool, snapshot collectors) consume `history`; Task 3 adds a *sibling* function and never edits this one. A guard test locks it.
> - **The leaf fetchers `_fetch_index_kr_current` / `_fetch_index_kr_history` / `_fetch_index_us_*` / `_fetch_index_crypto_current` (`fundamentals_sources_indices.py`)** — reused as-is; no edits (dropping history = *not calling* `_fetch_index_kr_history`, not changing it).
> - **`_tag_kr_index_data_state` / `_is_fresh_clock_lagging_kr_index` (`_market_index.py:38`,`:46`)** — reused verbatim so the ROB-464 `open`/`as_of` freshness semantics are identical.
> - **The kimchi path (`_crypto.py`, `fundamentals_sources_naver.py`) and all non-index card legs** — Task 2 only changes *when* `_build_kimchi_card` is scheduled (concurrently), never its internals.
> - **The router (`app/routers/invest_api.py:212`) and `InvestMarketParityResponse` schema** — signatures and output shape unchanged.
> - **No cache layer / Redis** — see Deferred / Non-goal.

---

## Task 1 — Diagnostic: do the within-card gathers actually overlap? (measurement, no production code, no commit)

**This task ships NO production code and has no TDD test — it is measurement + a recorded decision that calibrates the honest savings claim and confirms both fixes are safe to proceed.** It exists because the adversarial review found the ROI hinges on whether `asyncio.gather` inside the leaf handlers overlaps in practice (avg ≈ serial-HTTP-sum is a red flag). Tasks 2 and 3 proceed **regardless** of the finding (both are output-preserving and correct); the finding only sets the expected-savings language in the PR description and the eventual retro.

**Files:** none modified. Optionally a throwaway script under the scratchpad dir (never committed).

Steps:

- [ ] **Re-read the two hot paths and confirm the code-level claims** (anchor the exact current line numbers, they may drift):
  - `build_market_parity` serial awaits: `market_parity_service.py:527` (index), `:539` (stablecoin), `:543` (kimchi), `:561`–`:564` (synthetic loop).
  - `get_index_quote` → `handle_get_market_index(...count=1)`: `market_parity_service.py:79`.
  - naver branch `asyncio.gather(current, history)`: `_market_index.py:83`.
  - `_fetch_index_kr_current` internal `asyncio.gather(basic, price)`: `fundamentals_sources_indices.py:70`.
  - kimchi `asyncio.gather(upbit, binance, er-api)`: `fundamentals_sources_naver.py:224`.
  - Confirm the service **never** reads `history`: `grep -n "history" app/services/invest_view_model/market_parity_service.py` → expect no hits.
- [ ] **Pull a representative trace.** In Sentry, open a recent `GET /invest/api/market-parity` transaction (server-side). Inspect the `http.client` spans and record, for the two Naver calls and the er-api call, each span's **start** and **end** offsets. Determine overlap:
  - If the three Naver spans (basic / price / history) START at ~the same offset and their durations overlap → the within-handler gathers **do** overlap; dropping history saves ≈ **0ms wall** (only cuts one span + upstream load).
  - If they START sequentially (each begins ≈ when the previous ends) → the gathers do **not** overlap in practice (event-loop starvation, DNS, or per-call `httpx.AsyncClient` setup serializing) → dropping the history call saves ≈ **one Naver RTT** serially, and card-level `gather` (Task 2) additionally overlaps index vs kimchi.
- [ ] **If Sentry is unavailable to the executor**, fall back to a local measurement: in the scratchpad dir write a throwaway async script that calls `handle_get_market_index("KOSPI", count=1)` and, separately, the proposed `handle_get_market_index_current_only("KOSPI")`, wrapping each leaf fetcher with a `time.perf_counter()` log of enter/exit. Compare wall times and enter/exit interleaving. **Do not commit this script.** (This is a live-network probe; it is measurement only and touches no repo file.)
- [ ] **Record the decision.** Write 3–5 sentences into the PR description (and the ROB-689 retro memory) stating: (a) whether the within-card gathers overlap, (b) the resulting *honest* expected savings (`~0.5s` overlap win from Task 2 + `~0ms`..`~1.3s` from Task 3 depending on (a)), and (c) confirmation that neither fix changes output. Both Task 2 and Task 3 are GO regardless.
- [ ] **No commit for this task.**

---

## Task 2 — Parallelize the four independent card builders (output-preserving, migration-0)

**Files:**
- Modify `app/services/invest_view_model/market_parity_service.py` — rewrite the body of `build_market_parity` (`:527`–`:564`) to gather `_build_index_card`, `_build_stablecoin_card`, `_build_kimchi_card`, and the `_build_synthetic_card` calls concurrently while appending cards/warnings in the exact same order.
- Test (modify) `tests/test_invest_market_parity_service.py` — add a deterministic concurrency proof + an ordering/output-unchanged regression.

**Interfaces:**
- Consumes (unchanged): `_build_index_card(provider, config) -> tuple[InvestMarketParityCard, list[str]]` (`:235`), `_build_stablecoin_card(provider) -> tuple[...]` (`:290`), `_build_kimchi_card(provider) -> tuple[...]` (`:367`), `_build_synthetic_card(provider, config) -> tuple[...]` (`:400`). Each is already exception-contained per-leg via `_capture` (`:207`, `asyncio.wait_for(..., timeout=6)`), so gathering them cannot surface a new exception.
- Produces: `build_market_parity(...) -> InvestMarketParityResponse` — signature and output **unchanged**; only scheduling changes.

Steps:

- [ ] **Write failing test — the index and kimchi builders run concurrently.** Add to `tests/test_invest_market_parity_service.py` a provider that forces overlap via two `asyncio.Event`s, so under serial scheduling the index leg times out (→ `missing`) and under `gather` it resolves (→ `fresh`):
```python
import asyncio


class _OverlapProbeProvider(_StubParityProvider):
    """Proves index-card and kimchi-card run concurrently.

    get_index_quote sets ``index_started`` then blocks on ``kimchi_started``;
    get_crypto_kimchi_premium sets ``kimchi_started`` then blocks on
    ``index_started``. Under serial card building the index leg's inner
    wait_for(1.0) times out (kimchi has not started yet) -> index card 'missing'.
    Under asyncio.gather both events fire and both resolve -> index card 'fresh'.
    """

    def __init__(self) -> None:
        super().__init__()
        self.index_started = asyncio.Event()
        self.kimchi_started = asyncio.Event()

    async def get_index_quote(self, symbol: str) -> ParityQuote | None:
        self.index_started.set()
        await asyncio.wait_for(self.kimchi_started.wait(), timeout=1.0)
        return await super().get_index_quote(symbol)

    async def get_crypto_kimchi_premium(self, symbol: str) -> dict[str, Any] | None:
        self.kimchi_started.set()
        await asyncio.wait_for(self.index_started.wait(), timeout=1.0)
        return await super().get_crypto_kimchi_premium(symbol)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_builds_cards_concurrently() -> None:
    response = await build_market_parity(_OverlapProbeProvider())
    cards = {card.id: card for card in response.cards}
    # Under serial scheduling the index leg would time out -> 'missing'.
    assert cards["ewy-kospi-implied-parity"].dataState == "fresh"
    assert cards["btc-kimchi-premium"].dataState == "fresh"
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_market_parity_service.py -k concurrently -v`
  Expected: FAIL — current serial code builds the index card fully first; its `get_index_quote` blocks on `kimchi_started` (never set yet) and the inner `wait_for(1.0)` raises `TimeoutError`, which `_capture` swallows → `base=None` → index card `dataState == "missing"` (`emptyReason == "market_index_unavailable"`).

- [ ] **Minimal impl — gather the builders, preserve order.** In `market_parity_service.py`, replace the serial block (`:527`–`:564`, from `index_card, index_warnings = await _build_index_card(...)` through the `for config in synthetic_configs:` loop) with:
```python
    index_config = IndexParityConfig(
        id="ewy-kospi-implied-parity",
        title="EWY implied KOSPI parity",
        base_symbol="KOSPI",
        proxy_symbol="EWY",
    )
    synthetic_configs = [
        SyntheticParityConfig(
            base_symbol="005930",
            base_name="삼성전자",
            synthetic_symbol="xyz:SMSN",
            title="Samsung Electronics synthetic parity",
        ),
        SyntheticParityConfig(
            base_symbol="000660",
            base_name="SK하이닉스",
            synthetic_symbol="xyz:SKHX",
            title="SK hynix synthetic parity",
        ),
    ][: max(limit, 0)]

    # ROB-689: the four card builders are independent; gather them so the two
    # network-bound cards (index=KOSPI/naver, kimchi=upbit+binance+er-api) overlap
    # instead of summing. Order of cards + warnings is preserved exactly (gather
    # returns results positionally), so the response is byte-identical to serial.
    (
        (index_card, index_warnings),
        (stablecoin_card, stablecoin_warnings),
        (kimchi_card, kimchi_warnings),
        *synthetic_results,
    ) = await asyncio.gather(
        _build_index_card(provider, index_config),
        _build_stablecoin_card(provider),
        _build_kimchi_card(provider),
        *(_build_synthetic_card(provider, config) for config in synthetic_configs),
    )

    cards.append(index_card)
    warnings.extend(index_warnings)
    cards.append(stablecoin_card)
    warnings.extend(stablecoin_warnings)
    cards.append(kimchi_card)
    warnings.extend(kimchi_warnings)
    for card, card_warnings in synthetic_results:
        cards.append(card)
        warnings.extend(card_warnings)
```
  (`asyncio` is already imported at `:11`. `IndexParityConfig`/`SyntheticParityConfig` are already defined. Leave everything after `if not include_disabled:` unchanged.)

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_market_parity_service.py -k concurrently -v` → 1 passed.

- [ ] **Regression — existing correctness + ordering unchanged.** `uv run pytest tests/test_invest_market_parity_service.py -v`
  Expected: all four pre-existing tests still pass (`test_build_market_parity_calculates_stubbed_cards`, `..._defaults_to_approval_gated_missing_cards`, `..._redacts_provider_exception_to_warning`, `..._can_hide_disabled_cards`) — card ids/order, premium math, warning surfacing, and `include_disabled=False` filtering are all order-preserved. Also `uv run pytest tests/test_invest_market_parity_router.py -v` → passes (router stub still receives `market/include_disabled/limit`).

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-689): gather independent market-parity card builders (Fix #1)"`

---

## Task 3 — Current-only index fetch: drop the unused KOSPI history page (output-preserving, migration-0)

**Files:**
- Modify `app/mcp_server/tooling/fundamentals/_market_index.py` — add `handle_get_market_index_current_only(symbol)` next to `handle_get_market_index` (`:62`), reusing `_INDEX_META`, the leaf `_fetch_index_*_current` fetchers, `_tag_kr_index_data_state` (`:46`), and `_error_payload`. The shared handler stays untouched.
- Modify `app/services/invest_view_model/market_parity_service.py` — swap the import at `:18` to the new helper and change `get_index_quote` (`:79`) to call `handle_get_market_index_current_only(symbol)` (no `period`/`count`).
- Test (create) `tests/mcp_server/tooling/test_market_index_current_only.py` — handler-level: drops history, preserves ROB-464 tag; guard that the shared handler still returns `history`.
- Test (modify) `tests/test_invest_market_parity_service.py` — provider-level: `get_index_quote` reads `current` via the current-only helper.

**Interfaces:**
- Produces `handle_get_market_index_current_only(symbol: str) -> dict[str, Any]` → `{"indices": [ <current-row dict> ]}` (no `"history"` key). naver rows are passed through `_tag_kr_index_data_state`; unknown symbol → `ValueError`; leaf failure → `_error_payload(source=..., message=..., symbol=...)` (same shape as the shared handler's except branch).
- Consumes (unchanged, imported already at `_market_index.py:9`–`:17`): `_INDEX_META`, `_fetch_index_kr_current`, `_fetch_index_crypto_current`, `_fetch_index_us_current`, `_tag_kr_index_data_state`, `_error_payload`.
- `DefaultMarketParityProvider.get_index_quote(symbol) -> ParityQuote | None` — signature unchanged; internally now calls the current-only helper.

Steps:

- [ ] **Write failing test — helper returns current-only, no history call, tag preserved.** Create `tests/mcp_server/tooling/test_market_index_current_only.py`:
```python
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.mcp_server.tooling.fundamentals._market_index as mkt

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_current_only_kospi_skips_history_call(monkeypatch):
    current = AsyncMock(
        return_value={
            "symbol": "KOSPI",
            "name": "코스피",
            "current": 2450.5,
            "change": -45.3,
            "change_pct": -1.82,
            "open": 2390.0,
            "source": "naver",
        }
    )
    history = AsyncMock(return_value=[{"date": "2026-02-01", "close": 2450.5}])
    monkeypatch.setattr(mkt, "_fetch_index_kr_current", current)
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", history)

    result = await mkt.handle_get_market_index_current_only("KOSPI")

    assert "history" not in result
    assert result["indices"][0]["current"] == pytest.approx(2450.5)
    assert result["indices"][0]["data_state"]  # tagged by _tag_kr_index_data_state
    current.assert_awaited_once()
    history.assert_not_awaited()  # the wasted history page is never fetched


async def test_current_only_preserves_rob464_stale_override(monkeypatch):
    # change/change_pct == 0 but open != current on a FRESH clock -> ROB-464 marks
    # the KR index stale and stamps as_of. This must survive the current-only path.
    current = AsyncMock(
        return_value={
            "symbol": "KOSPI",
            "name": "코스피",
            "current": 8123.62,
            "change": 0,
            "change_pct": 0,
            "open": 8263.85,
            "source": "naver",
        }
    )
    monkeypatch.setattr(mkt, "_fetch_index_kr_current", current)
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock())
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "fresh")

    result = await mkt.handle_get_market_index_current_only("KOSPI")
    row = result["indices"][0]

    assert row["data_state"] == "stale"
    assert row["data_state_reason"] == mkt._KR_INDEX_LAGGING_REASON
    assert row["as_of"]  # stamped


async def test_current_only_unknown_symbol_raises():
    with pytest.raises(ValueError):
        await mkt.handle_get_market_index_current_only("NOPE")


async def test_current_only_leaf_failure_returns_error_payload(monkeypatch):
    monkeypatch.setattr(
        mkt, "_fetch_index_kr_current", AsyncMock(side_effect=RuntimeError("boom"))
    )
    result = await mkt.handle_get_market_index_current_only("KOSPI")
    assert "error" in result


async def test_shared_handler_still_returns_history(monkeypatch):
    # GUARD (constraint): the shared handle_get_market_index MUST keep fetching
    # history for its other callers. Task 3 must not touch it.
    monkeypatch.setattr(
        mkt,
        "_fetch_index_kr_current",
        AsyncMock(return_value={"symbol": "KOSPI", "current": 2450.5, "open": 2390.0}),
    )
    history = AsyncMock(return_value=[{"date": "2026-02-01", "close": 2450.5}])
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", history)

    result = await mkt.handle_get_market_index(symbol="KOSPI", count=1)

    assert "history" in result
    history.assert_awaited_once()
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_market_index_current_only.py -v`
  Expected: the four `current_only` tests FAIL with `AttributeError: module ... has no attribute 'handle_get_market_index_current_only'`; `test_shared_handler_still_returns_history` PASSES already (guard baseline).

- [ ] **Minimal impl — add the current-only helper.** In `app/mcp_server/tooling/fundamentals/_market_index.py`, add (below `handle_get_market_index`, reusing the already-imported helpers):
```python
async def handle_get_market_index_current_only(symbol: str) -> dict[str, Any]:
    """ROB-689: current-quote-only index fetch (drops the unused history page).

    market-parity's get_index_quote reads only the current row, but the shared
    handle_get_market_index also fetches a full history page per call. This sibling
    returns the same current-row shape ({"indices": [row]}) WITHOUT the history
    fetch. _fetch_index_kr_current (basic + 1-row price page) is kept intact so the
    'open' field is present and _tag_kr_index_data_state can still apply the ROB-464
    freshness override. The shared handle_get_market_index is intentionally NOT
    modified (its other callers consume the history).
    """
    sym = (symbol or "").strip().upper()
    meta = _INDEX_META.get(sym)
    if meta is None:
        raise ValueError(
            f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
        )
    try:
        if meta["source"] == "naver":
            current_data = await _fetch_index_kr_current(
                meta["naver_code"], meta["name"]
            )
            return {"indices": [_tag_kr_index_data_state(current_data)]}
        if meta["source"] == "coingecko":
            current_data = await _fetch_index_crypto_current(
                meta["cg_metric"], meta["name"], sym
            )
            return {"indices": [current_data]}
        current_data = await _fetch_index_us_current(
            meta["yf_ticker"], meta["name"], sym
        )
        return {"indices": [current_data]}
    except Exception as exc:
        return _error_payload(source=meta["source"], message=str(exc), symbol=sym)
```
  Add `_fetch_index_crypto_current` and `_fetch_index_us_current` to the existing import block from `fundamentals_sources_indices` (`:9`–`:17`) if not already listed (`_fetch_index_kr_current`, `_fetch_index_us_current`, and `_fetch_index_crypto_current` are all already imported — verify and add only what's missing).

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_market_index_current_only.py -v` → 5 passed.

- [ ] **Write failing test — provider uses the current-only helper.** Add to `tests/test_invest_market_parity_service.py`:
```python
from decimal import Decimal
from unittest.mock import AsyncMock

from app.services.invest_view_model.market_parity_service import (
    DefaultMarketParityProvider,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_provider_index_quote_uses_current_only(monkeypatch) -> None:
    import app.services.invest_view_model.market_parity_service as svc

    called = AsyncMock(
        return_value={
            "indices": [
                {"symbol": "KOSPI", "current": 2450.5, "source": "naver"}
            ]
        }
    )
    monkeypatch.setattr(svc, "handle_get_market_index_current_only", called)

    quote = await DefaultMarketParityProvider().get_index_quote("KOSPI")

    assert quote is not None
    assert quote.price == Decimal("2450.5")
    called.assert_awaited_once_with("KOSPI")
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_market_parity_service.py -k current_only -v`
  Expected: FAIL — `market_parity_service` has no name `handle_get_market_index_current_only` (still imports/calls `handle_get_market_index`).

- [ ] **Minimal impl — rewire get_index_quote.** In `app/services/invest_view_model/market_parity_service.py`:
  - Change the import (`:18`) from `handle_get_market_index` to `handle_get_market_index_current_only` (confirm `handle_get_market_index` is used nowhere else in this file — grep shows only `get_index_quote`).
  - In `get_index_quote` (`:79`) replace `payload = await handle_get_market_index(symbol=symbol, period="day", count=1)` with `payload = await handle_get_market_index_current_only(symbol)`. Leave the rest of the method (`_first_index_row`, `ParityQuote` construction) unchanged.

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_market_parity_service.py -k current_only -v` → 1 passed.

- [ ] **Regression — service + handler suites.** `uv run pytest tests/test_invest_market_parity_service.py tests/mcp_server/tooling/test_market_index_current_only.py -v` and `uv run pytest tests/test_mcp_fundamentals_tools.py -k Index -v` → all pass (the shared-handler tests in `TestGetMarketIndex`, including `test_single_kr_index` asserting `"history" in result`, are untouched).

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-689): current-only index fetch for market-parity, drop unused KOSPI history (Fix #2)"`

---

## Deferred / Non-goal — 30–60s response cache

Per the Global Constraint: *"A 30-60s cache must cache the data_state-tagged payload to preserve ROB-464 freshness semantics. At current traffic (6 req/7d) cache hit rate is ~0, so the cache is a low-value follow-up, not the headline."* This plan **does not** implement a cache. Rationale: at ~6 requests / 7 days the expected hit rate is ~0, so it would add a Redis dependency and a staleness-window to the read path for effectively zero latency benefit today. If a future traffic increase justifies it, the follow-up MUST cache the **`_tag_kr_index_data_state`-tagged** payload (post-tag, so the ROB-464 `data_state`/`as_of`/`data_state_reason` fields are cached, never the raw pre-tag Naver JSON), with a TTL of 30–60s, and remain read-only/migration-0. Tracked as a separate low-priority ticket, not ROB-689.
