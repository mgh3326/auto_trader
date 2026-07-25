# ROB-709 — Quotes A/B Parity Shadow: Toss `prices()` batch vs KIS batch layer (Toss-first flip precondition) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

> ⚠️ **PRECONDITION — ROB-708 must land first (US endpoint → live-last).** The KIS *US* batch layer today reads a **daily close**, not a live-last quote: `InvestQuoteService._kis_fetch_us` (`app/services/invest_quote_service.py:115-130`) calls `inquire_overseas_daily_price(symbol, exchange_code=..., n=1, period="D")` and takes `df.iloc[0]["close"]` (`:121-124`). The KIS *KR* layer is already live-last — `_kis_fetch_kr` (`:101-113`) calls `inquire_price(symbol, market="J")` → `stck_prpr` 현재가 (`domestic_market_data.py:191-227`). So **US divergence measured today is a known artifact** (Toss-live vs KIS-daily-close), NOT a promotion signal. ROB-708 flips `_kis_fetch_us` to the existing live-last endpoint `inquire_overseas_price` (HHDFS00000300, `overseas_market_data.py:236-255`). **Until ROB-708 lands, this probe runs and reports US divergence but marks the US-divergence go-bar `not_evaluable` and the overall decision `blocked`.** Coverage, currency-correctness, and latency/error-rate do NOT depend on ROB-708 and are evaluated now.

