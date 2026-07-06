# ROB-710 — /invest Batch Quotes Toss-first Flip (canary, KR→US) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

> ## ✅ PRECONDITION CLEARED (2026-07-06) — flip is now data-authorized, still staged
>
> This PR **lands the code with both per-market flags default `False`**, which is
> **byte-identical to today's KIS-first ordering** (see Global Constraints). Landing
> the code is safe and unblocked. **Turning a flag ON in production is still a
> separate, operator-gated canary act** — but the empirical evidence that authorizes
> it now exists (measured live with real RTH data on 2026-07-06):
>
> - **ROB-709 A/B go bars PASSED — both markets.** The A/B (Toss-first vs KIS-first
>   shadow compare) is the empirical evidence that Toss prices match KIS prices
>   closely enough to flip, and it cleared at RTH:
>   - **KR (Mon 09:02 KST):** divergence = **0 ticks, exact match** across all 4 KR
>     symbols (KIS `inquire_price` == Toss `/api/v1/prices`). This confirms **with
>     data** the plan's "KR current-price semantics already equivalent" assumption.
>     (The earlier Sunday 22-tick "outlier" on 087010 was a closed-market rest-value
>     artifact; at RTH it is 0 ticks.)
>   - **US (Mon ~22:40 KST):** divergence = **median 0 bps, max ~1.45 bps** across all
>     12 US symbols (Toss batch vs KIS `inquire_overseas_price` live-last, fetched
>     sequentially), well under the 10 bps go-bar. Coverage: Toss 12/12, KIS 12/12; 0
>     currency mis-keys. **ROB-708 (US endpoint → `inquire_overseas_price` live-last)
>     is MERGED** on this branch, so this is a live-last-vs-live-last comparison — the
>     precondition an earlier draft named as still-pending is now satisfied.
> - **Net: both KR and US are data-authorized to flip.** The remaining staging is
>   canary discipline, NOT a data gate: **flip KR first, observe, then US**, each
>   behind its own flag. It is NOT "US must wait for ROB-708" anymore.
>
> The flip is **flag-gated, per-market, and instantly revertible**: set the env var
> back to `false` and the very next `/invest` load is byte-identical KIS-first again.
> No deploy, no code change, no migration to revert.

> ## 🔎 SUPPORTING RATIONALE — Toss-first also fixes a real US reliability problem
>
> During the ROB-709 A/B, `InvestQuoteService._kis_fetch_us` (which fires all US
> symbols concurrently via `asyncio.gather`, `invest_quote_service.py:115-135`) had
> **11 of 12 US calls rate-limited / rejected by KIS** (only 1 succeeded), while
> calling `inquire_overseas_price` **sequentially** returned all 12 fine. So
> **today**, production `/invest` US pricing via KIS already mostly fails and leans on
> the ROB-696 Toss fallback. Flipping US to Toss-first therefore fixes a real
> reliability problem (ONE Toss `MARKET_DATA` batch vs a 12-way KIS fanout that KIS
> rate-limits), not just a TPS-efficiency preference.
>
> **Honest limitation (out of scope, follow-up):** after the flip, KIS becomes the US
> *fallback* — and that fallback is still the same concurrency-degraded
> `asyncio.gather` fanout it is today; this plan does **not** fix its rate-limiting, it
> only demotes it to a fallback (if Toss is down AND KIS is rate-limiting, US falls
> through to snapshot, fail-open, unchanged). Making the KIS US fallback
> concurrency-safe (batch or bounded-concurrency) is a **separate follow-up**, not this
> issue.

**Goal:** Invert the ROB-696 `PriceFallbackResolver` layer order for `/invest`
current-price reads from **KIS → Toss → snapshot** to **Toss batch → KIS per-symbol
→ snapshot**, behind a **per-market flag** (`invest_quotes_toss_first_kr` /
`invest_quotes_toss_first_us`, both default `False`). This is the office-hours-deferred
**Approach A**. Policy rationale: reserve KIS's scarce app-key TPS for what only KIS
does (daily-200 adjusted OHLCV, all US intraday, mature live orders); route the
batchable current-price reads to Toss (ONE call ≤200 symbols, `MARKET_DATA` 10 TPS —
which stayed up through the 2026-07-04 KIS maintenance). Toss is already the
production primary for FX + market calendar + KR warnings, so this extends an
established preference. The change is a **layer-order swap only**: the fail-open
per-layer chain, the snapshot tail, the `dict[str, float | None]` contract, and — when
both flags are `False` — the byte-identical KIS-first behavior are all preserved.
`get_quote` single-symbol rich quotes stay KIS (Toss has no OHLC), and daily-200
OHLCV stays KIS. Only the batch current-price surface consumed by `ManualHomeReader`
(`fetch_kr_prices` / `fetch_us_prices`) is affected.

## Architecture

### Current (KIS-first) flow — real refs

