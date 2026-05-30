# ROB-375 리포트 연속성/델타 레이어 버그 3건 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** advisory(항상-draft) 플로우에서 리포트 연속성/델타 신호를 회수할 수 있게 3개 조회·저장 버그를 수정한다.

**Architecture:** 3개 독립 슬라이스(각 1 PR), 우선순위 순서 Slice 1(Bug 3) → Slice 2(Bug 1) → Slice 3(Bug 2). 모두 opt-in/하위호환, 마이그레이션 0건, read-only.

**Tech Stack:** Python 3.13, FastAPI/SQLAlchemy async, pytest, MCP tooling, JSONB.

**Spec:** `docs/superpowers/specs/2026-05-31-rob-375-report-continuity-delta-design.md`

**공통 검증 (각 슬라이스 머지 전):** `uv run ruff check app/ tests/` + `uv run ruff format --check app/ tests/` + 해당 테스트 green + Test 워크플로 green.

---

## File Structure

- **Slice 1:** `app/mcp_server/tooling/trade_journal_tools.py` (enrich 로직), `app/mcp_server/tooling/trade_journal_registration.py` (파라미터/docstring), `tests/mcp/test_trade_journal_enrich.py` (신규).
- **Slice 2:** `app/services/investment_reports/query_service.py` (`include_draft`), `app/mcp_server/tooling/investment_reports_handlers.py` (도구 노출), `tests/` 해당 테스트.
- **Slice 3:** `app/services/action_report/snapshot_backed/generator.py` (numeric baseline 동결), `tests/` 해당 테스트.

---

## Slice 1 — Bug 3: `get_trade_journal` opt-in 라이브 enrich

### Task 1.1: enrich 헬퍼 + `enrich_live` 파라미터 (failing test 먼저)

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Test: `tests/mcp/test_trade_journal_enrich.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_trade_journal_enrich.py
import pytest

from app.mcp_server.tooling import trade_journal_tools as tjt


def test_enrich_entry_long_target_reached(monkeypatch):
    # long position, current >= target -> target_reached True, stop False
    entry = {"entry_price": 100.0, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=112.0, side="buy")
    assert entry["current_price"] == 112.0
    assert entry["pnl_pct_live"] == pytest.approx(12.0)
    assert entry["target_reached"] is True
    assert entry["stop_reached"] is False


def test_enrich_entry_long_stop_reached(monkeypatch):
    entry = {"entry_price": 100.0, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=88.0, side="buy")
    assert entry["stop_reached"] is True
    assert entry["target_reached"] is False
    assert entry["pnl_pct_live"] == pytest.approx(-12.0)


def test_enrich_entry_short_inverts(monkeypatch):
    # short: target below entry, current <= target -> target_reached
    entry = {"entry_price": 100.0, "target_price": 90.0, "stop_loss": 110.0}
    tjt._apply_live_enrich(entry, current_price=88.0, side="sell")
    assert entry["target_reached"] is True
    assert entry["stop_reached"] is False
    # short pnl positive when price falls
    assert entry["pnl_pct_live"] == pytest.approx(12.0)


def test_enrich_entry_missing_entry_price_leaves_pnl_null():
    entry = {"entry_price": None, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=112.0, side="buy")
    assert entry["current_price"] == 112.0
    assert entry["pnl_pct_live"] is None
    assert entry["target_reached"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp/test_trade_journal_enrich.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_apply_live_enrich'`

- [ ] **Step 3: Add the pure enrich helper**

`app/mcp_server/tooling/trade_journal_tools.py` — add near the top (after `_serialize_journal`):

```python
_LONG_SIDES = {"buy", "long"}


def _apply_live_enrich(
    entry: dict[str, Any], *, current_price: float, side: str | None
) -> None:
    """Mutate ``entry`` in place with live target/stop/pnl judgements.

    Pure: takes an already-fetched ``current_price`` so it is trivially
    testable without network. Leaves a field ``None`` when its inputs are
    missing rather than fabricating a value.
    """
    is_long = (side or "buy").strip().lower() in _LONG_SIDES
    entry["current_price"] = current_price

    entry_price = entry.get("entry_price")
    if entry_price:
        raw_pct = (current_price - entry_price) / entry_price * 100.0
        entry["pnl_pct_live"] = raw_pct if is_long else -raw_pct
    else:
        entry["pnl_pct_live"] = None

    target = entry.get("target_price")
    if target is not None:
        entry["target_reached"] = (
            current_price >= target if is_long else current_price <= target
        )
    stop = entry.get("stop_loss")
    if stop is not None:
        entry["stop_reached"] = (
            current_price <= stop if is_long else current_price >= stop
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp/test_trade_journal_enrich.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/mcp/test_trade_journal_enrich.py
git commit -m "feat(ROB-375): pure live-enrich helper for trade journal (Bug 3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 1.2: wire `enrich_live` into `get_trade_journal`

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py:200-324`
- Test: `tests/mcp/test_trade_journal_enrich.py`