**Goal:** Build a **read-only A/B shadow** (zero user-facing behavior change, zero writes, zero mutations) that, over the /invest KR+US symbol universe, calls **both** the Toss `prices()` batch **and** the raw KIS batch layer and emits structured **go/no-go** metrics for the ROB-710 decision to flip batch current-price reads to Toss-first. Per symbol it records: **(1) coverage** — does Toss echo every ticker as-passed, especially dotted US (`BRK.B`); count silent drops; **(2) price divergence** — Toss `last_price` vs KIS live-last, distribution (median/p99), split KR-RTH vs US-RTH; **(3) currency correctness** — KRW vs USD keyed on market (the resolver *discards* Toss's currency tag, so this shadow is the ONLY guard against a mis-key); **(4) off-hours semantics** — what Toss `last_price`/`timestamp` returns when the US market is closed; **(5) Toss latency p95 + error rate** under full fanout (canary evidence for promotion). Delivery is an **operator-run `scripts/` probe (dry-run-default)** whose comparison/stats engine lives in a pure, fully unit-tested module. **Go bars:** coverage ≥ 99.5% (allowlist for known misses, **0** silent drops); KR p99 |div| ≤ 1 KRX tick; US p99 ≤ ~10 bps; **0** currency mis-keys; Toss wall-time/error-rate no worse than the current KIS-primary path.

## Architecture

### Current (production) price path — real refs

- `/invest` prices flow through `InvestQuoteService.fetch_kr_prices` / `fetch_us_prices` (`app/services/invest_quote_service.py:44-48`), each delegating to `_resolve(...)` (`:50-67`) which runs the **fail-open fallback chain** `PriceFallbackResolver` (`app/services/invest_price_fallback.py:17-52`): **KIS → Toss → snapshot**, merging only non-`None` values and shrinking the missing-set each layer.
- **KIS layer (KR)** — `_kis_fetch_kr` (`invest_quote_service.py:101-113`): per-symbol `asyncio.gather` of `MarketDataClient.inquire_price(symbol, market="J")` → `float(df.iloc[0]["close"])`. `inquire_price` returns `stck_prpr` 현재가 (`domestic_market_data.py:191-227`) = **live-last during RTH**.
- **KIS layer (US)** — `_kis_fetch_us` (`invest_quote_service.py:115-130`): per-symbol `asyncio.gather` of `inquire_overseas_daily_price(symbol, exchange_code=exchange, n=1, period="D")` → `float(df.iloc[0]["close"])` = **daily close** (⚠️ ROB-708 precondition target — see top-of-file callout; the live-last endpoint `inquire_overseas_price` HHDFS00000300 already exists at `overseas_market_data.py:236-255`).
- **Toss layer** — `fetch_toss_batch_prices(client, symbols)` (`invest_price_fallback.py:98-120`): **one** `GET /api/v1/prices` per ≤200-symbol chunk (`_TOSS_PRICE_BATCH = 200`, `:87`; `_chunk`, `:94-95`), uppercases requests, **echo-matches** by `by_upper = {s.upper(): s}` and `str(price.symbol).upper()` (`:105-112`), returns `{requested_symbol: float(price.last_price)}`. **It throws away `price.currency` and `price.timestamp`** — only `last_price` survives. `TossReadClient.prices` (`client.py:161-169`) is in the `MARKET_DATA` group (10 TPS), `parse_prices` (`dto.py:120-129`) → `TossPrice(symbol, timestamp, last_price, currency)` (`dto.py:38-43`). Batch guard `_symbols_param` requires `1 ≤ len ≤ 200` (`client.py:138-142`).
- Toss is **already the production primary** for FX + market calendar + KR warnings; it stayed up through the 2026-07-04 KIS maintenance. ROB-696 built exactly this KIS→Toss→snapshot fallback; ROB-710 wants to make Toss the **primary** for batch current-price reads (reserve scarce KIS app-key TPS for daily-200 adjusted OHLCV, all US intraday, mature live orders).

### Target (this issue) — read-only shadow, no hot-path change

New pure module **`app/services/quote_parity_shadow.py`** (no I/O — `Decimal`/lists in, dataclasses out):

```
requested KR+US symbols
        │
        ├── Toss side:  toss_prices_fn(chunk≤200) -> list[TossPrice]      (timed per batch)
        │                    ↓ echo-match (SAME by_upper/.upper() as fetch_toss_batch_prices)
        │              coverage(drops, dotted BRK.B) · currency(KR→KRW/US→USD) · last_price · timestamp(off-hours)
        │
        └── KIS side:   kis_kr_fetch(kr) / kis_us_fetch(us) -> {sym: float|None}  (timed, raw layer, NO fallback)
                             ↓
     per-symbol pairs (toss_last, kis_last where BOTH present)
        │
        ▼
  summarize_divergence(KR: tick-normalized · US: bps)   summarize_latency(toss batches + KIS wall)
        │
        ▼
  evaluate_go_no_go(coverage, kr_div, us_div, currency, toss_lat, kis_lat, us_kis_live_last=<ROB-708 gate>)
        │
        ▼   go / no_go / blocked  +  per-bar pass|fail|not_evaluable
```

Orchestrator `run_quote_parity_probe(...)` takes **injected** `toss_prices_fn`, `kis_kr_fetch`, `kis_us_fetch`, and a `clock` — fully testable with fakes and a fake clock, **no network**. `scripts/quote_parity_shadow_probe.py` is a **thin operator harness**: enumerate the /invest KR+US universe (Toss `manual_holdings` via `ManualHoldingsService.get_holdings_by_user(user_id, broker_type="toss")`, `manual_holdings_service.py:130`, or an operator `--symbols-file`), build real `TossReadClient.from_settings()` + `SafeKISClient()` + `InvestQuoteService`, wire `toss_prices_fn=client.prices` and the two KIS fetchers via new **read-only public passthroughs** `InvestQuoteService.kis_only_kr_prices` / `kis_only_us_prices` (Task 6 — raw KIS batch layer, bypassing the fallback so the A/B is head-to-head), run the probe, print structured JSON, and exit `0=go / 2=no_go|blocked / 1=crash`. **Dry-run-default:** with no `--confirm-live` the script enumerates the universe and prints the planned batch counts but performs **zero** network calls.

**Why a probe, not inline shadow-logging behind a flag:** inline double-fetch on every `/invest` load would (a) add hot-path latency and (b) burn the exact **scarce KIS app-key TPS** the whole KIS↔Toss preference is trying to *reserve* — defeating ROB-696. The promotion decision is a **one-time canary**, so a bounded operator-run probe that emits aggregate go/no-go is the correct shape. (Recorded in key_decisions.)

## Tech Stack

Python 3.13, uv, pytest + pytest-asyncio (`@pytest.mark.asyncio`, markers `unit`/`asyncio`), asyncio, `dataclass` reports, stdlib `statistics` + a pure nearest-rank percentile, stdlib `time.monotonic` (injected), `Decimal`. Reuses existing `TossReadClient.prices` (`client.py:161`), `TossPrice`/`parse_prices` (`dto.py:38,120`), `InvestQuoteService` KIS layer (`invest_quote_service.py:101,115`), `to_db_symbol` (`app/core/symbol.py:26`), `ManualHoldingsService` (`manual_holdings_service.py:130`), `MarketType` (`app/models/manual_holdings.py:36-40`), the operator-script scaffolding from `scripts/diagnose_invest_screener_toss_parity.py` (secret-reject `:57-68`, `setup_logging_and_sentry` `app/core/cli.py:21`, `AsyncSessionLocal` `app/core/db.py:50`). **No new dependency, no Redis, migration-0** (no DB / alembic change). Toss `GET /api/v1/prices` (`MARKET_DATA`, 10 TPS) — call frequency unchanged, rate never raised.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **READ-ONLY shadow — ZERO user-facing behavior change.** No DB write, no broker/order/watch/order-intent mutation, no change to the `/invest` hot path, no change to `PriceFallbackResolver` / `fetch_toss_batch_prices` / the resolver chain / `TossReadClient.prices` / any rate-limit group. The shadow **observes** the production price seams; it never alters them.
- **PRECONDITION ROB-708 (US → live-last).** Until `_kis_fetch_us` reads a live-last quote, US divergence is a daily-close-vs-live artifact: `evaluate_go_no_go(..., us_kis_live_last=False)` MUST mark the US-divergence bar `not_evaluable` and the overall decision `blocked`. The operator passes `--us-kis-live-last` **only after ROB-708 has landed**.
- **Toss batch ≤ 200 symbols per `prices()` call** (`_symbols_param` guard, `client.py:138-142`); reuse the existing ≤200 chunking (`_TOSS_PRICE_BATCH`, `invest_price_fallback.py:87`). Never exceed; never raise a rate.
- **Currency mis-key is the money bug this shadow exists to catch.** The resolver discards Toss's `currency` tag (`fetch_toss_batch_prices` returns only `last_price`), so a KR symbol tagged `USD` (or US tagged `KRW`) would silently mis-value a position. `check_currency` MUST surface any KR≠KRW / US≠USD as a mis-key; the go-bar is **0** mis-keys.
- **Deterministic tests:** inject the Toss `prices` fn, both KIS fetch fns, and the clock; assert exact metrics, bar statuses, and exit codes; **NO real network, NO DB** in unit tests; reset any module singletons in fixtures.
- **Dry-run-default operator script:** no live Toss/KIS fanout unless `--confirm-live` is passed. Structured go/no-go JSON; exit `0=go`, `2=no_go|blocked`, `1=crash`. No secrets printed; `--symbols-file` is secret-rejected.
- **migration-0** (no alembic revision, no schema/config-DB change).
- **Test-file imports live in the single top-of-file import block.** The per-task test snippets below show new `from app.services.quote_parity_shadow import …` lines beside the code they enable for readability, but ruff **E402** forbids mid-file module imports (only `tests/conftest.py` is exempted, `pyproject.toml:153-154`). When implementing, merge each task's new imports into the existing top-of-file import group; paste only the test classes/helpers after it.
- Run tests with `uv run pytest <path> -v`. Lint with `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

## Approach / decision note

- **Delivery = operator-run probe + pure engine; NOT inline shadow-logging.** See "Why a probe" above. The pure engine (`quote_parity_shadow.py`) is fully unit-tested with fakes; the script is a thin harness. (Recorded in key_decisions.)
- **Compare against the RAW KIS batch layer (no fallback), head-to-head with the RAW Toss batch.** The probe wires the KIS fetchers directly, bypassing `PriceFallbackResolver` — otherwise KIS-then-Toss fallback would contaminate the KIS column with Toss values and the A/B would be meaningless. New read-only public passthroughs `kis_only_kr_prices`/`kis_only_us_prices` expose this seam explicitly (also the seam ROB-710 will flip). ruff `SLF` is not in the select set (`pyproject.toml:136-144`), so reaching `_kis_fetch_*` would not fail lint — but a named public passthrough documents intent and is reused by the promotion PR. (Recorded in key_decisions.)
- **Echo-match is byte-identical to production.** Coverage uses the SAME `by_upper`/`str(price.symbol).upper()` logic as `fetch_toss_batch_prices` (`invest_price_fallback.py:105-112`) so "silent drop" reflects exactly what production would drop — critically for dotted US (`BRK.B`): if Toss echoes `BRK.B` it matches; if it drops it or returns `BRK` it is a counted silent drop. **The probe must ALSO uppercase the *outbound* request** the same way production does (`fetch_toss_batch_prices` sends `_chunk([s.upper() for s in symbols])`, `invest_price_fallback.py:108`). The default DB universe is already-uppercase, but a `--symbols-file` may carry lowercase input, and `to_db_symbol` (`symbol.py:26`) does NOT uppercase — so `_fetch_toss_side` uppercases each batch before calling `toss_prices_fn`, or the measured coverage would not be the coverage production would see. (Recorded in key_decisions.)
- **KRX equity tick table is defined fresh — do NOT reuse `paper_fills._TICK_BANDS`.** That table (`app/services/paper_fills.py:14-27`) is the **Upbit KRW** tick ladder, not KRX cash-equity. KR divergence normalizes by the 2023-01 KRX equity tick bands (`<2,000→1 / <5,000→5 / <20,000→10 / <50,000→50 / <200,000→100 / <500,000→500 / ≥500,000→1,000`). (Recorded in key_decisions.)
- **Latency go-bar is wall-time-based; Toss per-batch p50/p95/p99 is supplementary canary evidence.** The fair "how long does /invest wait" metric is total wall time to resolve the same symbol set: Toss (sum of its ≤200 chunk calls) vs KIS (one internal `gather` fanout). The bar is `toss_total_wall ≤ kis_total_wall` AND `toss_error_rate ≤ kis_error_rate`; per-batch percentiles are emitted for the ROB-710 reviewer. (Recorded in key_decisions.)
- **Off-hours semantics are recorded, not gated.** When the US market is closed, the probe records Toss's returned `last_price`/`timestamp` verbatim (per-symbol, no fabrication) so ROB-710 can see whether Toss serves a stale last or a null. This is descriptive evidence, not a pass/fail bar. (Recorded in open_questions for reviewer sign-off on what "acceptable off-hours last" means.)
- **Universe:** default = distinct active Toss `manual_holdings` tickers split KR/US (the exact production hot-path symbols), via `ManualHoldingsService.get_holdings_by_user(user_id, broker_type="toss")`; `--symbols-file` (secret-rejected CSV/JSON) overrides for a broader canary set and to force dotted-symbol coverage. `--limit` bounds each market. (Recorded in open_questions for reviewer sign-off on whether KIS live holdings should also be enumerated.)

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/services/quote_parity_shadow.py` | Create | Tasks 1–5 — pure engine: `krx_tick_size` + `_percentile` (T1), `classify_coverage` (T2), `check_currency` (T3), `summarize_divergence` + `summarize_latency` (T4), `GoBars` + `evaluate_go_no_go` (T5). |
| `app/services/quote_parity_shadow.py` | Modify | Task 6 — orchestrator `run_quote_parity_probe(...)` (injected fns + clock). |
| `app/services/invest_quote_service.py` | Modify | Task 6 — add read-only public passthroughs `kis_only_kr_prices` / `kis_only_us_prices` (delegate to `_kis_fetch_kr`/`_kis_fetch_us`; raw KIS layer, no fallback). |
| `scripts/quote_parity_shadow_probe.py` | Create | Task 7 — thin operator harness: universe enumeration, dry-run-default `--confirm-live` gate, real clients wiring, JSON output, exit codes. |
| `tests/test_quote_parity_shadow.py` | Create | Tasks 1–6 tests — tick/percentile, coverage (dotted BRK.B + drops + allowlist), currency mis-key, divergence KR-ticks/US-bps, latency, go/no-go (incl. ROB-708 `not_evaluable`/`blocked`), orchestrator end-to-end with fakes + fake clock. |
| `tests/test_quote_parity_shadow_probe.py` | Create | Task 7 tests — arg parse, secret-reject on `--symbols-file`, dry-run performs no network, exit-code mapping (go/no_go/blocked/crash). |

> **NOT touched:**
> - `app/services/invest_price_fallback.py` — `PriceFallbackResolver`, `fetch_toss_batch_prices`, `_chunk`, `_TOSS_PRICE_BATCH` are read/observed only; the shadow re-implements the *same* echo-match for coverage but never edits the resolver.
> - `app/services/brokers/toss/client.py` / `dto.py` — `prices()`, `_symbols_param`, `parse_prices`, `TossPrice` are consumed as-is; no rate-limit-group change.
> - The `/invest` hot path (`invest_home_readers.py`, `invest_api.py`) and the resolver chain in `InvestQuoteService._resolve` (`:50-67`) — production price serving is byte-for-byte unchanged. Task 6 only **adds** two public read-only passthroughs beside the existing private fetchers; it does not alter `_resolve`, `_kis_fetch_kr`, or `_kis_fetch_us`.
> - Any order/mutation/watch path, any DB write, any alembic migration.

---

## Task 1 — KRX tick + pure percentile primitives (migration-0)

**Files:**
- Create `app/services/quote_parity_shadow.py` (module header + these two helpers).
- Create `tests/test_quote_parity_shadow.py` (first block).

**Interfaces:**
- `def krx_tick_size(price: Decimal) -> Decimal` — 2023-01 KRX cash-equity tick bands; first band whose threshold ≤ `price` applies (`<2,000→1 … ≥500,000→1,000`). Guards `price <= 0` → returns the smallest tick (`Decimal("1")`).
- `def _percentile(values: Sequence[float], pct: float) -> float | None` — deterministic **nearest-rank** percentile (`ceil(pct/100 * n)`-th of the sorted values); `None` for empty input; `pct` in `[0, 100]`.

Steps:

- [ ] **Write the failing tests — tick bands + nearest-rank percentile.** Create `tests/test_quote_parity_shadow.py`:
```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.quote_parity_shadow import _percentile, krx_tick_size

pytestmark = pytest.mark.unit


class TestKrxTickSize:
    @pytest.mark.parametrize(
        "price,tick",
        [
            ("500", "1"),      # <2,000
            ("1999", "1"),
            ("2000", "5"),     # boundary is inclusive-low
            ("4999", "5"),
            ("5000", "10"),
            ("19999", "10"),
            ("20000", "50"),
            ("49999", "50"),
            ("50000", "100"),
            ("199999", "100"),
            ("200000", "500"),
            ("499999", "500"),
            ("500000", "1000"),
            ("1250000", "1000"),
        ],
    )
    def test_bands(self, price, tick):
        assert krx_tick_size(Decimal(price)) == Decimal(tick)

    def test_nonpositive_price_returns_min_tick(self):
        assert krx_tick_size(Decimal("0")) == Decimal("1")


class TestPercentile:
    def test_empty_is_none(self):
        assert _percentile([], 99) is None

    def test_nearest_rank_p99(self):
        # 100 values 1..100; nearest-rank p99 -> ceil(0.99*100)=99th -> 99.
        assert _percentile([float(i) for i in range(1, 101)], 99) == 99.0

    def test_p50_and_p100(self):
        vals = [10.0, 20.0, 30.0, 40.0]
        assert _percentile(vals, 50) == 20.0   # ceil(0.5*4)=2nd
        assert _percentile(vals, 100) == 40.0
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v` → `ModuleNotFoundError: app.services.quote_parity_shadow`.

- [ ] **Minimal impl — create the module with the two helpers.** Create `app/services/quote_parity_shadow.py`:
```python
"""ROB-709 — read-only A/B parity shadow: Toss prices() batch vs the raw KIS
batch layer. Pure comparison/stats engine (no I/O) + an injected-fn orchestrator.

Decides whether /invest batch current-price reads can flip to Toss-first
(ROB-710). NO user-facing behavior change: this module never writes, never
mutates a broker/order/watch path, and never edits the production resolver — it
observes the Toss and KIS seams and emits go/no-go metrics.

PRECONDITION ROB-708: the KIS US layer must move to a live-last quote before US
divergence is a valid promotion signal; until then evaluate_go_no_go marks the
US-divergence bar not_evaluable and the decision blocked.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

# 2023-01 KRX cash-equity tick ladder (KOSPI/KOSDAQ). NOT the Upbit KRW ladder
# in app/services/paper_fills.py:14-27. First band whose threshold <= price wins.
_KRX_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("500000"), Decimal("1000")),
    (Decimal("200000"), Decimal("500")),
    (Decimal("50000"), Decimal("100")),
    (Decimal("20000"), Decimal("50")),
    (Decimal("5000"), Decimal("10")),
    (Decimal("2000"), Decimal("5")),
    (Decimal("0"), Decimal("1")),
)


def krx_tick_size(price: Decimal) -> Decimal:
    """KRX equity tick for ``price`` (KRW). Non-positive -> smallest tick."""
    if price <= 0:
        return Decimal("1")
    for threshold, unit in _KRX_TICK_BANDS:
        if price >= threshold:
            return unit
    return Decimal("1")  # pragma: no cover - last band threshold is 0


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Deterministic nearest-rank percentile; ``None`` for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((pct / 100.0) * len(ordered))
    idx = min(max(rank, 1), len(ordered)) - 1
    return ordered[idx]
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v` → all pass.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): quote_parity_shadow KRX tick + nearest-rank percentile primitives"`