- `app/routers/invest_api.py:170` builds `InvestQuoteService(kis_client, db)`
  (`get_invest_home_service`, `invest_api.py:154`). The **sole** consumer of
  `fetch_kr_prices` / `fetch_us_prices` is `ManualHomeReader` (verified: `grep` finds
  no other caller in `app/`) — it prices Toss manual holdings inside two nested
  coroutines `_fetch_kr_prices` / `_fetch_us_prices` (`invest_home_readers.py:775-793`,
  ROB-702) that call `quote_service.fetch_kr_prices(kr_tickers)` (`:781`) and
  `fetch_us_prices(us_tickers)` (`:791`), run concurrently via `asyncio.gather`
  (`:800-802`).
- `InvestQuoteService.fetch_kr_prices` (`invest_quote_service.py:44-45`) →
  `_resolve(symbols, market="kr", kis_fetch=self._kis_fetch_kr)`; `fetch_us_prices`
  (`:47-48`) → `_resolve(..., market="us", kis_fetch=self._kis_fetch_us)`.
- `_resolve` (`invest_quote_service.py:50-66`): builds `toss_fetch` via
  `_build_toss_fetch()` (`:68-87`, lazy `TossReadClient.from_settings()` when
  `settings.toss_api_enabled`, else `None`), constructs a `PriceFallbackResolver`
  (`:57-62`) with `kis_fetch` / `toss_fetch` / `snapshot_fetch` / `market`, calls
  `resolver.resolve(symbols)` (`:63`), and closes any owned Toss client in `finally`
  (`:64-66`).
- `PriceFallbackResolver.resolve` (`invest_price_fallback.py:35-52`) runs the layers
  in a **hard-coded** order: KIS first with the full symbol list (`:40`), then Toss
  with only the KIS-misses (`:45-49`, skipped when `toss_fetch is None`), then
  snapshot with whatever remains (`:51`). Each layer runs through `_apply_layer`
  (`:54-80`), which is wrapped **fail-open** (`except Exception → return {}` for that
  layer, `:59-67`) and merges only non-`None` values into `results`. `_missing`
  (`:82-84`) shrinks the still-missing set after each layer; an empty missing-set
  early-returns (`:42,48`).
- Byte-identical KIS-healthy fast path: when KIS returns a price for every symbol,
  `missing` is empty after the KIS layer (`:41-42`) and the resolver returns **before
  Toss or snapshot is ever called** (though `_build_toss_fetch` may have *constructed*
  a Toss client, exactly as today).

### Target (Toss-first, flag-gated) flow

- Add two per-market bool flags to `Settings` (`config.py`), both default `False`:
  `invest_quotes_toss_first_kr`, `invest_quotes_toss_first_us`. Env:
  `INVEST_QUOTES_TOSS_FIRST_KR` / `INVEST_QUOTES_TOSS_FIRST_US`.
- Give `PriceFallbackResolver.__init__` a new `order: tuple[str, ...] =
  KIS_FIRST_ORDER` parameter (default `("kis", "toss", "snapshot")` = today).
  `resolve` becomes a small **ordered loop** over the three named layers (`kis` /
  `toss` / `snapshot`), skipping any layer whose fetcher is `None` (Toss disabled),
  shrinking the missing-set and early-returning after each — a direct generalization
  of today's three hard-coded steps. `__init__` **validates** that `order` is exactly
  the 3 known layer names (fail-loud on a typo, mirroring the config validator ethos).
- `InvestQuoteService._resolve` computes a per-market order from the flags
  (`_layer_order(market)` → `TOSS_FIRST_ORDER = ("toss", "kis", "snapshot")` when the
  market's flag is on, else `KIS_FIRST_ORDER`) and passes `order=` to the resolver.
  Nothing else in `_resolve` / `_build_toss_fetch` changes.
- **Flag OFF (default, prod today):** `_layer_order` returns `KIS_FIRST_ORDER`, the
  resolver's default — output, call sequence, and per-layer fail-open behavior are
  byte-identical to today (proven by the 8 unmodified existing resolver tests).
- **Flag ON for a market:** the resolver runs **Toss first** with the full symbol
  list (ONE ≤200-symbol `MARKET_DATA` batch), **KIS only for Toss-misses**, then
  snapshot. When Toss resolves everything, **KIS is never called** — the whole point
  (reserve KIS TPS). Fail-open per layer, snapshot tail, and the `None`-for-remainder
  contract are unchanged.
- **Safety property — flag is inert without Toss configured:** when
  `toss_api_enabled=False` (or the Toss client fails to construct), `toss_fetch` is
  `None`; the Toss-first order `("toss", "kis", "snapshot")` skips the `None` layer →
  effectively KIS → snapshot, same as today. The flag only bites when Toss is armed.

## Tech Stack

Python 3.13, uv, pytest + pytest-asyncio (markers `unit`/`asyncio`), asyncio,
pydantic-settings `Settings` (`app/core/config.py`). Toss Open API `GET /api/v1/prices`
(`MARKET_DATA` group, 10 TPS, ≤200 symbols/call) via the existing
`fetch_toss_batch_prices` adapter (`invest_price_fallback.py:98-120`) — **unchanged**.
KIS `inquire_price` (KR) / `inquire_overseas_price` (US live-last, HHDFS00000300 —
ROB-708 already landed on this branch; **NOT** `inquire_overseas_daily_price`) via the
existing `_kis_fetch_kr` (`invest_quote_service.py:101-113`) / `_kis_fetch_us`
(`:115-135`) — **unchanged**.
No new dependency, **no Redis**, **migration-0** (no DB change), no LLM, no broker
mutation.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **PRECONDITION (rollout, not code) — CLEARED 2026-07-06:** landing this PR is
  unblocked. **ROB-709's A/B has now cleared its go bars for BOTH markets** (KR 0-tick
  exact match; US median 0 bps / max ~1.45 bps, well under the 10 bps bar) and
  **ROB-708 (US endpoint fix) is MERGED**, so both flags are data-authorized to flip.
  The remaining discipline is canary sequencing only — **flip KR first, observe, then
  US**, each behind its flag — with both shipped default `False` so prod stays
  byte-identical KIS-first until an operator flips them.
