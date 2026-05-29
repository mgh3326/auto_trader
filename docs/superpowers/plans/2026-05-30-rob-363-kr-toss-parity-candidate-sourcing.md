# ROB-363 KR /invest/reports Toss-parity Candidate Sourcing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch KR `/invest/reports` new-buy candidate sourcing from a single `top_gainers` momentum ranking to a multi-source fan-in over the Toss-parity screener presets (`consecutive_gainers`, `double_buy`, `high_yield_value`), preserving honest per-candidate freshness + Toss-parity lineage and demoting stale/missing candidates instead of overstating them.

**Architecture:** The `candidate_universe` collector (`app/services/action_report/snapshot_backed/collectors/candidate_universe.py`) gains a KR-only multi-source path that calls the three existing deterministic snapshot loaders in `app/services/invest_view_model/` directly (no new service layer — these are pure DB reads, no LLM, so the deterministic-prepare boundary holds). Rows from each preset are normalized into `CandidateEvidence` by an extended `build_candidate_evidence`, merged by symbol (preserving per-source reasons), ranked by a deterministic priority (full-parity + fresh + score first), then capped to `candidate_limit`. Per-candidate `data_state` flows from each loader's `_screener_snapshot_state`; `auto_emit` is extended to demote stale candidates per-row rather than only on a universe-wide flag. US keeps `top_gainers`; crypto is untouched.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest (`pytest.mark.asyncio`, `db_session` fixture), Ruff, `uv run`.

---

## Locked Design Decisions

These resolve the open questions surfaced during the ROB-363 review. Do not re-litigate during execution.

1. **Layering — direct import, no new service.** The collector already imports `get_preset` from `app.services.invest_view_model.screener_presets` (`candidate_universe.py:126`). It may likewise import the three loaders. The loaders are deterministic DB reads with no in-process LLM, so the "deterministic prepare → Hermes pull/compose" boundary (memory: `/invest/reports has no internal LLM`) is preserved. No `kr_candidate_sources` service is created (YAGNI).
2. **Uniform loader contract: `list[dict] | None`.** `None` = no partition at all (missing); `[]` = partition exists but no qualifiers (stale-empty); rows = results. `double_buy` and `high_yield_value` already match this. `consecutive_gainers` gets a thin public wrapper returning `.rows` / `None`.
3. **Per-candidate `data_state` from each row's `_screener_snapshot_state`** (`fresh`/`stale`), NOT a universe-wide value. A missing source contributes no rows.
4. **Merge** dedupes by `to_db_symbol(symbol)`, keeps the highest-priority occurrence, and UNIONS `reasons` (deduped, order-preserving) across every preset that surfaced the symbol. `source_preset` is the winning (highest-priority) preset; merged reasons retain each preset's provenance string.
5. **Priority** is a deterministic sort key: `(parity_rank, freshness_rank, -score, symbol)` where `parity_rank` is `full=0 / partial=1 / mismatch=2 / not_toss_parity=3` and `freshness_rank` is `fresh=0 / stale=1`. Internal pool is gathered wider than `candidate_limit`, then sliced.
6. **KR-only.** Preset sourcing is gated to `market == "kr"`. `market == "us"` keeps the existing `top_gainers` path verbatim; `market == "crypto"` is untouched.
7. **No `rejected` from the deterministic layer** (memory: `ROB-350 ... never fabricate rejected→Hermes`). Demotions are `watch_only` / `data_gap` / lower-confidence only. The starter prompt's mention of `rejected` is superseded by this.

---

## File Structure

| File | Responsibility | PRs |
|---|---|---|
| `app/services/invest_view_model/screener_service.py` | Add public `load_consecutive_gainers_from_snapshots` wrapper over the existing private loader | PR1 |
| `app/services/screener_evidence/builder.py` | Per-preset scoring/reasons for `consecutive_gainers`, `double_buy`, `high_yield_value`; accept per-row `data_state` passthrough | PR1, PR2 |
| `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` | KR multi-source fan-in, per-candidate freshness, cross-preset merge, deterministic priority + cap | PR1, PR2, PR3 |
| `app/services/action_report/snapshot_backed/action_verdict.py` | `classify_candidate_symbol` gains `candidate_fresh` param for per-row demotion | PR3 |
| `app/services/action_report/snapshot_backed/auto_emit.py` | Pass per-candidate freshness into `classify_candidate_symbol` | PR3 |
| `tests/services/screener_evidence/test_builder.py` | Builder per-preset unit tests | PR1, PR2 |
| `tests/services/action_report/test_candidate_universe_collector_evidence.py` | Collector multi-source, merge, priority, freshness tests | PR1, PR2, PR3 |
| `tests/test_auto_emit_candidate_citation.py` | Per-candidate stale demotion + lineage end-to-end | PR3 |

---

# PR1 — KR multi-source scaffold (consecutive_gainers wired)

**Branch:** `rob-363` (current worktree, at `origin/main` `8057607f`).
**Outcome:** KR `candidate_universe` sources from `consecutive_gainers` (full Toss parity) with `top_gainers` retained as fallback; per-candidate `data_state` honest; one preset proves the multi-source plumbing end-to-end. US/crypto unchanged.

### Task 1.1: Public `load_consecutive_gainers_from_snapshots` wrapper

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py` (add public wrapper after the private `_load_consecutive_gainers_from_snapshots`, ends at line 551)

- [ ] **Step 1: Write the failing test**

Create `tests/services/invest_view_model/test_consecutive_gainers_public_loader.py`:

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_view_model.screener_service import (
    load_consecutive_gainers_from_snapshots,
)


@pytest.mark.asyncio
async def test_public_wrapper_returns_rows_or_none(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()

    # No partition at all -> None (missing).
    assert (
        await load_consecutive_gainers_from_snapshots(db_session, market="kr")
    ) is None

    today = dt.date(2026, 5, 29)
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=today,
            latest_close=Decimal("70000"),
            change_rate=Decimal("1.5"),
            week_change_rate=Decimal("6.0"),
            consecutive_up_days=6,
            closes_window=[1, 2, 3],
            source="kis",
        )
    )
    await db_session.commit()

    rows = await load_consecutive_gainers_from_snapshots(db_session, market="kr")
    assert rows is not None
    assert isinstance(rows, list)
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["_screener_snapshot_state"] in {"fresh", "stale"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/invest_view_model/test_consecutive_gainers_public_loader.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_consecutive_gainers_from_snapshots'`