---

## Task 2 — Toss coverage classifier (echo-match, dotted symbols, silent drops) (migration-0)

**Files:**
- Modify `app/services/quote_parity_shadow.py` — add `CoverageReport` + `classify_coverage`.
- Modify `tests/test_quote_parity_shadow.py` — coverage cases.

**Interfaces:**
- `@dataclass(frozen=True) class CoverageReport` — `requested_count: int`, `echoed_count: int`, `matched: list[str]` (requested symbols Toss echoed), `silent_drops: list[str]` (requested, not echoed, minus allowlist), `allowlisted_misses: list[str]`, `unexpected_echoes: list[str]` (echoed but not requested), `coverage_ratio: float` (`len(matched)/requested_count`, `1.0` when `requested_count == 0`).
- `def classify_coverage(requested: Sequence[str], echoed_symbols: Iterable[str], *, allowlist: frozenset[str] = frozenset()) -> CoverageReport` — echo-match uses the SAME rule as `fetch_toss_batch_prices` (`invest_price_fallback.py:105-112`): `by_upper = {s.upper(): s for s in requested}`; an echoed symbol matches iff `str(echoed).upper() in by_upper`. Allowlist is compared upper-cased.

Steps:

- [ ] **Write the failing tests — dotted BRK.B match, silent drop, allowlist, unexpected echo.** Append to `tests/test_quote_parity_shadow.py`:
```python
from app.services.quote_parity_shadow import CoverageReport, classify_coverage


class TestCoverage:
    def test_dotted_us_symbol_matches_when_echoed_verbatim(self):
        rep = classify_coverage(["AAPL", "BRK.B", "MSFT"], ["aapl", "BRK.B", "MSFT"])
        assert rep.matched == ["AAPL", "BRK.B", "MSFT"]  # case-insensitive echo
        assert rep.silent_drops == []
        assert rep.coverage_ratio == 1.0

    def test_dropped_dotted_symbol_is_a_silent_drop(self):
        # Toss silently omits BRK.B (or returns a de-dotted "BRK" that won't match).
        rep = classify_coverage(["AAPL", "BRK.B"], ["AAPL", "BRK"])
        assert rep.silent_drops == ["BRK.B"]
        assert "BRK" in rep.unexpected_echoes
        assert rep.coverage_ratio == 0.5

    def test_allowlisted_miss_is_not_a_silent_drop(self):
        rep = classify_coverage(
            ["AAPL", "DELISTED1"], ["AAPL"], allowlist=frozenset({"DELISTED1"})
        )
        assert rep.silent_drops == []
        assert rep.allowlisted_misses == ["DELISTED1"]
        assert rep.coverage_ratio == 0.5  # coverage still counts it missing

    def test_empty_request_is_full_coverage(self):
        rep = classify_coverage([], [])
        assert rep.coverage_ratio == 1.0
        assert isinstance(rep, CoverageReport)
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v -k Coverage` → `ImportError` (`classify_coverage` missing).