- **Flag OFF ⇒ byte-identical KIS-first.** With `invest_quotes_toss_first_kr=False`
  and `invest_quotes_toss_first_us=False` (the shipped defaults), the resolver order
  is `("kis", "toss", "snapshot")` — the exact order and semantics of today. The 8
  pre-existing `PriceFallbackResolver(...)` tests (6 in
  `tests/test_price_fallback_resolver.py`, 2 in
  `tests/test_invest_price_fallback_circuit_open.py`) and the existing
  `tests/test_invest_quote_service.py` cases MUST stay green **unmodified** — that is
  the byte-identical proof. Do not edit those tests.
- **Preserve fail-open at EVERY layer.** `_apply_layer` (`invest_price_fallback.py:54-80`)
  is untouched; any layer exception (incl. `KISCircuitOpen` from ROB-699) degrades to
  `{}` for that layer and the chain continues. `fetch_kr_prices` / `fetch_us_prices`
  never raise. This holds identically for both orderings.
- **Layer-order swap ONLY.** Do NOT change: the `dict[str, float | None]` contract;
  the snapshot tail (snapshot is ALWAYS last in both orders); `_kis_fetch_kr` /
  `_kis_fetch_us` / `fetch_toss_batch_prices` / `_snapshot_latest`; the lazy Toss
  client construction/close in `_build_toss_fetch` / `_resolve` `finally`.
- **Do NOT touch get_quote single-symbol rich quotes (stays KIS — Toss has no OHLC)
  or daily-200 adjusted OHLCV (stays KIS).** This change is scoped to the batch
  current-price surface (`fetch_kr_prices` / `fetch_us_prices`).
- **Per-market flags, independent.** `fetch_kr_prices` reads only the KR flag,
  `fetch_us_prices` only the US flag — KR can be Toss-first while US stays KIS-first.
- **Order is validated fail-loud.** `PriceFallbackResolver` rejects an `order` that is
  not exactly `{"kis", "toss", "snapshot"}` (a typo must not silently drop a layer).
- **Deterministic tests:** inject fake fetchers / fake Toss client; assert call order
  and call counts; no real network, no DB, no clock.
- **migration-0.** No alembic revision, no schema change.
- Run tests with `uv run pytest <path> -v`. Lint with `make lint`. Do NOT commit
  unless the executing skill says so; each task lists its own commit message.

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/core/config.py` | Modify | Task 1 — add `invest_quotes_toss_first_kr: bool = False` + `invest_quotes_toss_first_us: bool = False` to `Settings`, after the ROB-701 sellable-cache block (`:256`). |
| `app/services/invest_price_fallback.py` | Modify | Task 2 — add `KIS_FIRST_ORDER` / `TOSS_FIRST_ORDER` constants + `order` param to `PriceFallbackResolver`; generalize `resolve` into an ordered loop; validate `order`. |
| `app/services/invest_quote_service.py` | Modify | Task 3 — `_layer_order(market)` reads the per-market flag; `_resolve` passes `order=` to the resolver. |
| `tests/test_config_flags.py` | Modify | Task 1 tests — the two new flags default `False` (append to the existing file). |
| `tests/test_invest_quotes_toss_first_config.py` | Create | Task 1 tests — `Settings()` default + kwarg + env-var override for both flags. |
| `tests/test_price_fallback_resolver.py` | Modify | Task 2 tests — **append** Toss-first / order-validation cases; the 6 existing cases stay unmodified (byte-identical proof). |
| `tests/test_invest_quote_service.py` | Modify | Task 3 tests — **append** flag-off KIS-first (Toss untouched), flag-on Toss-first (KIS skipped when Toss resolves all), per-market independence; the 7 existing cases (ROB-708 added a 7th, `test_fetch_us_prices_empty_live_last_falls_through_to_snapshot`) stay unmodified. |

> **NOT touched:**
> - `app/services/invest_price_fallback.py:54-84` — `_apply_layer` (fail-open merge) and
>   `_missing` are consumed unchanged; only `resolve` (`:35-52`) is generalized and
>   `__init__` (`:22-33`) gains a defaulted `order` param.
> - `app/services/invest_price_fallback.py:87-120` — `_TOSS_PRICE_BATCH`,
>   `TossPriceClient`, `_chunk`, `fetch_toss_batch_prices` (the ≤200 batch adapter) are
>   read as-is.
> - `app/services/invest_quote_service.py:68-149` — `_build_toss_fetch`,
>   `_snapshot_latest`, `_kis_fetch_kr`, `_kis_fetch_us`, and the ROB-709 shadow
>   passthroughs `kis_only_kr_prices` / `kis_only_us_prices` (`:137-149`) are unchanged;
>   Task 3 edits only `_resolve` (`:50-66`) and adds `_layer_order`.
> - `app/routers/invest_api.py:170` — `InvestQuoteService(kis_client, db)` stays a
>   2-arg call; no signature change.
> - `get_quote` single-symbol rich quotes, daily-200 OHLCV, and every KIS OHLCV/live-order
>   path — out of scope; KIS stays their source.
> - Any order/broker/watch/order-intent path — this is a read-only price surface. No
>   DB migration.

---

## Task 1 — Per-market Toss-first flags (migration-0)

**Files:**
- Modify `app/core/config.py` — add two fields to `Settings`, immediately after the
  ROB-701 sellable-cache block at `:256` (`toss_sellable_cache_ttl_seconds: float =
  45.0`), before the `# ROB-576` comment at `:258`.