- [ ] **Step 3: Add the public wrapper**

In `app/services/invest_view_model/screener_service.py`, immediately after the private `_load_consecutive_gainers_from_snapshots` definition (after line 551), add:

```python
async def load_consecutive_gainers_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = _SNAPSHOT_FIRST_LIMIT,
) -> list[dict[str, Any]] | None:
    """Public, uniform-contract wrapper over ``_load_consecutive_gainers_from_snapshots``.

    Returns the qualifying snapshot rows (each carrying ``_screener_snapshot_state``),
    ``[]`` when the latest partition has no qualifiers, or ``None`` when no partition
    exists / the check could not run — matching the ``load_double_buy_from_snapshots``
    and ``load_high_yield_value_from_snapshots`` contract so the candidate_universe
    collector can treat all three Toss-parity presets uniformly (ROB-363).
    """
    result = await _load_consecutive_gainers_from_snapshots(
        session, market=market, limit=limit
    )
    if result is None:
        return None
    return result.rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/invest_view_model/test_consecutive_gainers_public_loader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/services/invest_view_model/test_consecutive_gainers_public_loader.py
git commit -m "feat(ROB-363): public load_consecutive_gainers_from_snapshots wrapper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 1.2: Builder supports `consecutive_gainers` rows

**Files:**
- Modify: `app/services/screener_evidence/builder.py` (the `else` branch at lines 82-91 already handles `consecutive_up_days`; `consecutive_gainers` rows use `week_change_rate`/`consecutive_up_days` and key `close` not `price`)
- Test: `tests/services/screener_evidence/test_builder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/screener_evidence/test_builder.py`:

```python
def test_consecutive_gainers_preset_reasons_and_score():
    from app.services.screener_evidence.builder import build_candidate_evidence

    rows = [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "source": "kis",
            "change_rate": 2.0,
            "close": 70000,
            "week_change_rate": 8.0,
            "consecutive_up_days": 6,
            "volume": 1_000_000,
        }
    ]
    out = build_candidate_evidence(
        market="kr", preset="consecutive_gainers", rows=rows
    )
    assert len(out) == 1
    ev = out[0]
    assert ev.source_preset == "consecutive_gainers"
    assert ev.price == 70000.0  # reads `close` when `price` absent
    assert any("연속 상승" in r for r in ev.reasons)
    assert ev.score > 5.0  # +2.0% momentum -> above neutral
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py::test_consecutive_gainers_preset_reasons_and_score -v`
Expected: FAIL — `ev.price` is `None` because the builder only reads `row.get("price") or row.get("latest_close")`, not `close`.

- [ ] **Step 3: Extend the builder price/volume fallbacks**

In `app/services/screener_evidence/builder.py`, change the price read (line 68) from:

```python
        price = _to_float(row.get("price") or row.get("latest_close"))
```

to:

```python
        price = _to_float(
            row.get("price") or row.get("latest_close") or row.get("close")
        )
```

And in the `else` momentum branch volume read (lines 86-88), change from:

```python
            volume_value = _to_float(
                row.get("trade_amount_24h") or row.get("daily_volume")
            )
```

to:

```python
            volume_value = _to_float(
                row.get("trade_amount_24h")
                or row.get("daily_volume")
                or row.get("volume")
            )
```

(The existing `else` branch already appends the `{up_days}일 연속 상승` reason when `consecutive_up_days >= 2`, and `consecutive_gainers` rows carry `consecutive_up_days >= 5`, so the reason is produced. `source_preset` is already set to the `preset` argument at line 106.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py -v`
Expected: PASS (all existing builder tests still pass)

- [ ] **Step 5: Commit**

```bash
git add app/services/screener_evidence/builder.py tests/services/screener_evidence/test_builder.py
git commit -m "feat(ROB-363): builder reads close/volume keys for consecutive_gainers rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 1.3: Collector KR path sources `consecutive_gainers` with top_gainers fallback

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/test_candidate_universe_collector_evidence.py`:

```python
@pytest.mark.asyncio
async def test_kr_collector_sources_consecutive_gainers_preset(db_session):
    """ROB-363 — KR candidate source is consecutive_gainers (full Toss parity),
    not top_gainers, when the preset returns rows. Per-candidate data_state and
    toss_parity_status reflect the real preset."""
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()

    today = dt.date(2026, 5, 29)
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=today,
            latest_close=Decimal("70000"),
            change_rate=Decimal("2.0"),
            week_change_rate=Decimal("8.0"),
            consecutive_up_days=6,
            closes_window=[1, 2, 3, 4, 5],
            source="kis",
        )
    )
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(
            market="kr", account_scope=None, symbols=[], policy_snapshot={}
        )
    )
    payload = results[0].payload_json
    syms = [c["symbol"] for c in payload["candidates"]]
    assert "005930" in syms
    top = next(c for c in payload["candidates"] if c["symbol"] == "005930")
    assert top["source_preset"] == "consecutive_gainers"
    assert top["toss_parity_status"] == "full"
    assert top["data_state"] in {"fresh", "stale"}
```

Note: this test must NOT inject `InvestScreenerSnapshot` import — it is already imported at the top of the file via `from app.models.invest_screener_snapshot import ...`? Check: the existing file imports `InvestCryptoScreenerSnapshot` only. Add at the top of the test file:

```python
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py::test_kr_collector_sources_consecutive_gainers_preset -v`
Expected: FAIL — `source_preset` is `"top_gainers"` and `toss_parity_status` is `"not_toss_parity"` (current `_collect_equity` hardcodes `top_gainers`).

- [ ] **Step 3: Add a KR preset-sourcing branch to `_collect_equity`**

In `candidate_universe.py`, add this module-level helper after `_crypto_row_to_input` (after line 113):

```python
def _gainers_row_to_input(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a consecutive_gainers loader row into builder input.

    The loader emits ``close``/``week_change_rate``/``consecutive_up_days``/
    ``volume`` plus ``_screener_snapshot_state`` (ROB-363). The builder's
    momentum branch reads these via the close/volume fallbacks and appends the
    연속 상승 reason.
    """
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "source": row.get("source") or "kis",
        "change_rate": row.get("change_rate"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "consecutive_up_days": row.get("consecutive_up_days"),
    }
```

Then rewrite `_collect_equity` (lines 211-243). The KR branch sources `consecutive_gainers`; falls back to `top_gainers` when the preset returns `None` (missing) or `[]` (no qualifiers). US keeps `top_gainers`. Replace the method body with:

```python
    async def _collect_equity(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        limit = _candidate_limit(request)
        if request.market == "kr":
            preset_result = await self._collect_kr_presets(request, now, limit)
            if preset_result is not None:
                return preset_result
        # US, or KR with no preset rows -> top_gainers momentum fallback.
        return await self._collect_top_gainers(request, now, limit)

    async def _collect_top_gainers(
        self, request: CollectorRequest, now: dt.datetime, limit: int
    ) -> list[SnapshotCollectResult]:
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=now.date()
        )
        usefulness = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=limit
        )
        rows = _dedupe_rows(rows, key=lambda r: to_db_symbol(r.symbol))
        evidence = build_candidate_evidence(
            market=request.market,
            preset="top_gainers",
            rows=[_equity_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market=request.market,
                preset="top_gainers",
                evidence=evidence,
                candidate_limit=limit,
                fresh_count=coverage.fresh_count,
                stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at,
                usefulness=usefulness,
            )
        ]
```

Add the new KR-preset method after `_collect_top_gainers`. PR1 wires only `consecutive_gainers`; PR2 extends `_KR_PRESET_LOADERS`:

```python
    async def _collect_kr_presets(
        self, request: CollectorRequest, now: dt.datetime, limit: int
    ) -> list[SnapshotCollectResult] | None:
        """KR Toss-parity preset sourcing (ROB-363). Returns None when no preset
        produced any rows, so the caller falls back to top_gainers."""
        from app.services.invest_view_model.screener_service import (
            load_consecutive_gainers_from_snapshots,
        )

        # (preset_id, loader) — PR2 adds double_buy + high_yield_value here.
        pool_limit = max(limit * 3, limit + 20)
        loaders = [
            ("consecutive_gainers", load_consecutive_gainers_from_snapshots),
        ]
        evidence: list[CandidateEvidence] = []
        per_state: dict[str, str] = {}  # db_symbol -> fresh|stale
        any_rows = False
        for preset_id, loader in loaders:
            rows = await loader(self._session, market="kr", limit=pool_limit)
            if not rows:  # None (missing) or [] (stale-empty)
                continue
            any_rows = True
            built = build_candidate_evidence(
                market="kr",
                preset=preset_id,
                rows=[_gainers_row_to_input(r) for r in rows],
            )
            for ev, src_row in zip(built, rows, strict=False):
                per_state[to_db_symbol(ev.symbol)] = (
                    src_row.get("_screener_snapshot_state") or "fresh"
                )
            evidence.extend(built)
        if not any_rows:
            return None

        evidence = _dedupe_evidence(evidence, key=lambda e: to_db_symbol(e.symbol))
        evidence = evidence[:limit]
        return [
            self._build_preset_candidate_result(
                request=request,
                now=now,
                evidence=evidence,
                per_state=per_state,
                candidate_limit=limit,
                universe_count=len(evidence),
            )
        ]
```

Add the evidence-dedupe helper next to `_dedupe_rows` (after line 85):

```python
def _dedupe_evidence(
    evidence: list[CandidateEvidence], *, key: Callable[[CandidateEvidence], Hashable]
) -> list[CandidateEvidence]:
    """Order-preserving dedupe of CandidateEvidence by ``key`` (ROB-363).

    PR1 keeps the first occurrence (highest-ranked from a single preset). PR2
    replaces this with a reason-merging variant once multiple presets feed the
    pool."""
    seen: set[Any] = set()
    out: list[CandidateEvidence] = []
    for ev in evidence:
        k = key(ev)
        if k in seen:
            continue
        seen.add(k)
        out.append(ev)
    return out
```

Add `_build_preset_candidate_result` after `_build_candidate_result`. It stamps per-candidate `data_state` from `per_state` and a per-candidate `toss_parity_status` from each candidate's own `source_preset`:

```python
    def _build_preset_candidate_result(
        self,
        *,
        request: CollectorRequest,
        now: dt.datetime,
        evidence: list[CandidateEvidence],
        per_state: dict[str, str],
        candidate_limit: int,
        universe_count: int,
    ) -> SnapshotCollectResult:
        fresh_count = sum(1 for v in per_state.values() if v == "fresh")
        stale_count = sum(1 for v in per_state.values() if v == "stale")
        usefulness = _classify_usefulness(actionable=fresh_count, stale=stale_count)
        candidates: list[dict[str, Any]] = []
        for rank, e in enumerate(evidence, start=1):
            db_sym = to_db_symbol(e.symbol)
            candidates.append(
                {
                    **e.to_payload_dict(),
                    "rank": rank,
                    "candidate_rank": rank,
                    "data_state": per_state.get(db_sym, "fresh"),
                    "toss_parity_status": _toss_parity_status(
                        e.source_preset or "top_gainers", "kr"
                    ),
                }
            )
        capped = universe_count > candidate_limit
        source_coverage = _source_coverage(evidence)
        payload: dict[str, Any] = {
            "market": "kr",
            "preset": "toss_parity_multi",
            "as_of": now.isoformat(),
            "freshness_status": _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial"),
            "source_coverage": source_coverage,
            "candidate_limit": candidate_limit,
            "universe_count": universe_count,
            "capped": capped,
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": None,
            "usefulness": usefulness,
            "missing_data": _missing_data("kr", usefulness),
        }
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=request.market,
            account_scope=request.account_scope,
            payload=payload,
            origin="auto_trader_db",
            as_of=now,
            freshness_status="fresh" if usefulness == "useful" else "partial",
            coverage={
                "actionable_count": fresh_count,
                "stale_count": stale_count,
                "usefulness": usefulness,
                "candidate_count": len(candidates),
                "candidate_limit": candidate_limit,
                "universe_count": universe_count,
                "capped": capped,
            },
        )
```