- [ ] **Minimal impl.** Add to `app/services/quote_parity_shadow.py` (extend imports: `from collections.abc import Iterable, Sequence`, `from dataclasses import dataclass`):
```python
@dataclass(frozen=True)
class CoverageReport:
    requested_count: int
    echoed_count: int
    matched: list[str]
    silent_drops: list[str]
    allowlisted_misses: list[str]
    unexpected_echoes: list[str]
    coverage_ratio: float


def classify_coverage(
    requested: Sequence[str],
    echoed_symbols: Iterable[str],
    *,
    allowlist: frozenset[str] = frozenset(),
) -> CoverageReport:
    # SAME echo-match as fetch_toss_batch_prices (invest_price_fallback.py:105-112).
    by_upper = {s.upper(): s for s in requested}
    allow_upper = {a.upper() for a in allowlist}
    echoed = [str(e) for e in echoed_symbols]
    echoed_upper = {e.upper() for e in echoed}

    matched = [by_upper[u] for u in by_upper if u in echoed_upper]
    missing = [by_upper[u] for u in by_upper if u not in echoed_upper]
    allowlisted_misses = [s for s in missing if s.upper() in allow_upper]
    silent_drops = [s for s in missing if s.upper() not in allow_upper]
    unexpected_echoes = [e for e in echoed if e.upper() not in by_upper]

    req_n = len(by_upper)
    return CoverageReport(
        requested_count=req_n,
        echoed_count=len(echoed),
        matched=matched,
        silent_drops=silent_drops,
        allowlisted_misses=allowlisted_misses,
        unexpected_echoes=unexpected_echoes,
        coverage_ratio=(len(matched) / req_n) if req_n else 1.0,
    )
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v -k Coverage` → all pass.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): Toss coverage classifier (production echo-match, dotted-symbol + silent-drop detection)"`

---

## Task 3 — Currency correctness check (KR→KRW / US→USD mis-key detection) (migration-0)

**Files:**
- Modify `app/services/quote_parity_shadow.py` — add `CurrencyReport` + `check_currency`.
- Modify `tests/test_quote_parity_shadow.py` — currency cases.

**Interfaces:**
- `@dataclass(frozen=True) class CurrencyReport` — `checked_count: int`, `miskeys: list[dict[str, str]]` (each `{"symbol", "market", "expected", "got"}`), `miskey_count: int`.
- `def check_currency(rows: Sequence[tuple[str, str, str]]) -> CurrencyReport` — `rows` are `(symbol, market, toss_currency)`; `market` normalized upper; expected `{"KR": "KRW", "US": "USD"}`; a row is a mis-key iff its market is known and `toss_currency.upper() != expected`. This is the ONLY guard: the resolver discards `TossPrice.currency` (`fetch_toss_batch_prices` returns only `last_price`), so a mis-key here would silently mis-value a position.

Steps:

- [ ] **Write the failing tests — clean vs KR-tagged-USD mis-key.** Append:
```python
from app.services.quote_parity_shadow import CurrencyReport, check_currency


class TestCurrency:
    def test_all_correct_zero_miskeys(self):
        rep = check_currency(
            [("005930", "KR", "KRW"), ("AAPL", "US", "USD"), ("BRK.B", "US", "usd")]
        )
        assert rep.miskey_count == 0
        assert rep.checked_count == 3

    def test_kr_tagged_usd_is_a_miskey(self):
        rep = check_currency([("005930", "KR", "USD"), ("AAPL", "US", "USD")])
        assert rep.miskey_count == 1
        assert rep.miskeys[0] == {
            "symbol": "005930",
            "market": "KR",
            "expected": "KRW",
            "got": "USD",
        }

    def test_unknown_market_is_not_checked(self):
        rep = check_currency([("BTC", "CRYPTO", "KRW")])
        assert rep.miskey_count == 0
        assert rep.checked_count == 0  # unknown markets are skipped, not failed
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v -k Currency` → `ImportError`.

- [ ] **Minimal impl.** Add:
```python
_EXPECTED_CURRENCY = {"KR": "KRW", "US": "USD"}


@dataclass(frozen=True)
class CurrencyReport:
    checked_count: int
    miskeys: list[dict[str, str]]
    miskey_count: int


def check_currency(rows: Sequence[tuple[str, str, str]]) -> CurrencyReport:
    miskeys: list[dict[str, str]] = []
    checked = 0
    for symbol, market, currency in rows:
        expected = _EXPECTED_CURRENCY.get(str(market).upper())
        if expected is None:
            continue  # unknown market: not our jurisdiction, not a failure
        checked += 1
        if str(currency).upper() != expected:
            miskeys.append(
                {
                    "symbol": str(symbol),
                    "market": str(market).upper(),
                    "expected": expected,
                    "got": str(currency).upper(),
                }
            )
    return CurrencyReport(
        checked_count=checked, miskeys=miskeys, miskey_count=len(miskeys)
    )
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v -k Currency`.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): currency-correctness check (KR->KRW / US->USD mis-key detection)"`

---

## Task 4 — Divergence + latency aggregation (KR ticks / US bps · p50/p95/p99) (migration-0)

**Files:**
- Modify `app/services/quote_parity_shadow.py` — add `DivergenceStats` + `summarize_divergence`, `LatencyStats` + `summarize_latency`.
- Modify `tests/test_quote_parity_shadow.py` — divergence + latency cases.

**Interfaces:**
- `@dataclass(frozen=True) class DivergenceStats` — `market: str`, `count: int`, `median_bps: float | None`, `p99_bps: float | None`, `median_ticks: float | None`, `p99_ticks: float | None` (KR only; `None` for US), `worst: list[dict]` (top-N by bps: `{"symbol", "toss", "kis", "bps", "ticks"}`).
- `def summarize_divergence(pairs: Sequence[tuple[str, Decimal, Decimal]], *, market: str, top_n: int = 20) -> DivergenceStats` — `pairs` are `(symbol, toss_price, kis_price)` where BOTH are present (mismatched coverage belongs to Task 2, not here). Per pair: `bps = abs(toss - kis) / kis * 10000` (skip when `kis <= 0`); for KR, `ticks = abs(toss - kis) / krx_tick_size(kis)`. `median_*` via `statistics.median`, `p99_*` via `_percentile(..., 99)`.
- `@dataclass(frozen=True) class LatencyStats` — `label: str`, `call_count: int`, `error_count: int`, `error_rate: float`, `p50_ms/p95_ms/p99_ms: float | None`, `total_wall_ms: float`.
- `def summarize_latency(label: str, samples_ms: Sequence[float], *, error_count: int, total_wall_ms: float) -> LatencyStats` — percentiles via `_percentile`; `error_rate = error_count / call_count` (`0.0` when `call_count == 0`); `call_count = len(samples_ms) + error_count`.

Steps:

- [ ] **Write the failing tests — bps, KR tick normalization, latency percentiles.** Append:
```python
from app.services.quote_parity_shadow import (
    LatencyStats,
    summarize_divergence,
    summarize_latency,
)


class TestDivergence:
    def test_us_bps_no_ticks(self):
        # 100.00 vs 100.10 -> 10 bps.
        stats = summarize_divergence(
            [("AAPL", Decimal("100.10"), Decimal("100.00"))], market="US"
        )
        assert stats.market == "US"
        assert stats.count == 1
        assert round(stats.p99_bps, 4) == 10.0
        assert stats.p99_ticks is None  # US never tick-normalized
        assert stats.worst[0]["symbol"] == "AAPL"

    def test_kr_tick_normalization(self):
        # price 30,000 -> tick 50; toss 30,050 vs kis 30,000 -> 1 tick, ~16.67 bps.
        stats = summarize_divergence(
            [("005930", Decimal("30050"), Decimal("30000"))], market="KR"
        )
        assert stats.p99_ticks == 1.0
        assert round(stats.p99_bps, 2) == 16.67

    def test_skips_nonpositive_kis(self):
        stats = summarize_divergence(
            [("BAD", Decimal("10"), Decimal("0"))], market="US"
        )
        assert stats.count == 0
        assert stats.p99_bps is None


class TestLatency:
    def test_percentiles_and_error_rate(self):
        stats = summarize_latency(
            "toss", [10.0, 20.0, 30.0, 40.0], error_count=1, total_wall_ms=105.0
        )
        assert stats.call_count == 5          # 4 samples + 1 error
        assert stats.error_rate == 0.2
        assert stats.p50_ms == 20.0
        assert stats.total_wall_ms == 105.0

    def test_empty_samples(self):
        stats = summarize_latency("kis", [], error_count=0, total_wall_ms=0.0)
        assert stats.call_count == 0
        assert stats.error_rate == 0.0
        assert stats.p95_ms is None
        assert isinstance(stats, LatencyStats)
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v -k "Divergence or Latency"` → `ImportError`.