- Modify `tests/test_config_flags.py` — append a defaults assertion.
- Create `tests/test_invest_quotes_toss_first_config.py` — default + kwarg + env-var.

**Interfaces:**
- Produces `Settings.invest_quotes_toss_first_kr: bool = False` and
  `Settings.invest_quotes_toss_first_us: bool = False` (mirrors the `bool = False`
  rollout-flag style near `:243-248`; env `INVEST_QUOTES_TOSS_FIRST_KR` /
  `INVEST_QUOTES_TOSS_FIRST_US`).

Steps:

- [ ] **Write the failing test — both flags exist, default `False`, env-overridable.**
  Create `tests/test_invest_quotes_toss_first_config.py`:
```python
from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


class TestInvestQuotesTossFirstFlags:
    def test_defaults_are_kis_first(self):
        s = Settings()
        assert s.invest_quotes_toss_first_kr is False
        assert s.invest_quotes_toss_first_us is False

    def test_kwarg_override(self):
        s = Settings(invest_quotes_toss_first_kr=True)
        assert s.invest_quotes_toss_first_kr is True
        assert s.invest_quotes_toss_first_us is False  # per-market independent

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("INVEST_QUOTES_TOSS_FIRST_KR", "true")
        monkeypatch.setenv("INVEST_QUOTES_TOSS_FIRST_US", "false")
        s = Settings()
        assert s.invest_quotes_toss_first_kr is True
        assert s.invest_quotes_toss_first_us is False
```
  And append to `tests/test_config_flags.py`:
```python
def test_invest_quotes_toss_first_flags_default_false():
    assert settings.invest_quotes_toss_first_kr is False
    assert settings.invest_quotes_toss_first_us is False
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_quotes_toss_first_config.py tests/test_config_flags.py -v`
  Expected: `AttributeError` — the fields do not exist on `Settings` yet. (Confirm no
  clash: `grep -n "invest_quotes_toss_first" app/core/config.py` returns nothing today.)

- [ ] **Minimal impl — add the fields.** In `app/core/config.py`, immediately after
  line 256 (`toss_sellable_cache_ttl_seconds: float = 45.0`), insert:
```python

    # ROB-710: per-market layer-order flip for /invest batch current-price reads.
    # False (default) => today's KIS → Toss → snapshot order, byte-identical.
    # True => TOSS batch → KIS per-symbol → snapshot (reserve KIS app-key TPS for
    # OHLCV/US-intraday/live-orders; Toss MARKET_DATA batch stayed up through the
    # 2026-07-04 KIS maintenance). Data gate CLEARED 2026-07-06: ROB-709 A/B go bars
    # passed BOTH markets (KR 0-tick exact; US median 0 bps / max ~1.45 bps) and
    # ROB-708 (US live-last endpoint) is merged. Remaining discipline is canary
    # sequencing: flip KR first, observe, then US — both shipped False. Instantly
    # revertible: set back to False and the next /invest load is KIS-first again.
    invest_quotes_toss_first_kr: bool = False
    invest_quotes_toss_first_us: bool = False
```

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_quotes_toss_first_config.py tests/test_config_flags.py -v` → all green.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-710): add per-market invest_quotes_toss_first_{kr,us} flags (default off)"`

---

## Task 2 — `order` param + ordered-loop `resolve` in `PriceFallbackResolver` (migration-0)

**Files:**
- Modify `app/services/invest_price_fallback.py` — add module constants
  `KIS_FIRST_ORDER` / `TOSS_FIRST_ORDER`; add `order` param + validation to
  `__init__` (`:22-33`); generalize `resolve` (`:35-52`) into an ordered loop.
  `_apply_layer` (`:54-80`) and `_missing` (`:82-84`) are **unchanged**.
- Modify `tests/test_price_fallback_resolver.py` — **append** new cases; do NOT edit
  the existing 6.

**Interfaces:**
- `KIS_FIRST_ORDER: tuple[str, ...] = ("kis", "toss", "snapshot")`
- `TOSS_FIRST_ORDER: tuple[str, ...] = ("toss", "kis", "snapshot")`
- `PriceFallbackResolver.__init__(self, *, kis_fetch, toss_fetch, snapshot_fetch,
  market, order: tuple[str, ...] = KIS_FIRST_ORDER)` — validates
  `set(order) == {"kis", "toss", "snapshot"}` and `len(order) == 3`, else `ValueError`.