- [ ] **Step 1: Write the failing test (quote stub, near-target summary)**

```python
async def test_get_trade_journal_enrich_live_summary(monkeypatch, journal_factory):
    # journal_factory creates one active US journal: entry 100, target 110, stop 90
    j = await journal_factory(symbol="BAC", entry_price=100.0,
                              target_price=110.0, stop_loss=90.0,
                              instrument_type="equity_us", side="buy")

    async def _fake_quote(symbol, market):
        from app.services.market_data.contracts import Quote
        return Quote(symbol=symbol, market="equity_us", price=109.5, source="test")

    monkeypatch.setattr(
        "app.services.market_data.service.get_quote", _fake_quote
    )
    res = await tjt.get_trade_journal(market="us", enrich_live=True)
    assert res["success"] is True
    e = res["entries"][0]
    assert e["current_price"] == 109.5
    assert e["target_reached"] is False
    # within 1.5% of target (109.5 vs 110) -> near_target counted
    assert res["summary"]["near_target"] == 1


async def test_get_trade_journal_enrich_false_is_unchanged(journal_factory):
    await journal_factory(symbol="BAC", instrument_type="equity_us")
    res = await tjt.get_trade_journal(market="us")  # enrich_live defaults False
    e = res["entries"][0]
    assert e["current_price"] is None
    assert e["pnl_pct_live"] is None
    assert res["summary"]["near_target"] == 0
```

> Note: if no `journal_factory` fixture exists, add a minimal one to `tests/mcp/conftest.py` (or the test file) that inserts a `TradeJournal` row via the test session. Follow the existing trade-journal test fixtures in `tests/` (grep `TradeJournal(` under `tests/`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp/test_trade_journal_enrich.py -k enrich_live -v`
Expected: FAIL — `get_trade_journal() got an unexpected keyword argument 'enrich_live'`

- [ ] **Step 3: Add param + market map + enrich loop**

In `trade_journal_tools.py`, add the instrument→market map near `_LONG_SIDES`:

```python
_QUOTE_MARKET_BY_INSTRUMENT = {
    InstrumentType.equity_us: "us",
    InstrumentType.equity_kr: "kr",
    InstrumentType.crypto: "crypto",
}
_NEAR_PCT = 1.5  # within ±1.5% of target/stop counts as "near"
```

Add `enrich_live: bool = False` to the signature (after `paperclip_issue_id`):

```python
    paperclip_issue_id: str | None = None,
    enrich_live: bool = False,
) -> dict[str, Any]:
```

Replace the hardcoded null block (currently lines ~302-307) with:

```python
                # Live enrichment is opt-in (per-entry quote is slow for bulk).
                entry["current_price"] = None
                entry["pnl_pct_live"] = None
                entry["target_reached"] = None
                entry["stop_reached"] = None
                if enrich_live:
                    market_alias = _QUOTE_MARKET_BY_INSTRUMENT.get(j.instrument_type)
                    if market_alias is not None:
                        from app.services.market_data.service import get_quote

                        try:
                            quote = await get_quote(j.symbol, market_alias)
                        except Exception:  # fail-open per entry
                            logger.debug(
                                "enrich_live quote failed for %s", j.symbol,
                                exc_info=True,
                            )
                        else:
                            _apply_live_enrich(
                                entry, current_price=quote.price, side=j.side
                            )
                            if j.status == JournalStatus.active:
                                tgt = entry.get("target_price")
                                stp = entry.get("stop_loss")
                                if tgt and abs(quote.price - tgt) / tgt * 100 <= _NEAR_PCT:
                                    near_target += 1
                                if stp and abs(quote.price - stp) / stp * 100 <= _NEAR_PCT:
                                    near_stop += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_trade_journal_enrich.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/mcp/test_trade_journal_enrich.py
