# ROB-918: kr-preopen New-Candidate Shadow Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject a read-only, advisory-only "new candidate" observation section (consecutive-gainers + theme leaders + double-buy) into kr-preopen `trading_decision_sessions.market_brief`, plus a read-only shadow-aggregation script — with a hard guarantee that no `trading_decision_proposals` rows are ever created for these candidates.

**Architecture:** A new pure-read service module (`app/services/research_run_new_candidates.py`) queries three existing snapshot tables (`invest_screener_snapshots`, `invest_theme_event_snapshots`, `investor_flow_snapshots`) plus `kr_candles_1d` (crash-guard index proxy), and returns a plain dict. `research_run_decision_session_service.create_decision_session_from_research_run` calls it once and merges the result into the `market_brief` JSONB it already assembles (no new proposal path, no migration). A separate read-only CLI script later joins recorded candidates against `kr_candles_1d` to compute D+1 % moves for the 2-week shadow review.

**Tech Stack:** Python 3.13, SQLAlchemy async, existing `resolve_healthy_partition` / `InvestMomentumEventSnapshotsRepository` / `load_double_buy_from_snapshots` helpers.

## Global Constraints

- MUST NOT insert any `trading_decision_proposals` row for these candidates (hard requirement, proven by test).
- MUST NOT change any TaskIQ/cron schedule.
- MUST NOT write to the DB from the shadow script (SELECT only).
- Migration must be 0 (JSONB reuse on `trading_decision_sessions.market_brief`).
- Only activates for `research_run.market_scope == "kr"` and `research_run.stage == "preopen"`; all other market/stage combos are a no-op (return `None`, key omitted from `market_brief`).
- `research_run_decision_session_service.py` is guarded by `tests/test_research_run_decision_session_service_safety.py::test_orchestrator_service_forbidden_imports` — the new module must not import anything under `app.services.brokers`, `app.services.kis*`, `app.services.upbit*`, `app.services.market_data`, `app.tasks`, etc. (see forbidden-prefix list in that test).
- Filters for consecutive_gainers candidates: market_cap >= 200_000_000_000 KRW AND trade_value_est >= 20_000_000_000 KRW (trade_value has no direct column on `invest_screener_snapshots`; approximate as `daily_volume * latest_close`, documented inline).
- Crash guard: KODEX200 (symbol `069500`, venue `KRX`) latest two `kr_candles_1d` closes; gap_pct = (latest.close - prior.close) / prior.close * 100. `<= -3.0` → `market_state = "crash_warning"`, else `"normal"`. Insufficient data → `"unknown"`. Label only — never blocks candidate generation.
- Every candidate record carries `advisory_only: true`, `baseline_date`, `baseline_close`, and `outcome: {"d1_close_pct": null}` for later shadow-script fill-in.

---

## Task 1: `research_run_new_candidates` service module

**Files:**
- Create: `app/services/research_run_new_candidates.py`
- Test: `tests/services/test_research_run_new_candidates.py`

**Interfaces:**
- Consumes: `AsyncSession`; `app.services.invest_screener_snapshots.partition_health.resolve_healthy_partition`; `app.services.invest_momentum_events.repository.InvestMomentumEventSnapshotsRepository`; `app.services.invest_view_model.double_buy_screener.load_double_buy_from_snapshots`; `app.services.daily_candles.repository.DailyCandlesRepository`, `MarketKey`; `app.models.invest_screener_snapshot.InvestScreenerSnapshot`; `app.models.market_valuation_snapshot.MarketValuationSnapshot`; `app.models.kr_symbol_universe.KRSymbolUniverse`.
- Produces: `async def build_new_candidate_section(db: AsyncSession, *, market_scope: str, stage: str, top_n: int = 10) -> dict[str, Any] | None`. Returns `None` when `(market_scope, stage) != ("kr", "preopen")`. Otherwise returns:
  ```python
  {
      "advisory_only": True,
      "market_state": "normal" | "crash_warning" | "unknown",
      "market_state_detail": {...},
      "consecutive_gainers": [ {...candidate...}, ... ],
      "theme_leaders": [ {...candidate...}, ... ],
      "double_buy": [ {...candidate...}, ... ],
      "omitted_sections": [ {"section": str, "reason": str}, ... ],
  }
  ```
  Each candidate dict: `symbol`, `name`, `reason` (`"consecutive_gainers" | "theme_leader" | "double_buy"`), `advisory_only: True`, `selection_rationale` (Korean string), `metrics` (dict, keys vary by reason), `baseline_date` (ISO date str | None), `baseline_close` (float | None), `outcome: {"d1_close_pct": None}`.