- [ ] **Minimal impl.** Add (extend imports: `import statistics`). `krx_tick_size` and `_percentile` are already defined in this module (Task 1) — reference them directly, do **not** re-import:
```python
@dataclass(frozen=True)
class DivergenceStats:
    market: str
    count: int
    median_bps: float | None
    p99_bps: float | None
    median_ticks: float | None
    p99_ticks: float | None
    worst: list[dict]


def summarize_divergence(
    pairs: Sequence[tuple[str, Decimal, Decimal]],
    *,
    market: str,
    top_n: int = 20,
) -> DivergenceStats:
    is_kr = str(market).upper() == "KR"
    rows: list[dict] = []
    for symbol, toss, kis in pairs:
        if kis <= 0:
            continue
        bps = abs(float(toss) - float(kis)) / float(kis) * 10000.0
        ticks = (
            abs(float(toss) - float(kis)) / float(krx_tick_size(kis)) if is_kr else None
        )
        rows.append({"symbol": symbol, "toss": float(toss), "kis": float(kis),
                     "bps": bps, "ticks": ticks})
    bps_vals = [r["bps"] for r in rows]
    tick_vals = [r["ticks"] for r in rows if r["ticks"] is not None]
    rows.sort(key=lambda r: r["bps"], reverse=True)
    return DivergenceStats(
        market=str(market).upper(),
        count=len(rows),
        median_bps=statistics.median(bps_vals) if bps_vals else None,
        p99_bps=_percentile(bps_vals, 99),
        median_ticks=statistics.median(tick_vals) if tick_vals else None,
        p99_ticks=_percentile(tick_vals, 99),
        worst=rows[:top_n],
    )


# (No self-import: krx_tick_size / _percentile are defined above in this module.)


@dataclass(frozen=True)
class LatencyStats:
    label: str
    call_count: int
    error_count: int
    error_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    total_wall_ms: float


def summarize_latency(
    label: str,
    samples_ms: Sequence[float],
    *,
    error_count: int,
    total_wall_ms: float,
) -> LatencyStats:
    call_count = len(samples_ms) + error_count
    return LatencyStats(
        label=label,
        call_count=call_count,
        error_count=error_count,
        error_rate=(error_count / call_count) if call_count else 0.0,
        p50_ms=_percentile(samples_ms, 50),
        p95_ms=_percentile(samples_ms, 95),
        p99_ms=_percentile(samples_ms, 99),
        total_wall_ms=total_wall_ms,
    )
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v -k "Divergence or Latency"`.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): divergence (KR ticks / US bps) + latency (p50/p95/p99, error-rate) aggregation"`

---

## Task 5 — `GoBars` + `evaluate_go_no_go` (ROB-708 precondition-aware) (migration-0)

**Files:**
- Modify `app/services/quote_parity_shadow.py` — add `GoBars`, `BarResult`, `GoNoGoDecision`, `evaluate_go_no_go`.
- Modify `tests/test_quote_parity_shadow.py` — go/no-go cases incl. the ROB-708 `not_evaluable`/`blocked` gate.

**Interfaces:**
- `@dataclass(frozen=True) class GoBars` — `coverage_min: float = 0.995`, `max_silent_drops: int = 0`, `kr_p99_max_ticks: float = 1.0`, `us_p99_max_bps: float = 10.0`, `max_currency_miskeys: int = 0`. Latency bars are ratio-based: `require_toss_wall_le_kis: bool = True`, `require_toss_error_rate_le_kis: bool = True`.
- `@dataclass(frozen=True) class BarResult` — `name: str`, `status: str` (`"pass" | "fail" | "not_evaluable"`), `detail: str`.
- `@dataclass(frozen=True) class GoNoGoDecision` — `decision: str` (`"go" | "no_go" | "blocked"`), `bars: list[BarResult]`.
- `def evaluate_go_no_go(*, coverage: CoverageReport, kr_div: DivergenceStats, us_div: DivergenceStats, currency: CurrencyReport, toss_latency: LatencyStats, kis_latency: LatencyStats, us_kis_live_last: bool, bars: GoBars = GoBars()) -> GoNoGoDecision`.
  - Bars: `coverage_ratio ≥ coverage_min`; `len(coverage.silent_drops) ≤ max_silent_drops`; `kr_div.p99_ticks ≤ kr_p99_max_ticks` (pass when `None`/no KR sample — nothing to fail); **US-divergence bar**: `not_evaluable` when `us_kis_live_last is False` (detail: "blocked on ROB-708 — KIS US is daily-close, not live-last"), else `us_div.p99_bps ≤ us_p99_max_bps`; `currency.miskey_count ≤ max_currency_miskeys`; latency: `toss_latency.total_wall_ms ≤ kis_latency.total_wall_ms` and `toss_latency.error_rate ≤ kis_latency.error_rate`.
  - Decision: `"blocked"` if any bar is `not_evaluable`; else `"no_go"` if any bar is `"fail"`; else `"go"`.

Steps:

- [ ] **Write the failing tests — all-pass go, a fail no_go, ROB-708 blocked.** Append:
```python
from app.services.quote_parity_shadow import (  # DivergenceStats used by _div helper
    DivergenceStats,
    GoBars,
    GoNoGoDecision,
    evaluate_go_no_go,
)


def _cov(ratio=1.0, drops=None):
    return classify_coverage(["AAPL"], ["AAPL"]) if ratio == 1.0 else CoverageReport(
        requested_count=2, echoed_count=1, matched=["AAPL"],
        silent_drops=list(drops or []), allowlisted_misses=[],
        unexpected_echoes=[], coverage_ratio=ratio,
    )


def _div(market, p99_bps=1.0, p99_ticks=None):
    return DivergenceStats(market=market, count=1, median_bps=p99_bps, p99_bps=p99_bps,
                           median_ticks=p99_ticks, p99_ticks=p99_ticks, worst=[])


def _lat(label, wall, err_rate=0.0):
    # call_count derived; craft via summarize_latency for realism.
    n = 10
    return summarize_latency(label, [wall / n] * n, error_count=0, total_wall_ms=wall)


class TestGoNoGo:
    def test_all_pass_when_precondition_met(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_bps=5.0, p99_ticks=1.0),
            us_div=_div("US", p99_bps=8.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert isinstance(d, GoNoGoDecision)
        assert d.decision == "go"
        assert {b.status for b in d.bars} == {"pass"}

    def test_blocked_when_us_precondition_unmet(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=999.0),  # huge, but not evaluated
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=False,   # ROB-708 not landed
        )
        assert d.decision == "blocked"
        us_bar = next(b for b in d.bars if b.name == "us_divergence")
        assert us_bar.status == "not_evaluable"
        assert "ROB-708" in us_bar.detail

    def test_no_go_on_silent_drop(self):
        d = evaluate_go_no_go(
            coverage=_cov(0.9, drops=["BRK.B"]),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "silent_drops" and b.status == "fail" for b in d.bars)
        assert any(b.name == "coverage" and b.status == "fail" for b in d.bars)

    def test_no_go_on_currency_miskey(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("005930", "KR", "USD")]),  # mis-key
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "currency" and b.status == "fail" for b in d.bars)

    def test_no_go_when_toss_slower_than_kis(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 500.0),   # slower than kis
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "latency_wall" and b.status == "fail" for b in d.bars)
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v -k GoNoGo` → `ImportError`.