git commit -m "feat(ROB-375): get_trade_journal enrich_live opt-in (Bug 3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 1.3: expose `enrich_live` in MCP registration + docstring

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_registration.py`

- [ ] **Step 1: Inspect registration**

Run: `grep -n "enrich\|get_trade_journal\|def \|param" app/mcp_server/tooling/trade_journal_registration.py | head -40`

- [ ] **Step 2: Add `enrich_live` to the registered signature**

Mirror the `get_trade_journal` parameter list in the registration wrapper (add `enrich_live: bool = False`) and pass it through to the impl. Update the docstring to add:
> `enrich_live` (optional, default False): fetch live quotes to compute current_price/pnl_pct_live/target_reached/stop_reached and near_target/near_stop. Slower (one quote per returned entry); fail-open per entry.

- [ ] **Step 3: Run the full trade-journal test module**

Run: `uv run pytest tests/ -k trade_journal -v`
Expected: PASS (no regressions)

- [ ] **Step 4: Lint + commit**

```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
git add app/mcp_server/tooling/trade_journal_registration.py
git commit -m "feat(ROB-375): register enrich_live param for get_trade_journal (Bug 3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

> **Slice 1 PR gate:** push branch, open PR, confirm full CI green before merge.

---

## Slice 2 — Bug 1: `include_draft` for report context

### Task 2.1: `include_draft` on `previous_report_context`

**Files:**
- Modify: `app/services/investment_reports/query_service.py:258-276`
- Test: existing query_service test module (grep `previous_report_context` under `tests/`) or new `tests/services/test_report_context_include_draft.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_report_context_include_draft.py
import pytest

# Use the project's report-ingest test helpers to insert reports.
# (grep tests/ for an existing fixture that inserts InvestmentReport rows,
#  e.g. publish_report / ingest helper used by ROB-352 Slice B tests.)


async def test_include_draft_true_returns_draft_priors(report_ctx_env):
    # report_ctx_env inserts two reports for (market=us, account_scope=kis_live):
    #   one status=draft (newer), one status=active (older)
    svc = report_ctx_env.service
    excl = False

    default_ctx = await svc.previous_report_context(
        market="us", account_scope="kis_live"
    )
    # default drops drafts -> only the active one
    assert [r.status for r in default_ctx["prior_reports"]] == ["active"]

    incl_ctx = await svc.previous_report_context(
        market="us", account_scope="kis_live", include_draft=True
    )
    statuses = sorted(r.status for r in incl_ctx["prior_reports"])
    assert statuses == ["active", "draft"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_report_context_include_draft.py -v`
Expected: FAIL — `previous_report_context() got an unexpected keyword argument 'include_draft'`

- [ ] **Step 3: Add the parameter and guard the filter**

`app/services/investment_reports/query_service.py` — add to signature:

```python
        exclude_report_uuid: UUID | None = None,
        n_prior: int = 3,
        events_since: datetime | None = None,
        include_draft: bool = False,
    ) -> dict[str, Any]:
```

Replace the unconditional draft drop (line ~275):

```python
        if not include_draft:
            prior_reports = [r for r in prior_reports if r.status != "draft"]
        prior_reports = prior_reports[:n_prior]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_report_context_include_draft.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/query_service.py tests/services/test_report_context_include_draft.py
git commit -m "feat(ROB-375): include_draft opt-in for previous_report_context (Bug 1)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 2.2: expose `include_draft` on the MCP tool

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py:416-435`
- Modify: MCP registration for `investment_report_context_get` (grep `investment_report_context_get` in the registration module)

- [ ] **Step 1: Write the failing test**

```python
async def test_context_get_impl_forwards_include_draft(report_ctx_env, monkeypatch):
    captured = {}

    async def _spy(self, **kwargs):
        captured.update(kwargs)
        return report_ctx_env.empty_ctx  # a valid empty context dict

    monkeypatch.setattr(
        "app.services.investment_reports.query_service.InvestmentReportQueryService.previous_report_context",
        _spy,
    )
    from app.mcp_server.tooling.investment_reports_handlers import (
        investment_report_context_get_impl,
    )
    await investment_report_context_get_impl(
        market="us", account_scope="kis_live", include_draft=True
    )
    assert captured["include_draft"] is True
```