Also fix the stale ROB-346 attribution in the `_toss_parity_status` docstring (line 124): change `(candidate strategy — ROB-346)` to `(candidate strategy — ROB-363; US universe is ROB-346)`.

- [ ] **Step 4: Run the test + the full collector suite to verify**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -v`
Expected: PASS — the new KR test passes; existing crypto + US (`test_equity_collector_dedupes_symbol_format_variants` uses `market="us"`) + cap tests still pass. Note: `test_equity_collector_respects_candidate_limit` and the two cap tests use `market="kr"` with a `_FakeEquityRepository` and NO `invest_screener_snapshots` rows for `consecutive_gainers` → `_collect_kr_presets` returns `None` → falls back to `_collect_top_gainers` using the injected repo. Confirm those three still pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-363): KR candidate_universe sources consecutive_gainers preset (top_gainers fallback)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 1.4: PR1 verification gate

- [ ] **Step 1: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: no errors. (Fix with `uv run ruff format app/ tests/` if format check fails, then re-commit.)

- [ ] **Step 2: Run the three touched suites**

Run:
```bash
uv run pytest \
  tests/services/invest_view_model/test_consecutive_gainers_public_loader.py \
  tests/services/screener_evidence/test_builder.py \
  tests/services/action_report/test_candidate_universe_collector_evidence.py -v
```
Expected: all PASS.

- [ ] **Step 3: No-mutation boundary grep (evidence for PR description)**

Run:
```bash
git diff origin/main --name-only
grep -rn "submit_order\|place_order\|cancel_order\|modify_order\|preview_order" \
  app/services/action_report/snapshot_backed/collectors/candidate_universe.py \
  app/services/screener_evidence/builder.py \
  app/services/invest_view_model/screener_service.py || echo "NO MUTATION CALLS — boundary clean"
```
Expected: `NO MUTATION CALLS — boundary clean`.

- [ ] **Step 4: Push + open PR**

```bash
git push -u origin rob-363
gh pr create --base main --title "feat(ROB-363): KR /invest/reports consecutive_gainers candidate sourcing (PR1/3)" --body "$(cat <<'EOF'
## ROB-363 PR1/3 — KR multi-source scaffold

Switches KR `/invest/reports` new-buy candidate sourcing scaffold from `top_gainers`-only to a Toss-parity preset fan-in, wiring `consecutive_gainers` (full parity) first with `top_gainers` retained as fallback.

### Preset source
- `consecutive_gainers` (full Toss parity) via new public `load_consecutive_gainers_from_snapshots`.
- `top_gainers` kept as fallback (KR no-preset-rows) and for US (unchanged).

### Stale/partial handling
- Per-candidate `data_state` derived from each row's `_screener_snapshot_state` (not universe-wide).
- Missing partition (`None`) / no qualifiers (`[]`) → fall back to top_gainers.

### evidence_snapshot lineage example
```json
{"candidate_source_preset": "consecutive_gainers", "candidate_data_state": "fresh", "candidate_toss_parity_status": "full"}
```

### Tests
- `tests/services/invest_view_model/test_consecutive_gainers_public_loader.py`
- `tests/services/screener_evidence/test_builder.py`
- `tests/services/action_report/test_candidate_universe_collector_evidence.py`
- `ruff check` + `ruff format --check` clean.

### No-side-effect boundary
No broker/order/watch/order-intent/trade-journal mutation. No migration. No scheduler. KR-only; US/crypto paths unchanged. Read-only DB.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# PR2 — Add double_buy + high_yield_value sources + cross-preset merge

**Branch:** new branch off latest `origin/main` AFTER PR1 merges (per CLAUDE.md follow-up rule). `git fetch --prune origin && git switch -c rob-363-pr2 origin/main`.
**Outcome:** All three full-parity presets feed the KR pool; duplicate symbols merge with per-source reasons preserved; per-source freshness aggregated honestly.

### Task 2.1: Builder per-preset scoring/reasons for double_buy + high_yield_value

**Files:**
- Modify: `app/services/screener_evidence/builder.py`
- Test: `tests/services/screener_evidence/test_builder.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/screener_evidence/test_builder.py`:

```python
def test_double_buy_preset_reasons():
    from app.services.screener_evidence.builder import build_candidate_evidence

    rows = [
        {
            "symbol": "000660",
            "name": "SK하이닉스",
            "source": "kis",
            "change_rate": 1.0,
            "latest_close": 180000,
            "volume": 500000,
            "foreign_net": 1_000_000,
            "institution_net": 500_000,
            "foreign_consecutive_buy_days": 3,
        }
    ]
    out = build_candidate_evidence(market="kr", preset="double_buy", rows=rows)
    assert out[0].source_preset == "double_buy"
    assert any("쌍끌이" in r or "외국인" in r for r in out[0].reasons)


def test_high_yield_value_preset_reasons_and_score_label():
    from app.services.screener_evidence.builder import build_candidate_evidence

    rows = [
        {
            "symbol": "005490",
            "name": "POSCO홀딩스",
            "source": "kis",
            "change_rate": 0.5,
            "latest_close": 400000,
            "volume": 100000,
            "roe": 18.0,
            "per": 6.5,
            "pbr": 0.8,
        }
    ]
    out = build_candidate_evidence(
        market="kr", preset="high_yield_value", rows=rows
    )
    assert out[0].source_preset == "high_yield_value"
    assert "ROE" in out[0].score_label or "PER" in out[0].score_label
    assert any("저평가" in r or "ROE" in r for r in out[0].reasons)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py -k "double_buy or high_yield" -v`