- [ ] **Minimal impl.** Add:
```python
@dataclass(frozen=True)
class GoBars:
    coverage_min: float = 0.995
    max_silent_drops: int = 0
    kr_p99_max_ticks: float = 1.0
    us_p99_max_bps: float = 10.0
    max_currency_miskeys: int = 0
    require_toss_wall_le_kis: bool = True
    require_toss_error_rate_le_kis: bool = True


@dataclass(frozen=True)
class BarResult:
    name: str
    status: str  # "pass" | "fail" | "not_evaluable"
    detail: str


@dataclass(frozen=True)
class GoNoGoDecision:
    decision: str  # "go" | "no_go" | "blocked"
    bars: list[BarResult]


def _bar(name: str, ok: bool, detail: str) -> BarResult:
    return BarResult(name=name, status="pass" if ok else "fail", detail=detail)


def evaluate_go_no_go(
    *,
    coverage: CoverageReport,
    kr_div: DivergenceStats,
    us_div: DivergenceStats,
    currency: CurrencyReport,
    toss_latency: LatencyStats,
    kis_latency: LatencyStats,
    us_kis_live_last: bool,
    bars: GoBars = GoBars(),
) -> GoNoGoDecision:
    results: list[BarResult] = []

    results.append(
        _bar(
            "coverage",
            coverage.coverage_ratio >= bars.coverage_min,
            f"coverage_ratio={coverage.coverage_ratio:.4f} min={bars.coverage_min}",
        )
    )
    results.append(
        _bar(
            "silent_drops",
            len(coverage.silent_drops) <= bars.max_silent_drops,
            f"silent_drops={len(coverage.silent_drops)} max={bars.max_silent_drops}",
        )
    )
    kr_ok = kr_div.p99_ticks is None or kr_div.p99_ticks <= bars.kr_p99_max_ticks
    results.append(
        _bar(
            "kr_divergence",
            kr_ok,
            f"kr_p99_ticks={kr_div.p99_ticks} max={bars.kr_p99_max_ticks}",
        )
    )

    # ROB-708 precondition: US divergence is a daily-close-vs-live artifact until
    # _kis_fetch_us moves to a live-last quote. Do NOT pass/fail it — mark it
    # not_evaluable so the operator cannot mistake a blocked run for a go.
    if not us_kis_live_last:
        results.append(
            BarResult(
                name="us_divergence",
                status="not_evaluable",
                detail=(
                    "blocked on ROB-708 — KIS US layer is daily-close (period=D), "
                    "not live-last; US divergence is not a valid promotion signal"
                ),
            )
        )
    else:
        us_ok = us_div.p99_bps is None or us_div.p99_bps <= bars.us_p99_max_bps
        results.append(
            _bar(
                "us_divergence",
                us_ok,
                f"us_p99_bps={us_div.p99_bps} max={bars.us_p99_max_bps}",
            )
        )

    results.append(
        _bar(
            "currency",
            currency.miskey_count <= bars.max_currency_miskeys,
            f"miskeys={currency.miskey_count} max={bars.max_currency_miskeys}",
        )
    )
    if bars.require_toss_wall_le_kis:
        results.append(
            _bar(
                "latency_wall",
                toss_latency.total_wall_ms <= kis_latency.total_wall_ms,
                f"toss_wall_ms={toss_latency.total_wall_ms} "
                f"kis_wall_ms={kis_latency.total_wall_ms}",
            )
        )
    if bars.require_toss_error_rate_le_kis:
        results.append(
            _bar(
                "error_rate",
                toss_latency.error_rate <= kis_latency.error_rate,
                f"toss_err={toss_latency.error_rate} kis_err={kis_latency.error_rate}",
            )
        )

    if any(b.status == "not_evaluable" for b in results):
        decision = "blocked"
    elif any(b.status == "fail" for b in results):
        decision = "no_go"
    else:
        decision = "go"
    return GoNoGoDecision(decision=decision, bars=results)
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v -k GoNoGo`.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): evaluate_go_no_go with ROB-708 US-precondition gate (blocked/not_evaluable)"`

---

## Task 6 — Orchestrator `run_quote_parity_probe` + raw-KIS public passthroughs (migration-0)

**Files:**
- Modify `app/services/quote_parity_shadow.py` — add `run_quote_parity_probe` + the type aliases (`PricesFn`, `KisFetchFn`) and `import asyncio`, `import time`, `from typing import Any`, `from collections.abc import Awaitable, Callable`, `from app.services.brokers.toss.dto import TossPrice`.
- Modify `app/services/invest_quote_service.py` — add read-only public passthroughs `kis_only_kr_prices` / `kis_only_us_prices` right after `_kis_fetch_us` (`:130`), each delegating to the existing private fetcher (raw KIS batch layer, **no** fallback).
- Modify `tests/test_quote_parity_shadow.py` — end-to-end orchestrator test with fake Toss/KIS fns + fake clock.

**Interfaces:**
- `PricesFn = Callable[[list[str]], Awaitable[list[TossPrice]]]` (one Toss batch), `KisFetchFn = Callable[[list[str]], Awaitable[dict[str, float | None]]]` (raw KIS layer, internally fanned out).
- `async def run_quote_parity_probe(*, kr_symbols: list[str], us_symbols: list[str], toss_prices_fn: PricesFn, kis_kr_fetch: KisFetchFn, kis_us_fetch: KisFetchFn, allowlist: frozenset[str] = frozenset(), us_kis_live_last: bool = False, clock: Callable[[], float] = time.monotonic, bars: GoBars = GoBars(), batch_size: int = 200) -> dict[str, Any]` — chunks each market at ≤`batch_size`, calls `toss_prices_fn` per chunk (timing each with `clock`, `return_exceptions`-style try/except per batch → counts an error, continues), collects `TossPrice`; calls `kis_kr_fetch(kr_symbols)` / `kis_us_fetch(us_symbols)` (each one timed wall sample); builds coverage (per market + combined), currency rows `(symbol, market, toss.currency)`, divergence pairs (symbol → `toss.last_price` vs KIS price where BOTH present), off-hours capture (`{symbol: {last_price, timestamp}}` for US), latency stats; runs `evaluate_go_no_go`; returns a JSON-serializable dict. **Deterministic**: no wall-clock, no network — everything injected.
- `InvestQuoteService.kis_only_kr_prices(self, symbols: list[str]) -> dict[str, float | None]` → `return await self._kis_fetch_kr(symbols)`; `kis_only_us_prices` → `_kis_fetch_us`. Pure delegation; no behavior change to `_resolve`.

Steps:

- [ ] **Write the failing test — orchestrator end-to-end with fakes + fake clock (blocked path, then go path).** Append to `tests/test_quote_parity_shadow.py`:
```python
import pytest

from app.services.brokers.toss.dto import TossPrice
from app.services.quote_parity_shadow import run_quote_parity_probe


class _FakeClock:
    """Monotonic-ish clock: each call advances by a fixed step for deterministic ms."""

    def __init__(self, start=1000.0, step=0.01):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


@pytest.mark.asyncio
async def test_orchestrator_blocked_until_rob708(monkeypatch):
    async def toss_prices(batch):
        return [
            TossPrice(symbol=s, timestamp="2026-07-05T12:00:00Z",
                      last_price=Decimal("100.10") if s == "AAPL" else Decimal("30050"),
                      currency="USD" if s == "AAPL" else "KRW")
            for s in batch
        ]

    async def kis_kr(symbols):
        return {"005930": 30000.0}

    async def kis_us(symbols):
        return {"AAPL": 100.00}

    report = await run_quote_parity_probe(
        kr_symbols=["005930"],
        us_symbols=["AAPL"],
        toss_prices_fn=toss_prices,
        kis_kr_fetch=kis_kr,
        kis_us_fetch=kis_us,
        clock=_FakeClock(),
        us_kis_live_last=False,   # ROB-708 not landed
    )
    assert report["go_no_go"]["decision"] == "blocked"
    assert report["coverage"]["combined"]["silent_drops"] == []
    assert report["currency"]["miskey_count"] == 0
    # US off-hours capture is recorded verbatim.
    assert report["off_hours"]["us"]["AAPL"]["timestamp"] == "2026-07-05T12:00:00Z"


@pytest.mark.asyncio
async def test_orchestrator_go_when_precondition_met_and_bars_pass():
    async def toss_prices(batch):
        return [TossPrice(symbol=s, timestamp="t", last_price=Decimal("100.05"),
                          currency="USD") for s in batch]

    async def kis_kr(symbols):
        return {}

    async def kis_us(symbols):
        return {"AAPL": 100.00}   # 5 bps < 10

    report = await run_quote_parity_probe(
        kr_symbols=[],
        us_symbols=["AAPL"],
        toss_prices_fn=toss_prices,
        kis_kr_fetch=kis_kr,
        kis_us_fetch=kis_us,
        # Constant clock (step=0) => every measured duration is 0ms, so the
        # latency wall-bar is a deterministic 0 <= 0 pass. A stepping clock would
        # make Toss look slower purely because the probe calls clock() more times
        # on the Toss side than the KIS side — an artifact of the fake, not real
        # latency. Real timing comes from the monotonic clock in the live script.
        clock=_FakeClock(step=0.0),
        us_kis_live_last=True,
    )
    assert report["go_no_go"]["decision"] == "go"


@pytest.mark.asyncio
async def test_orchestrator_counts_toss_batch_error_fail_open():
    calls = {"n": 0}

    async def toss_prices(batch):
        calls["n"] += 1
        raise RuntimeError("toss 500")

    async def kis_us(symbols):
        return {"AAPL": 100.0}

    report = await run_quote_parity_probe(
        kr_symbols=[], us_symbols=["AAPL"],
        toss_prices_fn=toss_prices, kis_kr_fetch=lambda s: _empty(),
        kis_us_fetch=kis_us, clock=_FakeClock(), us_kis_live_last=True,
    )
    assert report["latency"]["toss"]["error_count"] == 1
    # A failed Toss batch => everything is a silent drop => no_go, never a crash.
    assert report["go_no_go"]["decision"] == "no_go"


async def _empty():
    return {}
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow.py -v -k orchestrator` → `ImportError` (`run_quote_parity_probe` missing).