- `resolve(symbols)` behavior:
  - `order=KIS_FIRST_ORDER` (default) → **today's exact behavior** (KIS full list →
    Toss on misses (skipped if `toss_fetch is None`) → snapshot on remainder).
  - `order=TOSS_FIRST_ORDER` → Toss full list (skipped if `None`) → KIS on Toss-misses
    → snapshot on remainder.
  - Empty `symbols` → `{}` (unchanged early return).

Steps:

- [ ] **Write the failing tests — Toss-first order, `None`-layer skip, validation.**
  Append to `tests/test_price_fallback_resolver.py` (reuse the module's existing
  `_fetcher` helper and `pytestmark`):
```python
from app.services.invest_price_fallback import (  # add to existing import
    KIS_FIRST_ORDER,
    TOSS_FIRST_ORDER,
)


async def test_toss_first_order_consults_toss_before_kis():
    kis_calls, toss_calls = [], []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": 11.0}, calls=kis_calls),
        toss_fetch=_fetcher({"A": 99.0, "B": 98.0}, calls=toss_calls),
        snapshot_fetch=_fetcher({}),
        market="kr",
        order=TOSS_FIRST_ORDER,
    )
    out = await resolver.resolve(["A", "B"])
    # Toss resolved everything first => KIS never consulted; Toss values win.
    assert out == pytest.approx({"A": 99.0, "B": 98.0})
    assert toss_calls == [["A", "B"]]
    assert kis_calls == []


async def test_toss_first_falls_to_kis_only_for_toss_misses():
    kis_calls, toss_calls, snap_calls = [], [], []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"B": 20.0, "C": None}, calls=kis_calls),
        toss_fetch=_fetcher({"A": 99.0}, calls=toss_calls),  # B, C miss on Toss
        snapshot_fetch=_fetcher({"C": 30.0}, calls=snap_calls),
        market="kr",
        order=TOSS_FIRST_ORDER,
    )
    out = await resolver.resolve(["A", "B", "C"])
    assert out == pytest.approx({"A": 99.0, "B": 20.0, "C": 30.0})
    assert toss_calls == [["A", "B", "C"]]  # Toss first, full list
    assert kis_calls == [["B", "C"]]        # KIS only Toss-misses
    assert snap_calls == [["C"]]            # snapshot only the remainder


async def test_toss_first_with_toss_disabled_skips_to_kis_then_snapshot():
    kis_calls, snap_calls = [], []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0}, calls=kis_calls),
        toss_fetch=None,  # Toss disabled => layer skipped even though it is first
        snapshot_fetch=_fetcher({"B": 5.0}, calls=snap_calls),
        market="kr",
        order=TOSS_FIRST_ORDER,
    )
    out = await resolver.resolve(["A", "B"])
    assert out == pytest.approx({"A": 10.0, "B": 5.0})
    assert kis_calls == [["A", "B"]]  # KIS runs first when Toss is None
    assert snap_calls == [["B"]]


async def test_toss_first_fail_open_when_toss_raises_then_kis_fills():
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": 11.0}),
        toss_fetch=_fetcher({}, boom=True),  # Toss layer errors -> fail-open {}
        snapshot_fetch=_fetcher({}),
        market="kr",
        order=TOSS_FIRST_ORDER,
    )
    out = await resolver.resolve(["A", "B"])
    assert out == pytest.approx({"A": 10.0, "B": 11.0})  # KIS filled after Toss error


async def test_default_order_constant_matches_signature_default():
    assert KIS_FIRST_ORDER == ("kis", "toss", "snapshot")
    assert TOSS_FIRST_ORDER == ("toss", "kis", "snapshot")


async def test_invalid_order_fails_loud():
    with pytest.raises(ValueError, match="order"):
        PriceFallbackResolver(
            kis_fetch=_fetcher({}),
            toss_fetch=None,
            snapshot_fetch=_fetcher({}),
            market="kr",
            order=("kis", "toss"),  # missing 'snapshot' -> reject
        )
```

- [ ] **Run it — fails.** `uv run pytest tests/test_price_fallback_resolver.py -v`
  Expected: **the whole module errors at collection.** The appended top-level
  `from app.services.invest_price_fallback import (KIS_FIRST_ORDER, TOSS_FIRST_ORDER)`
  references names that do not exist yet (`ImportError`), so pytest cannot import the
  file and reports a **collection error covering every case in it — the 6 pre-existing
  included, not just the 6 new** (contrary to a naive "only the new cases fail"; a
  module-top import that fails takes the whole file down). That collection error IS the
  valid RED signal. After Task 2 impl adds the constants + `order` kwarg, the 6
  pre-existing cases return to green **unmodified** (the byte-identical proof) and the 6
  new cases pass. (If you specifically want to watch the 6 pre-existing stay green
  *during* RED, use a local `from ... import KIS_FIRST_ORDER, TOSS_FIRST_ORDER` inside
  each new test body instead of the module-top import — the final green state is
  identical either way.)