Expected: FAIL — both presets currently hit the `else` momentum branch with the generic `단기 상승 모멘텀 후보` reason; no `쌍끌이`/`저평가` reason and no ROE/PER score label.

- [ ] **Step 3: Add preset branches in the builder**

In `app/services/screener_evidence/builder.py`, add reason constants near the existing ones (after line 14):

```python
_DOUBLE_BUY_REASON = "외국인·기관 쌍끌이 매수"
_HIGH_YIELD_VALUE_REASON = "고수익 저평가 (ROE 15%↑·PER 0~10)"
```

Insert two new branches in `build_candidate_evidence` BEFORE the final `else` (i.e. before line 82 `else:  # crypto_momentum + equity top_gainers`):

```python
        elif preset == "double_buy":
            score = scoring.momentum_score(change_rate)
            score_label = f"{change_rate:+.2f}%" if change_rate is not None else "-"
            reasons = [_DOUBLE_BUY_REASON]
            fcd = row.get("foreign_consecutive_buy_days")
            if isinstance(fcd, int) and fcd >= 2:
                reasons.append(f"외국인 {fcd}일 연속 순매수")
            volume_value = _to_float(row.get("daily_volume") or row.get("volume"))
        elif preset == "high_yield_value":
            roe = _to_float(row.get("roe"))
            per = _to_float(row.get("per"))
            score = scoring.high_yield_value_score(roe, per)
            roe_part = f"ROE {roe:.1f}%" if roe is not None else "ROE -"
            per_part = f"PER {per:.1f}" if per is not None else "PER -"
            score_label = f"{roe_part} · {per_part}"
            reasons = [_HIGH_YIELD_VALUE_REASON]
            volume_value = _to_float(row.get("daily_volume") or row.get("volume"))
```

- [ ] **Step 4: Add the `high_yield_value_score` curve**

In `app/services/screener_evidence/scoring.py`, append:

```python
def high_yield_value_score(roe: float | None, per: float | None) -> float:
    """ROE-led value score (ROB-363). Higher ROE and lower PER → higher score.

    Both qualify under the preset (ROE>=15, 0<PER<=10) so both contribute:
    ROE 15 → +0, ROE 35 → +5 (capped); PER 10 → +0, PER 0 → +5. ``None`` parts
    contribute 0. Result clamped 0–10."""
    roe_part = 0.0 if roe is None else clamp((roe - 15.0) / 4.0, 0.0, 5.0)
    per_part = 0.0 if per is None else clamp((10.0 - per) / 2.0, 0.0, 5.0)
    return clamp(roe_part + per_part)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py -v`