- [ ] **Minimal impl — orchestrator.** Add to `app/services/quote_parity_shadow.py`:
```python
def _chunk(symbols: list[str], size: int) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


async def _fetch_toss_side(
    symbols: list[str], toss_prices_fn: PricesFn, clock, batch_size: int
) -> tuple[list[TossPrice], list[float], int, float]:
    """Return (prices, per_batch_ms, error_count, wall_ms). Fail-open per batch."""
    prices: list[TossPrice] = []
    samples: list[float] = []
    errors = 0
    wall_start = clock()
    for batch in _chunk(symbols, batch_size):
        if not batch:
            continue
        # Uppercase the OUTBOUND request to mirror production exactly
        # (fetch_toss_batch_prices sends _chunk([s.upper() ...]),
        # invest_price_fallback.py:108). A --symbols-file may carry lowercase
        # symbols and to_db_symbol does not uppercase — send them in the same
        # case production would, or coverage would not reflect production drops.
        batch = [s.upper() for s in batch]
        t0 = clock()
        try:
            batch_prices = await toss_prices_fn(batch)
        except Exception:  # noqa: BLE001 — probe fails open; a failed batch is a drop
            errors += 1
            continue
        # Only SUCCESSFUL calls contribute a latency sample; failures are counted
        # via `errors` alone, so call_count = len(samples) + errors is exact (a
        # failed batch must not inflate both buckets).
        prices.extend(batch_prices)
        samples.append((clock() - t0) * 1000.0)
    wall_ms = (clock() - wall_start) * 1000.0
    return prices, samples, errors, wall_ms


async def run_quote_parity_probe(
    *,
    kr_symbols: list[str],
    us_symbols: list[str],
    toss_prices_fn: PricesFn,
    kis_kr_fetch: KisFetchFn,
    kis_us_fetch: KisFetchFn,
    allowlist: frozenset[str] = frozenset(),
    us_kis_live_last: bool = False,
    clock: Callable[[], float] = time.monotonic,
    bars: GoBars = GoBars(),
    batch_size: int = 200,
) -> dict[str, Any]:
    # --- Toss side (one batch call per <=200 chunk) ---
    kr_prices, kr_samp, kr_err, kr_wall = await _fetch_toss_side(
        kr_symbols, toss_prices_fn, clock, batch_size
    )
    us_prices, us_samp, us_err, us_wall = await _fetch_toss_side(
        us_symbols, toss_prices_fn, clock, batch_size
    )
    toss_latency = summarize_latency(
        "toss", kr_samp + us_samp, error_count=kr_err + us_err,
        total_wall_ms=kr_wall + us_wall,
    )

    # --- KIS side (raw layer, no fallback) ---
    kis_wall_start = clock()
    kis_kr = await kis_kr_fetch(kr_symbols) if kr_symbols else {}
    kis_us = await kis_us_fetch(us_symbols) if us_symbols else {}
    kis_wall = (clock() - kis_wall_start) * 1000.0
    kis_none = sum(1 for v in {**kis_kr, **kis_us}.values() if v is None)
    kis_calls = len(kis_kr) + len(kis_us)
    kis_latency = LatencyStats(
        label="kis", call_count=kis_calls, error_count=kis_none,
        error_rate=(kis_none / kis_calls) if kis_calls else 0.0,
        p50_ms=None, p95_ms=None, p99_ms=None, total_wall_ms=kis_wall,
    )

    # --- Coverage / currency / off-hours ---
    kr_cov = classify_coverage(kr_symbols, [p.symbol for p in kr_prices], allowlist=allowlist)
    us_cov = classify_coverage(us_symbols, [p.symbol for p in us_prices], allowlist=allowlist)
    combined_cov = classify_coverage(
        kr_symbols + us_symbols,
        [p.symbol for p in kr_prices + us_prices],
        allowlist=allowlist,
    )
    currency = check_currency(
        [(p.symbol, "KR", p.currency) for p in kr_prices]
        + [(p.symbol, "US", p.currency) for p in us_prices]
    )

    # Echo-match Toss last_price back to the requested key (SAME rule as prod).
    def _by_requested(reqs: list[str], prices: list[TossPrice]) -> dict[str, TossPrice]:
        by_upper = {s.upper(): s for s in reqs}
        out: dict[str, TossPrice] = {}
        for p in prices:
            req = by_upper.get(str(p.symbol).upper())
            if req is not None:
                out[req] = p
        return out

    kr_toss = _by_requested(kr_symbols, kr_prices)
    us_toss = _by_requested(us_symbols, us_prices)

    kr_pairs = [
        (sym, kr_toss[sym].last_price, Decimal(str(kis_kr[sym])))
        for sym in kr_toss
        if kis_kr.get(sym) is not None
    ]
    us_pairs = [
        (sym, us_toss[sym].last_price, Decimal(str(kis_us[sym])))
        for sym in us_toss
        if kis_us.get(sym) is not None
    ]
    kr_div = summarize_divergence(kr_pairs, market="KR")
    us_div = summarize_divergence(us_pairs, market="US")

    off_hours_us = {
        sym: {"last_price": str(p.last_price), "timestamp": p.timestamp}
        for sym, p in us_toss.items()
    }

    decision = evaluate_go_no_go(
        coverage=combined_cov, kr_div=kr_div, us_div=us_div, currency=currency,
        toss_latency=toss_latency, kis_latency=kis_latency,
        us_kis_live_last=us_kis_live_last, bars=bars,
    )

    def _cov_dict(c: CoverageReport) -> dict[str, Any]:
        return {
            "requested_count": c.requested_count, "echoed_count": c.echoed_count,
            "coverage_ratio": c.coverage_ratio, "silent_drops": c.silent_drops,
            "allowlisted_misses": c.allowlisted_misses,
            "unexpected_echoes": c.unexpected_echoes,
        }

    def _div_dict(d: DivergenceStats) -> dict[str, Any]:
        return {
            "market": d.market, "count": d.count, "median_bps": d.median_bps,
            "p99_bps": d.p99_bps, "median_ticks": d.median_ticks,
            "p99_ticks": d.p99_ticks, "worst": d.worst,
        }

    def _lat_dict(x: LatencyStats) -> dict[str, Any]:
        return {
            "label": x.label, "call_count": x.call_count, "error_count": x.error_count,
            "error_rate": x.error_rate, "p50_ms": x.p50_ms, "p95_ms": x.p95_ms,
            "p99_ms": x.p99_ms, "total_wall_ms": x.total_wall_ms,
        }

    return {
        "universe": {"kr_count": len(kr_symbols), "us_count": len(us_symbols)},
        "precondition": {
            "us_kis_live_last": us_kis_live_last,
            "note": "ROB-708 must land before US divergence is a valid go-signal",
        },
        "coverage": {"kr": _cov_dict(kr_cov), "us": _cov_dict(us_cov),
                     "combined": _cov_dict(combined_cov)},
        "currency": {"checked_count": currency.checked_count,
                     "miskey_count": currency.miskey_count, "miskeys": currency.miskeys},
        "divergence": {"kr": _div_dict(kr_div), "us": _div_dict(us_div)},
        "latency": {"toss": _lat_dict(toss_latency), "kis": _lat_dict(kis_latency)},
        "off_hours": {"us": off_hours_us},
        "go_no_go": {
            "decision": decision.decision,
            "bars": [{"name": b.name, "status": b.status, "detail": b.detail}
                     for b in decision.bars],
        },
    }
```
And add the type aliases near the top (after imports): `PricesFn = Callable[[list[str]], Awaitable[list["TossPrice"]]]` and `KisFetchFn = Callable[[list[str]], Awaitable[dict[str, float | None]]]`.

- [ ] **Minimal impl — public passthroughs on `InvestQuoteService`.** In `app/services/invest_quote_service.py`, immediately after `_kis_fetch_us` ends (`:130`), add:
```python
    async def kis_only_kr_prices(
        self, symbols: list[str]
    ) -> dict[str, float | None]:
        """ROB-709 shadow: RAW KIS KR batch layer (no fallback chain). Read-only."""
        return await self._kis_fetch_kr(symbols)

    async def kis_only_us_prices(
        self, symbols: list[str]
    ) -> dict[str, float | None]:
        """ROB-709 shadow: RAW KIS US batch layer (no fallback chain). Read-only.

        NOTE (ROB-708): until _kis_fetch_us moves to a live-last quote, this
        returns a daily close — the A/B shadow must gate US divergence on the
        ROB-708 precondition (us_kis_live_last).
        """
        return await self._kis_fetch_us(symbols)
```

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow.py -v -k orchestrator` → all pass. Regression: `uv run pytest tests/test_invest_quote_service.py -v` → unchanged (passthroughs are additive; `_resolve` untouched).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): run_quote_parity_probe orchestrator + raw-KIS read-only passthroughs on InvestQuoteService"`