- [ ] **Minimal impl — constants + `order` param + ordered loop.** In
  `app/services/invest_price_fallback.py`:
  1. Add module constants after `Fetcher = ...` (`:14`):
```python
KIS_FIRST_ORDER: tuple[str, ...] = ("kis", "toss", "snapshot")
TOSS_FIRST_ORDER: tuple[str, ...] = ("toss", "kis", "snapshot")
_KNOWN_LAYERS = frozenset(KIS_FIRST_ORDER)
```
  2. Change `__init__` (`:22-33`) to accept and validate `order`:
```python
    def __init__(
        self,
        *,
        kis_fetch: Fetcher,
        toss_fetch: Fetcher | None,
        snapshot_fetch: Fetcher,
        market: str,
        order: tuple[str, ...] = KIS_FIRST_ORDER,
    ) -> None:
        if len(order) != len(_KNOWN_LAYERS) or set(order) != _KNOWN_LAYERS:
            # Fail-loud: a typo must not silently drop a fallback layer.
            raise ValueError(
                f"order must be a permutation of {sorted(_KNOWN_LAYERS)}, got {order!r}"
            )
        self._kis_fetch = kis_fetch
        self._toss_fetch = toss_fetch
        self._snapshot_fetch = snapshot_fetch
        self._market = market
        self._order = order
```
  3. Replace `resolve` (`:35-52`) with the ordered loop (a direct generalization of
     the three hard-coded steps; `_apply_layer` / `_missing` unchanged):
```python
    async def resolve(self, symbols: list[str]) -> PriceMap:
        if not symbols:
            return {}
        results: PriceMap = dict.fromkeys(symbols, None)
        layers: dict[str, Fetcher | None] = {
            "kis": self._kis_fetch,
            "toss": self._toss_fetch,
            "snapshot": self._snapshot_fetch,
        }
        missing = symbols  # first layer runs on the full list
        for name in self._order:
            fetch = layers[name]
            if fetch is None:  # e.g. Toss disabled -> skip this layer
                continue
            await self._apply_layer(name, fetch, missing, results)
            missing = self._missing(symbols, results)
            if not missing:
                return results
        return results
```

- [ ] **Run it — passes.** `uv run pytest tests/test_price_fallback_resolver.py tests/test_invest_price_fallback_circuit_open.py -v`
  Expected: all pass — the 6 new cases green, AND the 6 existing resolver cases + the
  2 circuit-open cases still green **unmodified** (byte-identical proof: default order
  reproduces today's KIS-first call sequences `toss_calls == [["B", "C"]]`,
  `snap_calls == [["B"]]`, KIS-only-happy-path, all-layers-fail-open, empty-input).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-710): PriceFallbackResolver order param + ordered-loop resolve (default byte-identical)"`

---

## Task 3 — Per-market order wiring in `InvestQuoteService._resolve` (migration-0)

**Files:**
- Modify `app/services/invest_quote_service.py` — import `KIS_FIRST_ORDER` /
  `TOSS_FIRST_ORDER` (extend the existing `invest_price_fallback` import at `:14-18`);
  add `_layer_order(self, market)`; pass `order=self._layer_order(market)` to the
  resolver in `_resolve` (`:57-62`). `_build_toss_fetch` / `_snapshot_latest` /
  `_kis_fetch_*` / the `finally` close are **unchanged**.
- Modify `tests/test_invest_quote_service.py` — **append** flag cases; do NOT edit the
  7 existing cases (ROB-708 added the 7th,
  `test_fetch_us_prices_empty_live_last_falls_through_to_snapshot`).

**Interfaces:**
- `_layer_order(self, market: str) -> tuple[str, ...]` — reads
  `settings.invest_quotes_toss_first_kr` for `market == "kr"`,
  `settings.invest_quotes_toss_first_us` for `market == "us"` (via
  `getattr(settings, ..., False)`, mirroring the `toss_api_enabled` read at `:72`);
  returns `TOSS_FIRST_ORDER` when the market's flag is truthy, else `KIS_FIRST_ORDER`.
- `_resolve` passes `order=self._layer_order(market)`; everything else identical.

Steps:

- [ ] **Write the failing tests — flag off KIS-first (Toss untouched), flag on
  Toss-first (KIS skipped), per-market independence.** Append to
  `tests/test_invest_quote_service.py`:
```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_flag_off_is_kis_first_toss_untouched(monkeypatch):
    # Default (flag off): KIS resolves everything => Toss must NOT be consulted.
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.invest_quotes_toss_first_kr",
        False,
        raising=False,
    )

    class _FakeToss:
        def __init__(self):
            self.called = False

        async def prices(self, symbols):
            self.called = True
            return []

    toss = _FakeToss()
    service = InvestQuoteService(MagicMock(), MagicMock(), toss_client=toss)
    service._market_data = AsyncMock()
    service._market_data.inquire_price.return_value = pd.DataFrame(
        [{"close": 70000.0}], index=["005930"]
    )

    out = await service.fetch_kr_prices(["005930"])
    assert out == pytest.approx({"005930": 70000.0})
    assert toss.called is False  # KIS-first: Toss never reached when KIS healthy


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_flag_on_is_toss_first_kis_skipped_when_toss_resolves(monkeypatch):
    from decimal import Decimal

    from app.services.brokers.toss.dto import TossPrice

    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.invest_quotes_toss_first_kr",
        True,
        raising=False,
    )

    class _FakeToss:
        async def prices(self, symbols):
            return [
                TossPrice(
                    symbol=s, timestamp=None, last_price=Decimal("123"), currency="KRW"
                )
                for s in symbols
            ]

    service = InvestQuoteService(MagicMock(), MagicMock(), toss_client=_FakeToss())
    service._market_data = AsyncMock()  # KIS present but must NOT be called

    out = await service.fetch_kr_prices(["005930", "000660"])
    assert out == pytest.approx({"005930": 123.0, "000660": 123.0})
    service._market_data.inquire_price.assert_not_called()  # Toss-first won


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flags_are_per_market_independent(monkeypatch):
    from decimal import Decimal

    from app.services.brokers.toss.dto import TossPrice

    # KR flipped, US NOT flipped.
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.invest_quotes_toss_first_kr",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.invest_quotes_toss_first_us",
        False,
        raising=False,
    )

    class _FakeToss:
        def __init__(self):
            self.calls = 0

        async def prices(self, symbols):
            self.calls += 1
            return [
                TossPrice(
                    symbol=s, timestamp=None, last_price=Decimal("5"), currency="KRW"
                )
                for s in symbols
            ]

    toss = _FakeToss()
    service = InvestQuoteService(MagicMock(), MagicMock(), toss_client=toss)
    service._market_data = AsyncMock()
    # KR KIS resolves a DISTINCT price (70000). This makes the case a real RED
    # discriminator: pre-impl (KIS-first) KR takes KIS's 70000 and NEVER reaches
    # Toss (kr == {"005930": 70000.0}, toss.calls == 0), so the kr == {5.0} and
    # toss.calls == 1 assertions both FAIL. Post-impl with the KR flag ON
    # (Toss-first) KIS is skipped for KR, so kr == {5.0} (Toss's price) PROVES the
    # flip and toss.calls == 1. (If KR KIS were left un-resolved it would MISS and
    # fall through to Toss even pre-impl -> toss.calls == 1 and kr == {5.0} in RED
    # too -> the test would pass before impl and prove nothing. The distinct KIS
    # price is what forces a genuine pre-impl failure.)
    service._market_data.inquire_price.return_value = pd.DataFrame(
        [{"close": 70000.0}], index=["005930"]
    )
    # US KIS path resolves (flag off => KIS-first => Toss not reached for US).
    # ROB-708 landed: _kis_fetch_us now reads the LIVE-LAST endpoint
    # (inquire_overseas_price / HHDFS00000300), NOT inquire_overseas_daily_price. The
    # US KIS mock MUST therefore be on inquire_overseas_price — otherwise US misses
    # KIS, falls through to Toss, and the toss.calls == 1 / us == {150.0} assertions
    # both break.
    service._market_data.inquire_overseas_price.return_value = pd.DataFrame(
        [{"close": 150.0, "previous_close": 148.0, "volume": 1000}]
    )
    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )

    kr = await service.fetch_kr_prices(["005930"])   # Toss-first
    us = await service.fetch_us_prices(["AAPL"])     # KIS-first

    assert kr == pytest.approx({"005930": 5.0})
    assert us == pytest.approx({"AAPL": 150.0})
    assert toss.calls == 1  # Toss consulted for KR only, not US (US flag off)
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_quote_service.py -v -k "flag or per_market"`
  Expected: today `_resolve` always builds the resolver with the default KIS-first
  order (it ignores the flags), so:
  - `test_kr_flag_on_is_toss_first_kis_skipped_when_toss_resolves` FAILS — KIS-first
    means `inquire_price` IS consulted, so `inquire_price.assert_not_called()` fails.
    (Its `out == {123, 123}` assertion alone would *pass* even pre-impl, because a KIS
    miss falls through to Toss; the `assert_not_called()` is the true discriminator.)
  - `test_flags_are_per_market_independent` FAILS — KIS-first means KR takes KIS's
    distinct 70000 and never reaches Toss, so `kr == {"005930": 5.0}` and
    `toss.calls == 1` both fail (kr is `{70000.0}`, toss.calls is 0).
  - `test_kr_flag_off_is_kis_first_toss_untouched` PASSES today (flag off == today)
    and stays green after impl — a regression lock.

- [ ] **Minimal impl — import + `_layer_order` + pass `order=`.** In
  `app/services/invest_quote_service.py`:
  1. Extend the import at `:14-18`:
```python
from app.services.invest_price_fallback import (
    KIS_FIRST_ORDER,
    TOSS_FIRST_ORDER,
    Fetcher,
    PriceFallbackResolver,
    fetch_toss_batch_prices,
)
```
  2. Add the helper (e.g. directly above `_resolve` at `:50`):