- [ ] Write failing tests in `tests/services/test_research_run_new_candidates.py`:
  - `test_returns_none_for_non_kr_preopen` — market_scope="us" or stage="intraday" → `None`.
  - `test_crash_warning_label_when_index_gap_below_threshold` — seed two `kr_candles_1d` rows for `069500`/`KRX` with a -4% gap → `market_state == "crash_warning"`.
  - `test_normal_market_state_when_index_gap_small` — seed a +0.5% gap → `market_state == "normal"`.
  - `test_unknown_market_state_when_index_candles_missing` — no `069500` rows → `market_state == "unknown"`, no exception.
  - `test_consecutive_gainers_filters_by_market_cap_and_trade_value` — seed `InvestScreenerSnapshot` rows (one above both thresholds, one below market_cap, one below trade_value-via-volume) + matching `MarketValuationSnapshot` rows; assert only the qualifying symbol appears, with `metrics.market_cap`/`metrics.trade_value_est`/`metrics.change_rate`/`metrics.consecutive_up_days` populated and `baseline_close == latest_close`.
  - `test_consecutive_gainers_omitted_when_snapshot_missing` — no `InvestScreenerSnapshot` rows for market='kr' → `consecutive_gainers == []` and an `omitted_sections` entry with `section="consecutive_gainers"`.
  - `test_theme_leaders_flattened_from_leader_symbols` — seed `InvestThemeEventSnapshot` (event_kind='theme') + `InvestThemeEventSnapshotStock` rows; assert flattened per-symbol candidates with `reason="theme_leader"`, `metrics.theme_name`, `baseline_close` from the stock row's `price`.
  - `test_theme_leaders_omitted_when_no_snapshots` — empty table → `theme_leaders == []` + omitted entry.
  - `test_double_buy_delegates_to_existing_loader` — monkeypatch `load_double_buy_from_snapshots` to return a canned `_SnapshotLoadResult`; assert mapping into `reason="double_buy"` candidates.
  - `test_double_buy_omitted_when_loader_returns_none` — monkeypatch loader to return `None` → `double_buy == []` + omitted entry.
  - Run: `uv run pytest tests/services/test_research_run_new_candidates.py -v` — expect all FAIL (`ModuleNotFoundError`).

- [ ] Implement `app/services/research_run_new_candidates.py` per the interface above. Reuse `resolve_healthy_partition` for the screener/valuation partitions (mirrors `screener_service.py`/`double_buy_screener.py` patterns already in the codebase). Reuse `InvestMomentumEventSnapshotsRepository.list_theme_events(event_kind="theme", limit=top_n)` + `list_theme_event_stocks(...)`. Reuse `load_double_buy_from_snapshots(db, market="kr", limit=top_n)` unmodified. Use `DailyCandlesRepository(session=db).fetch_recent(market=MarketKey.KR, symbol="069500", partition="KRX", count=2)` for the crash guard (rows come back newest-first per the existing KR/US branch of `fetch_recent`).

- [ ] Run: `uv run pytest tests/services/test_research_run_new_candidates.py -v` — expect PASS.

- [ ] Commit: `feat(ROB-918): add kr-preopen new-candidate shadow service module`

## Task 2: Wire into `create_decision_session_from_research_run`

**Files:**
- Modify: `app/services/research_run_decision_session_service.py:574-600` (the `session.market_brief = _json_safe({...})` block)
- Test: `tests/test_research_run_decision_session_service.py` (extend), new `tests/test_research_run_decision_session_service_new_candidates.py`

**Interfaces:**
- Consumes: `research_run_new_candidates.build_new_candidate_section` (Task 1).
- Produces: `market_brief["new_candidates"]` key populated for kr/preopen sessions; absent (or explicitly omitted) for other market/stage combos.