---

## Task 7 — Operator probe script (dry-run-default, universe enumeration, exit codes) (migration-0)

**Files:**
- Create `scripts/quote_parity_shadow_probe.py`.
- Create `tests/test_quote_parity_shadow_probe.py`.

**Interfaces:**
- `def parse_args(argv: list[str] | None = None) -> argparse.Namespace` — `--symbols-file: Path|None`, `--user-id: int|None`, `--limit: int = 200`, `--allowlist: str = ""` (comma symbols), `--us-kis-live-last: store_true` (ROB-708 landed), `--confirm-live: store_true` (arm network; default dry-run), `--json: store_true`.
- `def load_symbols_file(path: Path) -> tuple[list[str], list[str]]` — secret-rejected (reuse the `_reject_if_sensitive` pattern from `diagnose_invest_screener_toss_parity.py:57-68`); CSV/JSON with `market`(KR/US) + `symbol`; returns `(kr, us)` de-duped, `to_db_symbol`-normalized (`app/core/symbol.py:26`).
- `async def enumerate_db_universe(session, *, user_id: int, limit: int) -> tuple[list[str], list[str]]` — `ManualHoldingsService(session).get_holdings_by_user(user_id, broker_type="toss")` split by `MarketType.KR/US`, capped at `limit` each.
- `def exit_code_for(decision: str) -> int` — `"go"→0`, `"no_go"→2`, `"blocked"→2`, else `1`.
- `async def main(argv=None) -> int` — dry-run (no `--confirm-live`): enumerate, print `{"mode":"dry_run","universe":{...},"planned_toss_batches":N}`, return `0`, **no network**. Live: `setup_logging_and_sentry(...)`, build `TossReadClient.from_settings()` + `SafeKISClient()` + `InvestQuoteService`, wire `toss_prices_fn=toss.prices`, `kis_kr_fetch=quote.kis_only_kr_prices`, `kis_us_fetch=quote.kis_only_us_prices`, `await run_quote_parity_probe(...)`, print JSON, `await toss.aclose()` in `finally`, return `exit_code_for(report["go_no_go"]["decision"])`; wrap in try/except → log + return `1`.

Steps:

- [ ] **Write the failing tests — arg parse, dry-run no-network, exit-code map, secret-reject.** Create `tests/test_quote_parity_shadow_probe.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.quote_parity_shadow_probe import (
    exit_code_for,
    load_symbols_file,
    main,
    parse_args,
)

pytestmark = pytest.mark.unit


class TestArgs:
    def test_defaults_are_dry_run(self):
        ns = parse_args(["--user-id", "1"])
        assert ns.confirm_live is False
        assert ns.us_kis_live_last is False
        assert ns.limit == 200

    def test_exit_code_map(self):
        assert exit_code_for("go") == 0
        assert exit_code_for("no_go") == 2
        assert exit_code_for("blocked") == 2
        assert exit_code_for("???") == 1


class TestSymbolsFile:
    def test_split_kr_us_and_normalize_dotted(self, tmp_path):
        p = tmp_path / "u.json"
        p.write_text(json.dumps([
            {"market": "US", "symbol": "BRK-B"},
            {"market": "KR", "symbol": "005930"},
        ]), encoding="utf-8")
        kr, us = load_symbols_file(p)
        assert kr == ["005930"]
        assert us == ["BRK.B"]  # to_db_symbol normalization

    def test_rejects_secrets(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("symbol,authorization\nAAPL,Bearer abc123\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_symbols_file(p)


@pytest.mark.asyncio
async def test_dry_run_performs_no_network(tmp_path, capsys, monkeypatch):
    # A symbols-file dry-run must not construct any broker client.
    import scripts.quote_parity_shadow_probe as probe

    def _boom(*a, **k):
        raise AssertionError("dry-run must not build a live client")

    monkeypatch.setattr(probe, "_build_live_clients", _boom, raising=False)
    p = tmp_path / "u.json"
    p.write_text(json.dumps([{"market": "US", "symbol": "AAPL"}]), encoding="utf-8")

    rc = await main(["--symbols-file", str(p)])   # no --confirm-live
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["mode"] == "dry_run"
    assert out["universe"]["us_count"] == 1
```

- [ ] **Run it — fails.** `uv run pytest tests/test_quote_parity_shadow_probe.py -v` → `ModuleNotFoundError: scripts.quote_parity_shadow_probe`.

- [ ] **Minimal impl — create the script.** Create `scripts/quote_parity_shadow_probe.py` following the `diagnose_invest_screener_toss_parity.py` scaffolding (argparse, `setup_logging_and_sentry`, `AsyncSessionLocal`, secret-reject, `print(json.dumps(...))`, `raise SystemExit(asyncio.run(main()))`). Key bodies:
  - `load_symbols_file`: read text, `_reject_if_sensitive("file", text)`, parse JSON list / CSV `DictReader`, per row `market = row["market"].upper()`, `symbol = to_db_symbol(row["symbol"].strip())`, split into `kr`/`us`, de-dupe preserving order.
  - `enumerate_db_universe`: `ManualHoldingsService(session).get_holdings_by_user(user_id, broker_type="toss")`, `kr = [to_db_symbol(h.ticker) for h in holds if h.market_type == MarketType.KR][:limit]`, likewise US.
  - `exit_code_for` as specified.
  - `_build_live_clients()` helper (so the dry-run test can assert it is never called): returns `(toss_client, quote_service)`; called ONLY on the live branch.
  - `main`: parse; resolve universe (`--symbols-file` precedence, else DB via `--user-id`); if not `--confirm-live`: print dry-run dict (`mode="dry_run"`, `universe`, `planned_toss_batches = ceil(kr/200)+ceil(us/200)`) and `return 0`; else live path per Interfaces, print report, return `exit_code_for(...)`; outer `try/except Exception: logger.exception(...); return 1`.

- [ ] **Run it — passes.** `uv run pytest tests/test_quote_parity_shadow_probe.py -v` → all pass.

- [ ] **Regression — whole shadow suite + quote service.** `uv run pytest tests/test_quote_parity_shadow.py tests/test_quote_parity_shadow_probe.py tests/test_invest_quote_service.py -q` → no failures.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-709): quote_parity_shadow_probe operator script (dry-run-default, exit go=0/no_go|blocked=2/crash=1)"`

---

## Done criteria

- A read-only operator probe (`scripts/quote_parity_shadow_probe.py`) calls **both** the Toss `prices()` batch and the raw KIS batch layer over the /invest KR+US universe and emits structured go/no-go JSON; the `/invest` hot path and the production resolver chain are byte-for-byte unchanged (verified by `tests/test_invest_quote_service.py` unchanged + the additive-only passthroughs).
- **Coverage**: dotted US (`BRK.B`) echo-match is production-identical; silent drops are counted (allowlist-aware). Proven by `TestCoverage`.
- **Divergence**: KR tick-normalized (fresh KRX table, NOT the Upbit ladder), US bps, median/p99 via deterministic percentile — split KR vs US. Proven by `TestDivergence`.
- **Currency**: KR→KRW / US→USD mis-key detection — the only guard, since the resolver discards Toss's currency tag. Proven by `TestCurrency` + orchestrator.
- **Off-hours**: Toss `last_price`/`timestamp` captured verbatim per US symbol (descriptive, not gated). Proven by the orchestrator test.
- **Latency**: Toss per-batch p50/p95/p99 + wall + error-rate; KIS wall + None-rate; go-bar `toss_wall ≤ kis_wall` and `toss_err ≤ kis_err`. Proven by `TestLatency` + `TestGoNoGo`.
- **ROB-708 precondition enforced**: with `us_kis_live_last=False`, the US-divergence bar is `not_evaluable` and the decision is `blocked` — the operator cannot mistake a pre-ROB-708 run for a go. Proven by `test_blocked_when_us_precondition_unmet` + `test_orchestrator_blocked_until_rob708`.
- **Dry-run-default**: no `--confirm-live` ⇒ zero network, enumerate + print plan, exit 0. Proven by `test_dry_run_performs_no_network`.
- Exit codes: `go=0`, `no_go|blocked=2`, `crash=1`. `make lint` clean; no alembic revision added (migration-0).
```