```python
    def _layer_order(self, market: str) -> tuple[str, ...]:
        # ROB-710: per-market flag flips KIS→Toss→snapshot to Toss→KIS→snapshot.
        # Default (both flags False) == today's KIS-first, byte-identical.
        if market == "kr":
            toss_first = bool(
                getattr(settings, "invest_quotes_toss_first_kr", False)
            )
        elif market == "us":
            toss_first = bool(
                getattr(settings, "invest_quotes_toss_first_us", False)
            )
        else:
            toss_first = False
        return TOSS_FIRST_ORDER if toss_first else KIS_FIRST_ORDER
```
  3. Pass `order=` in the resolver construction (`:57-62`):
```python
            resolver = PriceFallbackResolver(
                kis_fetch=kis_fetch,
                toss_fetch=toss_fetch,
                snapshot_fetch=lambda syms: self._snapshot_latest(market, syms),
                market=market,
                order=self._layer_order(market),
            )
```

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_quote_service.py -v`
  Expected: all pass — the 3 new flag cases green AND the 7 pre-existing cases green
  **unmodified** (`test_fetch_kr_prices`, `test_fetch_us_prices_uses_live_last_endpoint`,
  `..._fetch_kr_prices_falls_back_to_toss_then_snapshot`,
  `..._fetch_us_prices_toss_disabled_uses_snapshot`,
  `..._fetch_kr_prices_all_layers_down_returns_none`,
  `..._fetch_kr_prices_toss_enabled_but_misconfigured_is_fail_open`,
  `test_fetch_us_prices_empty_live_last_falls_through_to_snapshot` — all exercise the
  flag-off KIS-first default, byte-identical proof).

- [ ] **Regression — full resolver + quote + reader surfaces.**
  `uv run pytest tests/test_price_fallback_resolver.py tests/test_invest_price_fallback_circuit_open.py tests/test_invest_quote_service.py tests/test_invest_quotes_toss_first_config.py tests/test_config_flags.py -q`
  Expected: no failures.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-710): wire per-market Toss-first order into InvestQuoteService (flag-gated)"`

---

## Rollout / canary procedure (operator, post-merge)

**This section is executed by an operator, gated on the PRECONDITION — it is not part
of the code PR.**

1. **Land the PR** (both flags default `False`). Prod is byte-identical KIS-first;
   nothing changes at runtime. This is safe and unblocked.
2. **ROB-709's A/B go bars are CLEARED (2026-07-06, both markets)** — KR 0-tick exact
   match, US median 0 bps / max ~1.45 bps (Toss-vs-KIS agreement within tolerance). The
   data gate is satisfied; proceed with canary sequencing (KR first, then US).
3. **Flip KR first** — set `INVEST_QUOTES_TOSS_FIRST_KR=true` in the prod env and
   reload. KR manual-holding prices on `/invest` now come from the Toss `MARKET_DATA`
   batch first, KIS only for Toss-misses. Observe: `/invest` KR prices populate, KIS
   app-key TPS headroom improves, Sentry `invest.home.manual.fetch_kr_prices` span
   error rate does not regress. **Revert instantly** by setting the flag back to
   `false` if anything looks wrong — the next load is KIS-first again.
4. **US flip — both data gates already satisfied.** ROB-708 (US endpoint → live-last
   `inquire_overseas_price`) is MERGED and ROB-709's US bars cleared (median 0 bps /
   max ~1.45 bps), so US is a live-last-vs-live-last comparison, not the old
   daily-close mismatch. After KR is stable, set `INVEST_QUOTES_TOSS_FIRST_US=true` and
   observe `invest.home.manual.fetch_us_prices`. **Reliability bonus:** Toss-first also
   removes the 12-way KIS US fanout that KIS rate-limits today (11/12 rejected in the
   A/B), so US pricing should get MORE reliable, not just cheaper on TPS. (KIS remains
   the US *fallback* and stays concurrency-degraded — see the Supporting-Rationale
   limitation note; making it concurrency-safe is a separate follow-up.)
5. When both markets are Toss-first and stable, a follow-up may make `True` the default
   (out of scope here; keep the flags for instant revert).

## Done criteria

- Both flags land default `False`; prod `/invest` is byte-identical KIS-first (the 8
  pre-existing resolver tests + the 7 pre-existing quote-service tests stay green
  **unmodified**).
- With a market's flag `True`, that market's batch price reads consult **Toss first**
  (ONE ≤200-symbol `MARKET_DATA` batch), **KIS only for Toss-misses**, then snapshot;
  when Toss resolves everything, KIS is never called (`inquire_price.assert_not_called`
  in `test_kr_flag_on_...`).
- Flags are per-market independent (`test_flags_are_per_market_independent`): KR
  Toss-first while US stays KIS-first.
- Fail-open per layer is preserved for both orders
  (`test_toss_first_fail_open_when_toss_raises_then_kis_fills`, plus the unmodified
  all-layers-down and circuit-open tests); a bad `order` fails loud
  (`test_invalid_order_fails_loud`).
- `get_quote` single-symbol quotes and daily-200 OHLCV are untouched (KIS stays their
  source — not referenced by this change).
- `make lint` clean; no alembic revision added (migration-0).
- The flip is instantly revertible via the env var; the US flip's data gates (ROB-708
  landed + ROB-709 US bars) are already CLEARED (2026-07-06), so both flips remain
  operator-gated only as a canary sequence — KR first, then US (stated in the
  PRECONDITION and Rollout section).
```