- [ ] Write failing test in new file `tests/test_research_run_decision_session_service_new_candidates.py`:
  ```python
  @pytest.mark.unit
  async def test_kr_preopen_session_gets_new_candidates_section_without_proposals(
      db_session, user, research_run_factory, research_run_candidate_factory,
  ):
      run = await research_run_factory(db_session, user_id=user.id, market_scope="kr", stage="preopen")
      await research_run_candidate_factory(db_session, research_run_id=run.id, symbol="005930")
      snapshot = LiveRefreshSnapshot(refreshed_at=datetime.now(UTC), quote_by_symbol={}, warnings=[])
      request = ResearchRunDecisionSessionRequest(selector=ResearchRunSelector(run_uuid=run.run_uuid))

      before_count = (await db_session.execute(select(func.count(TradingDecisionProposal.id)))).scalar_one()
      result = await create_decision_session_from_research_run(
          db_session, user_id=user.id, research_run=run, snapshot=snapshot, request=request,
      )
      after_count = (await db_session.execute(select(func.count(TradingDecisionProposal.id)))).scalar_one()

      assert "new_candidates" in result.session.market_brief
      assert result.session.market_brief["new_candidates"]["advisory_only"] is True
      # Hard safety requirement: the new-candidate section must add zero proposal rows.
      assert after_count - before_count == 1  # only the one seeded research-run candidate
  ```
  Run: `uv run pytest tests/test_research_run_decision_session_service_new_candidates.py -v` — expect FAIL (`KeyError: 'new_candidates'`).

- [ ] Implement: in `research_run_decision_session_service.py`, import `research_run_new_candidates`, call `new_candidates = await research_run_new_candidates.build_new_candidate_section(db, market_scope=research_run.market_scope, stage=research_run.stage)` before building the `market_brief` dict, and add `"new_candidates": new_candidates` as a key (may be `None` for non-kr-preopen — keep the key present but `None` so the shape is predictable, or omit if `None`; prefer always-present key for a stable Hermes-side contract).

- [ ] Run: `uv run pytest tests/test_research_run_decision_session_service_new_candidates.py tests/test_research_run_decision_session_service.py tests/test_research_run_decision_session_service_safety.py -v` — expect PASS (including the forbidden-imports guard).

- [ ] Commit: `feat(ROB-918): inject new-candidate section into kr-preopen market_brief`

## Task 3: Read-only shadow aggregation script

**Files:**
- Create: `scripts/shadow_new_candidates_report.py`
- Test: `tests/test_shadow_new_candidates_report.py`

**Interfaces:**
- Consumes: `trading_decision_sessions.market_brief->'new_candidates'` (Task 2 shape); `kr_candles_1d` via `DailyCandlesRepository`.
- Produces: `async def build_shadow_report(session: AsyncSession, *, since: date, market_scope: str = "kr") -> ShadowReportRow list` (testable core, no argparse/DB-write), plus a thin CLI `main()` mirroring `scripts/diagnose_invest_screener_snapshots.py`'s shape.

- [ ] Write failing test `tests/test_shadow_new_candidates_report.py`: seed one `TradingDecisionSession` (source_profile='research_run', market_scope='kr') with a `market_brief["new_candidates"]["consecutive_gainers"]` entry carrying `baseline_date`/`baseline_close`, seed two `kr_candles_1d` rows (baseline day + D+1) for that symbol, call `build_shadow_report`, assert the returned row's `d1_close_pct` matches the expected computed percentage and that no writes occurred (assert `db_session` has no pending changes / re-query row counts unchanged for `kr_candles_1d`).

- [ ] Implement `scripts/shadow_new_candidates_report.py`: query recent `TradingDecisionSession` rows with `market_brief['new_candidates']` present, flatten all three candidate lists, for each `(symbol, baseline_date)` find the next `kr_candles_1d` row strictly after `baseline_date` (`ORDER BY time ASC LIMIT 1`), compute `pct = (close - baseline_close) / baseline_close * 100`, print a table (recovered-rate / false-positive-rate style summary) — no DB writes anywhere in the file.

- [ ] Run: `uv run pytest tests/test_shadow_new_candidates_report.py -v` — expect PASS.

- [ ] Commit: `feat(ROB-918): add read-only 2-week shadow aggregation script`

## Task 4: Runbook + PR

- [ ] Create `docs/runbooks/kr-preopen-new-candidates-shadow.md` documenting the shadow rollout, filters, crash-guard, and how to run the aggregation script.
- [ ] Run full suite: `uv run pytest tests/services/test_research_run_new_candidates.py tests/test_research_run_decision_session_service*.py tests/test_shadow_new_candidates_report.py -v` and `make lint`.
- [ ] `gh pr create --base main` with proposal-count-unchanged evidence in the PR body.