> If mocking the bound method is awkward, instead assert end-to-end: insert a draft prior, call the impl with `include_draft=True`, and assert `prior_reports` is non-empty in the returned dict.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ -k context_get_impl_forwards_include_draft -v`
Expected: FAIL — unexpected keyword argument `include_draft`

- [ ] **Step 3: Add the parameter and forward it**

In `investment_report_context_get_impl` add `include_draft: bool = False` to the signature and pass `include_draft=include_draft` into `service.previous_report_context(...)`.

Update the registration wrapper for `investment_report_context_get` to accept and forward `include_draft: bool = False`, with docstring:
> `include_draft` (optional, default False): include draft reports as prior context. advisory reports persist as draft, so set True to chain the next delta report off the latest advisory baseline.

- [ ] **Step 4: Run test + regression**

Run: `uv run pytest tests/ -k "context_get or previous_report_context" -v`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
git add app/mcp_server/tooling/investment_reports_handlers.py app/mcp_server/tooling/*registration*.py tests/
git commit -m "feat(ROB-375): expose include_draft on investment_report_context_get (Bug 1)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

> **Slice 2 PR gate:** push branch, open PR, confirm full CI green before merge.

---

## Slice 3 — Bug 2: freeze numeric baseline into report row snapshots

### Task 3.0: confirm the empty-`{}` write path (investigation, no code)

- [ ] **Step 1: Identify which path wrote `market_snapshot:{}` for the repro rows**

Run:
```bash
grep -rn "market_snapshot\|portfolio_snapshot" app/services/investment_reports/mock_preview/ app/services/investment_stages/ app/services/action_report/snapshot_backed/
```
Determine whether the repro rows (dfda9a04/7004e783) came from `generator.py` (descriptor path) or `mock_preview/runner.py` / `hermes_ingest.py` (which may pass `{}`). Record the finding as a comment in the PR description. The fix below targets `generator.py`; if mock_preview/hermes also emit empty snapshots and are in scope, apply the same baseline helper there. **Do not** widen scope into ROB-376 feature paths.

### Task 3.1: numeric baseline extraction helper (pure, failing test first)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py`
- Test: `tests/services/test_snapshot_baseline.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_snapshot_baseline.py
from app.services.action_report.snapshot_backed import generator as gen


def test_market_baseline_whitelists_indices():
    payload = {
        "market": "us",
        "from_date": "2026-05-30",
        "to_date": "2026-05-30",
        "event_count": 3,
        "events": [{"big": "blob"}],  # excluded
        "indices": {"SPX": {"price": 5300.0, "change_pct": 0.4}},
    }
    base = gen._market_numeric_baseline(payload)
    assert base["indices"] == {"SPX": {"price": 5300.0, "change_pct": 0.4}}
    assert base["market"] == "us"
    assert "events" not in base  # heavy list not copied


def test_portfolio_baseline_whitelists_cash_and_summary():
    payload = {
        "holdings": [{"ticker": "BAC"}],  # excluded heavy list
        "primary_source": "kis_live",
        "cash": {"usd_cash": 3095.26, "usd_orderable": 3078.32},
        "buying_power": {"usd": 3078.32},
        "sellable_summary": {"count": 4},
    }
    base = gen._portfolio_numeric_baseline(payload)
    assert base["cash"] == {"usd_cash": 3095.26, "usd_orderable": 3078.32}
    assert base["buying_power"] == {"usd": 3078.32}
    assert base["sellable_summary"] == {"count": 4}
    assert base["primary_source"] == "kis_live"
    assert base["holdings_count"] == 1
    assert "holdings" not in base


def test_baselines_handle_missing_keys():
    assert gen._market_numeric_baseline({}) == {}
    assert gen._portfolio_numeric_baseline({}) == {"holdings_count": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_snapshot_baseline.py -v`
Expected: FAIL — `module ... has no attribute '_market_numeric_baseline'`

- [ ] **Step 3: Add the two pure helpers**

In `generator.py` (module level):

```python
_MARKET_BASELINE_KEYS = ("market", "from_date", "to_date", "indices")
_PORTFOLIO_BASELINE_KEYS = (
    "primary_source",
    "cash",
    "buying_power",
    "sellable_summary",
)


def _market_numeric_baseline(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the small delta-relevant numerics from a market snapshot
    payload. Never copies the heavy ``events`` list. Missing keys are skipped
    (no fabrication)."""
    return {k: payload[k] for k in _MARKET_BASELINE_KEYS if k in payload}


def _portfolio_numeric_baseline(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist cash/buying_power/sellable_summary + a holdings count.
    Never copies the heavy ``holdings`` list."""
    base = {k: payload[k] for k in _PORTFOLIO_BASELINE_KEYS if k in payload}
    holdings = payload.get("holdings")
    base["holdings_count"] = len(holdings) if isinstance(holdings, list) else 0
    return base
```