Expected: PASS (all builder tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/screener_evidence/builder.py app/services/screener_evidence/scoring.py tests/services/screener_evidence/test_builder.py
git commit -m "feat(ROB-363): builder scoring/reasons for double_buy + high_yield_value presets

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 2.2: Wire double_buy + high_yield_value loaders + reason-merging dedupe

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

Append to the collector test file:

```python
@pytest.mark.asyncio
async def test_kr_collector_merges_duplicate_symbol_across_presets(db_session):
    """ROB-363 — a symbol surfaced by two presets becomes ONE candidate whose
    reasons union both presets' provenance."""
    from sqlalchemy import text

    from app.services.action_report.snapshot_backed.collectors import (
        candidate_universe as cu,
    )

    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()

    async def fake_consecutive(session, *, market, limit):
        return [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "source": "kis",
                "change_rate": 2.0,
                "close": 70000,
                "consecutive_up_days": 6,
                "volume": 1,
                "_screener_snapshot_state": "fresh",
            }
        ]

    async def fake_high_yield(session, *, market, limit, today_market_date=None):
        return [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "source": "kis",
                "change_rate": 2.0,
                "latest_close": 70000,
                "roe": 20.0,
                "per": 7.0,
                "volume": 1,
                "_screener_snapshot_state": "fresh",
            }
        ]

    async def fake_double_buy(session, *, market, limit):
        return None

    monkey = {
        "load_consecutive_gainers_from_snapshots": fake_consecutive,
        "load_double_buy_from_snapshots": fake_double_buy,
        "load_high_yield_value_from_snapshots": fake_high_yield,
    }
    # patch the screener_service symbols the collector imports
    import app.services.invest_view_model.screener_service as ss
    import app.services.invest_view_model.high_yield_value_screener as hy
    import app.services.invest_view_model.double_buy_screener as dbb

    orig = {}
    orig["ss"] = ss.load_consecutive_gainers_from_snapshots
    orig["hy"] = hy.load_high_yield_value_from_snapshots
    orig["dbb"] = dbb.load_double_buy_from_snapshots
    ss.load_consecutive_gainers_from_snapshots = monkey[
        "load_consecutive_gainers_from_snapshots"
    ]
    hy.load_high_yield_value_from_snapshots = monkey[
        "load_high_yield_value_from_snapshots"
    ]
    dbb.load_double_buy_from_snapshots = monkey["load_double_buy_from_snapshots"]
    try:
        collector = cu.CandidateUniverseSnapshotCollector(db_session)
        results = await collector.collect(
            CollectorRequest(
                market="kr", account_scope=None, symbols=[], policy_snapshot={}
            )
        )
    finally:
        ss.load_consecutive_gainers_from_snapshots = orig["ss"]
        hy.load_high_yield_value_from_snapshots = orig["hy"]
        dbb.load_double_buy_from_snapshots = orig["dbb"]

    payload = results[0].payload_json
    rows_005930 = [c for c in payload["candidates"] if c["symbol"] == "005930"]
    assert len(rows_005930) == 1, "duplicate symbol must merge to one candidate"
    merged = rows_005930[0]
    reason_text = " ".join(merged["reasons"])
    assert "연속 상승" in reason_text  # from consecutive_gainers
    assert "저평가" in reason_text or "ROE" in reason_text  # from high_yield_value
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py::test_kr_collector_merges_duplicate_symbol_across_presets -v`
Expected: FAIL — PR1's `_KR_PRESET_LOADERS` has only `consecutive_gainers`; `high_yield_value` is not consulted and reasons are not merged.

- [ ] **Step 3: Extend the loader list + import the other two loaders**

In `candidate_universe.py` `_collect_kr_presets`, replace the single-loader block. Import all three at the top of the method (module-relative so the test's monkeypatch on each module's attribute is honored):

```python
        from app.services.invest_view_model import (
            double_buy_screener,
            high_yield_value_screener,
            screener_service,
        )

        pool_limit = max(limit * 3, limit + 20)
        loaders = [
            (
                "consecutive_gainers",
                screener_service.load_consecutive_gainers_from_snapshots,
            ),
            ("double_buy", double_buy_screener.load_double_buy_from_snapshots),
            (
                "high_yield_value",
                high_yield_value_screener.load_high_yield_value_from_snapshots,
            ),
        ]
```

Replace `_gainers_row_to_input` usage with a preset-aware normalizer. Add this helper (after `_gainers_row_to_input`):

```python
def _preset_row_to_input(preset: str, row: dict[str, Any]) -> dict[str, Any]:
    """Normalize any KR Toss-parity loader row into builder input, carrying the
    fundamental fields each preset's builder branch needs (ROB-363)."""
    base = {
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "source": row.get("source") or "kis",
        "change_rate": row.get("change_rate"),
        "close": row.get("close") or row.get("latest_close"),
        "latest_close": row.get("latest_close") or row.get("close"),
        "volume": row.get("volume") or row.get("daily_volume"),
        "consecutive_up_days": row.get("consecutive_up_days"),
        "foreign_consecutive_buy_days": row.get("foreign_consecutive_buy_days"),
        "roe": row.get("roe"),
        "per": row.get("per"),
        "pbr": row.get("pbr"),
    }
    return base
```

In the loop, build with the preset-aware normalizer and call the merge variant. Replace the loop body's build line with `rows=[_preset_row_to_input(preset_id, r) for r in rows]`. Pass `loaders` through a uniform call — note `high_yield_value` has an extra `today_market_date` kwarg with a default, so a positional/`limit` call works for all three.

- [ ] **Step 4: Replace `_dedupe_evidence` with a reason-merging merge**

Replace the PR1 `_dedupe_evidence` helper with a merge that unions reasons and keeps the first (highest-priority by insertion order; final priority sort is PR3) occurrence's scalar fields:

```python
def _merge_evidence(
    evidence: list[CandidateEvidence], *, key: Callable[[CandidateEvidence], Hashable]
) -> list[CandidateEvidence]:
    """Merge duplicate-symbol CandidateEvidence across presets (ROB-363).

    Keeps the first occurrence's scalar fields (symbol/score/source_preset) and
    UNIONS ``reasons`` + ``risk_flags`` (order-preserving, deduped) from every
    preset that surfaced the symbol, so per-source provenance is preserved.
    Order-preserving on first occurrence."""
    import dataclasses

    order: list[Hashable] = []
    merged: dict[Hashable, CandidateEvidence] = {}
    for ev in evidence:
        k = key(ev)
        if k not in merged:
            merged[k] = ev
            order.append(k)
            continue
        prev = merged[k]
        reasons = list(prev.reasons)
        for r in ev.reasons:
            if r not in reasons:
                reasons.append(r)
        flags = list(prev.risk_flags)
        for f in ev.risk_flags:
            if f not in flags:
                flags.append(f)
        merged[k] = dataclasses.replace(prev, reasons=reasons, risk_flags=flags)
    return [merged[k] for k in order]
```

Update `_collect_kr_presets` to call `_merge_evidence(evidence, key=lambda e: to_db_symbol(e.symbol))` instead of `_dedupe_evidence`, and update `per_state` so a fresh state from ANY preset wins over stale (a symbol that is fresh in one source is fresh):

```python
            for ev, src_row in zip(built, rows, strict=False):
                k = to_db_symbol(ev.symbol)
                state = src_row.get("_screener_snapshot_state") or "fresh"
                if per_state.get(k) != "fresh":
                    per_state[k] = state
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -v`
Expected: PASS (merge test + all PR1 tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-363): fan-in double_buy + high_yield_value, merge duplicate symbols with reason union

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 2.3: PR2 verification gate

- [ ] **Step 1: Lint + format + touched suites**

Run:
```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
uv run pytest tests/services/screener_evidence/ tests/services/action_report/test_candidate_universe_collector_evidence.py -v
```
Expected: clean + all PASS.

- [ ] **Step 2: Push + PR** (same `gh pr create --base main` pattern as PR1, title `feat(ROB-363): KR double_buy + high_yield_value sourcing + merge (PR2/3)`, body documenting all three preset sources, merge behaviour, per-source freshness, evidence lineage, no-mutation boundary).

---

# PR3 — Deterministic priority + per-candidate stale demotion + regression coverage

**Branch:** new branch off latest `origin/main` AFTER PR2 merges. `git fetch --prune origin && git switch -c rob-363-pr3 origin/main`.
**Outcome:** Cross-source deterministic priority (full+fresh+score first); per-candidate stale candidates demoted to `watch_only` in `auto_emit` even when the universe is useful; comprehensive regression tests for the acceptance criteria.

### Task 3.1: Deterministic cross-source priority in the collector

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_kr_priority_full_fresh_outranks_partial_and_stale(db_session):
    """ROB-363 — deterministic priority: full+fresh+higher-score first. Internal
    pool wider than candidate_limit, then sliced."""
    from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
        _priority_sort_key,
    )
    from app.services.screener_evidence.models import CandidateEvidence

    def ev(symbol, preset, score):
        return CandidateEvidence(
            symbol=symbol, market="kr", name=symbol, score=score,
            score_label="", change_rate=None, price=None, volume_value=None,
            reasons=[], source="kis", risk_flags=[], source_preset=preset,
        )

    rows = [
        (ev("A", "consecutive_gainers", 6.0), "stale"),   # full but stale
        (ev("B", "high_yield_value", 5.0), "fresh"),       # full + fresh, lower score
        (ev("C", "high_yield_value", 9.0), "fresh"),       # full + fresh, top score
    ]
    ordered = sorted(rows, key=lambda pair: _priority_sort_key(pair[0], pair[1]))
    assert [p[0].symbol for p in ordered] == ["C", "B", "A"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py::test_kr_priority_full_fresh_outranks_partial_and_stale -v`
Expected: FAIL — `_priority_sort_key` does not exist.

- [ ] **Step 3: Add `_priority_sort_key` and apply it in `_collect_kr_presets`**

Add the helper (after `_merge_evidence`):

```python
_PARITY_RANK = {"full": 0, "partial": 1, "mismatch": 2, "not_toss_parity": 3}
_FRESHNESS_RANK = {"fresh": 0, "stale": 1, "missing": 2}


def _priority_sort_key(
    ev: CandidateEvidence, data_state: str
) -> tuple[int, int, float, str]:
    """Deterministic candidate priority (ROB-363): full Toss parity first, then
    fresh, then higher score, then symbol (stable tiebreak). Lower tuple sorts
    first; ``-score`` makes higher score rank earlier."""
    parity = _toss_parity_status(ev.source_preset or "top_gainers", "kr")
    return (
        _PARITY_RANK.get(parity, 3),
        _FRESHNESS_RANK.get(data_state, 2),
        -ev.score,
        ev.symbol,
    )
```

In `_collect_kr_presets`, after `_merge_evidence`, sort by the priority key and THEN slice to `limit` (widen the pool first — `pool_limit` already over-fetches per preset):

```python
        evidence = _merge_evidence(evidence, key=lambda e: to_db_symbol(e.symbol))
        evidence.sort(
            key=lambda e: _priority_sort_key(e, per_state.get(to_db_symbol(e.symbol), "fresh"))
        )
        universe_count = len(evidence)
        evidence = evidence[:limit]
```

Pass the pre-slice `universe_count` into `_build_preset_candidate_result` (so `capped` reflects the true pre-cap pool). Update the call accordingly.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-363): deterministic cross-preset priority (full+fresh+score) then cap

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 3.2: Per-candidate stale demotion in auto_emit

**Files:**
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py` (`classify_candidate_symbol`, line 74)
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py` (call site line 358)
- Test: `tests/test_auto_emit_candidate_citation.py`

- [ ] **Step 1: Write the failing test**

Inspect `tests/test_auto_emit_candidate_citation.py` for its existing candidate-builder fixture/helper, then append a test asserting that a candidate whose own `data_state == "stale"` is emitted as `watch_only` with `reject_or_wait_reason == "screener_stale"` EVEN when `usefulness == "useful"`, while a sibling `fresh` candidate with an actionable quote stays `buy_review`. Use the same snapshot-construction helper the existing tests in that file use (do not invent a new harness — reuse the file's `_candidate_universe_snapshot(...)`-style builder; if the candidates list there does not accept per-row `data_state`, add `"data_state": "stale"` to the relevant candidate dict).

```python
@pytest.mark.asyncio
async def test_stale_candidate_demoted_even_when_universe_useful(...):
    # universe usefulness == "useful"; candidate STALE rows must NOT be buy_review.
    # ... build candidate_universe snapshot with two candidates:
    #     {"symbol": "AAA", ..., "data_state": "fresh"}  -> buy_review (actionable quote)
    #     {"symbol": "BBB", ..., "data_state": "stale"}  -> watch_only / screener_stale
    items = await emitter.build(...)
    by_symbol = {i.symbol: i for i in items}
    assert by_symbol["BBB"].evidence_snapshot["action_verdict"] == "watch_only"
    assert by_symbol["BBB"].evidence_snapshot["reject_or_wait_reason"] == "screener_stale"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py -k "stale_candidate_demoted" -v`
Expected: FAIL — stale candidate is currently `buy_review` because demotion only consults the universe-wide `universe_useful` flag.

- [ ] **Step 3: Add `candidate_fresh` to `classify_candidate_symbol`**

In `action_verdict.py`, change the signature and the universe-stale rule:

```python
def classify_candidate_symbol(
    quote: dict[str, Any] | None,
    *,
    universe_useful: bool,
    quote_snapshot_present: bool,
    candidate_fresh: bool = True,
) -> str:
    ...
    if not quote_snapshot_present:
        return "data_gap"
    if not _quote_is_actionable(quote):
        return "watch_only"
    if not universe_useful or not candidate_fresh:
        return "watch_only"
    return "buy_review"
```

Update the docstring rule 3 to: `3. quote actionable, universe stale OR this candidate stale -> watch_only`.

- [ ] **Step 4: Pass per-candidate freshness at the call site**

In `auto_emit.py` (line 358), pass the per-candidate flag derived from the candidate dict's `data_state`:

```python
            verdict = classify_candidate_symbol(
                quote,
                universe_useful=candidate_actionable,
                quote_snapshot_present=symbol_pair is not None,
                candidate_fresh=(cand.get("data_state") or "fresh") == "fresh",
            )
```

Also ensure the `watch_only` reason picks `screener_stale` when the candidate is stale: the existing branch (lines 366-371) already defaults to `screener_stale` when the quote IS actionable, so a stale-but-liquid candidate correctly reports `screener_stale`. No change needed there.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py -v`
Expected: PASS (new test + all existing auto_emit citation tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/action_verdict.py app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_candidate_citation.py
git commit -m "feat(ROB-363): demote per-candidate stale screener rows to watch_only

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 3.3: Acceptance-criteria regression tests

**Files:**
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Add the remaining acceptance-criteria tests**

Add tests covering the criteria not yet explicitly asserted:

1. `test_full_parity_status_validated_in_regression` — assert a `consecutive_gainers`/`high_yield_value` candidate yields `toss_parity_status == "full"` (criterion: full parity validated).
2. `test_stale_only_preset_not_overstated` — when all loaders return only `stale` rows, payload `usefulness == "stale_only"` and `freshness_status != "fresh"` (criterion: stale demotion).
3. `test_top_gainers_only_when_no_preset_rows` — when all three loaders return `None`/`[]`, the collector falls back to `top_gainers` with `toss_parity_status == "not_toss_parity"` and that path is NOT presented as full parity (criterion: not_toss_parity honesty).
4. `test_pool_wider_than_limit_then_capped` — feed >`candidate_limit` qualifiers across presets; assert `universe_count > candidate_limit`, `capped is True`, and `len(candidates) == candidate_limit` (criterion: wider pool → priority → cap).

Use the monkeypatch-the-loaders pattern from Task 2.2 for tests needing controlled rows.

- [ ] **Step 2: Run the full touched-area suite**

Run:
```bash
uv run pytest \
  tests/services/screener_evidence/ \
  tests/services/action_report/test_candidate_universe_collector_evidence.py \
  tests/test_auto_emit_candidate_citation.py \
  tests/services/invest_view_model/test_consecutive_gainers_public_loader.py -v
```
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "test(ROB-363): acceptance-criteria regression coverage for KR Toss-parity sourcing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 3.4: PR3 final verification + full-CI gate

- [ ] **Step 1: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean.

- [ ] **Step 2: Full report + screener suites (catch regressions in the wider blast radius)**

Run:
```bash
uv run pytest tests/services/action_report/ tests/services/screener_evidence/ tests/services/invest_view_model/ -p no:cacheprovider -q
```
Expected: PASS (note any pre-existing local-only failures per memory `Local full-sweep test noise` and confirm they are unrelated to this diff).

- [ ] **Step 3: No-mutation boundary grep**

Run:
```bash
grep -rn "submit_order\|place_order\|cancel_order\|modify_order\|preview_order\|order_intent" \
  app/services/action_report/snapshot_backed/collectors/candidate_universe.py \
  app/services/action_report/snapshot_backed/auto_emit.py \
  app/services/action_report/snapshot_backed/action_verdict.py \
  app/services/screener_evidence/ \
  || echo "NO MUTATION CALLS — boundary clean"
```
Expected: `NO MUTATION CALLS — boundary clean`.

- [ ] **Step 4: Push + PR + confirm Test workflow green BEFORE merge**

```bash
git push -u origin rob-363-pr3
gh pr create --base main --title "feat(ROB-363): KR priority + per-candidate stale demotion + regression (PR3/3)" --body "..."
```

Per memory `Pre-merge full-CI gate`: do NOT `gh pr merge --auto` blindly. After pushing, watch the Test workflow to green (`gh pr checks <num> --watch`) and confirm `ruff` + import guards pass in CI before merging each PR.

---

## Self-Review

**Spec coverage (acceptance criteria → task):**
- KR source uses ≥1 of the three presets → PR1 Task 1.3 (consecutive_gainers), PR2 Task 2.2 (all three). ✓
- `evidence_snapshot.candidate_source_preset` has real preset id → flows via `auto_emit.py:167` from `source_preset`; asserted PR1 Task 1.3. ✓
- `candidate_toss_parity_status=full` validated → PR3 Task 3.3 test 1. ✓
- stale/missing not overstated → PR1 per-candidate `data_state`; PR3 Task 3.2 demotion; Task 3.3 test 2. ✓
- partial presets keep `partial` + missing note → `_toss_parity_status` derives `partial` from catalog automatically (cheap_value/steady_dividend `parityStatus=_PARTIAL`); not wired as a source in this plan (out of the three full presets) but parity flows if added. NOTE: the three sourced presets are all `full`; partial presets remain available via the same machinery. Acceptance criterion is conditional ("when used"), so satisfied by the parity-derivation path. ✓
- top_gainers fallback only / not_toss_parity not shown as full → PR1 Task 1.3 fallback; PR3 Task 3.3 test 3. ✓
- duplicate symbols merge w/ reasons preserved → PR2 Task 2.2. ✓
- wider pool → deterministic priority → cap → PR3 Task 3.1 + Task 3.3 test 4. ✓
- focused tests cover sourcing/demotion/dedupe/priority/lineage → PRs 1-3 tests. ✓
- no-mutation boundary in PR desc + grep → PR1 Task 1.4 / PR3 Task 3.4. ✓

**Placeholder scan:** Task 3.2 Step 1 references the existing file's candidate-builder helper rather than reproducing it — this is intentional (the helper must be reused, not duplicated; reproducing it risks drift). The executor must read `tests/test_auto_emit_candidate_citation.py` first. All code-bearing steps include full code.

**Type consistency:** loader contract `list[dict] | None` uniform across all three (Task 1.1 wrapper aligns consecutive_gainers). `_priority_sort_key(ev, data_state)` signature consistent between Task 3.1 definition and call site. `classify_candidate_symbol(..., candidate_fresh=True)` default keeps existing callers valid. `CandidateEvidence` fields match `models.py`. `_merge_evidence` replaces `_dedupe_evidence` (PR1→PR2) — executor must remove the PR1 helper, not leave both.

**Open verification deferred to operator:** local advisory-only KR report render smoke after merge (Suggested verification), gated on operator approval of any production-data read. Not in automated scope.