(`Mapping` and `Any` are already imported in this module — verify with `grep -n "from typing import\|Mapping" app/services/action_report/snapshot_backed/generator.py`; add to the import if absent.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_snapshot_baseline.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/test_snapshot_baseline.py
git commit -m "feat(ROB-375): numeric baseline extractors for report snapshots (Bug 2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 3.2: fold baseline into `_section_snapshot_descriptors`

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py:528-569`
- Test: existing generator test module (grep `_section_snapshot_descriptors` under `tests/`) or extend `tests/services/test_snapshot_baseline.py`

- [ ] **Step 1: Write the failing test**

```python
# Extend the report row to carry provenance + baseline.
# Insert a bundle with a 'market' snapshot whose payload has indices and a
# 'portfolio' snapshot whose payload has cash, then assert the descriptor
# dict now has shape {"provenance": {...}, "baseline": {...}}.
async def test_section_descriptors_carry_baseline(snapshot_backed_env):
    market, portfolio = await snapshot_backed_env.generator._section_snapshot_descriptors(
        bundle_uuid=snapshot_backed_env.bundle_uuid,
        unavailable_sources={},
    )
    assert market["provenance"]["snapshot_kind"] == "market"
    assert "indices" in market["baseline"]
    assert portfolio["provenance"]["snapshot_kind"] == "portfolio"
    assert "cash" in portfolio["baseline"]
```

> Reuse the existing snapshot-backed generator fixtures (grep `_section_snapshot_descriptors` / `SnapshotBackedReportGenerator(` under `tests/` for the setup helper that builds a bundle with market/portfolio snapshots).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ -k section_descriptors_carry_baseline -v`
Expected: FAIL — `KeyError: 'provenance'` (current code returns flat descriptor)

- [ ] **Step 3: Wrap descriptor + add baseline from `payload_json`**

In `_section_snapshot_descriptors`, change `_descriptor` and the kind loop so each section becomes `{"provenance": <descriptor>, "baseline": <numeric>}`, and `_unavailable` stays as the unavailable marker:

```python
        def _section(snapshot: Any, baseline: dict[str, Any]) -> dict[str, Any]:
            as_of = getattr(snapshot, "as_of", None)
            return {
                "provenance": {
                    "snapshot_uuid": str(snapshot.snapshot_uuid),
                    "snapshot_kind": snapshot.snapshot_kind,
                    "as_of": as_of.isoformat() if as_of is not None else None,
                    "freshness_status": getattr(snapshot, "freshness_status", None),
                    "coverage": getattr(snapshot, "coverage_json", None) or {},
                },
                "baseline": baseline,
            }

        market = _unavailable("market")
        portfolio = _unavailable("portfolio")
        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            return market, portfolio
        pairs = await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        for _item, snapshot in pairs:
            payload = getattr(snapshot, "payload_json", None) or {}
            if snapshot.snapshot_kind == "market":
                market = _section(snapshot, _market_numeric_baseline(payload))
            elif snapshot.snapshot_kind == "portfolio":
                portfolio = _section(snapshot, _portfolio_numeric_baseline(payload))
        return market, portfolio
```

- [ ] **Step 4: Run test + the generator regression module**

Run: `uv run pytest tests/ -k "section_descriptors or snapshot_backed" -v`
Expected: PASS. If any existing test asserted the old flat descriptor shape, update it to read `["provenance"]` (these are provenance-shape assertions, not behavior changes).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
git add app/services/action_report/snapshot_backed/generator.py tests/
git commit -m "feat(ROB-375): freeze numeric baseline in report row snapshots (Bug 2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

> **Slice 3 PR gate:** push branch, open PR, confirm full CI green before merge.

---

## Self-Review notes

- **Spec coverage:** Slice 1 ↔ Bug 3 (Tasks 1.1–1.3); Slice 2 ↔ Bug 1 (Tasks 2.1–2.2); Slice 3 ↔ Bug 2 (Tasks 3.0–3.2). All spec sections mapped.
- **Hidden assumptions flagged for the implementer:** (a) test fixture names (`journal_factory`, `report_ctx_env`, `snapshot_backed_env`) are illustrative — reuse the project's existing report/journal test fixtures; grep before inventing. (b) Slice 3 row-shape change (`{provenance, baseline}`) is a NEW consumer contract — if any reader downstream reads `market_snapshot["snapshot_uuid"]` flat, update it (grep `market_snapshot[` / `portfolio_snapshot[`). (c) Task 3.0 must confirm the actual empty-`{}` path before assuming generator.py is the only writer.
