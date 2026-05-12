# ROB-205 plan: KR investor_flow_snapshots backfill and recurring freshness

Planner: planner
Date: 2026-05-12 08:39 KST
Workspace: `/Users/mgh3326/worktrees/auto_trader/rob-205-investor-flow-snapshots`
Branch: `feature/rob-205-investor-flow-snapshots`
Linear: ROB-205
Actual model/runtime note: Claude Code CLI is installed (`2.1.138`) but not authenticated, so this plan was produced by the Hermes planner agent directly after repo reconnaissance.

## 1. Goal and acceptance criteria

ROB-205 should populate and operationalize durable KR `investor_flow_snapshots` so `/invest/api/investor-flow` and `/invest/api/coverage` no longer depend on live request-path scraping and can report KR investor-flow coverage as `partial`/`fresh` after an approval-gated backfill.

Acceptance criteria from Linear:

1. Dry-run backfill reports expected row count/date/symbol scope and idempotency.
2. Tests cover model/service freshness and coverage integration.
3. After approved deploy/backfill, `/invest/coverage` reports `investor_flow` as partial/fresh rather than missing for supported KR scope.
4. No production writes, scheduler activation, broker/order/watch/order-intent side effects without explicit operator approval.

## 2. Current repo state found during K0

Already present from ROB-191/201/203/204 on current `github/main`:

- Model and migration:
  - `app/models/investor_flow_snapshot.py`
  - `alembic/versions/9f1a2b3c4d5e_add_investor_flow_snapshots.py`
- Repository/read model:
  - `app/services/investor_flow_snapshots/repository.py`
  - `app/services/invest_view_model/investor_flow_service.py`
  - `app/schemas/investor_flow.py`
  - `app/routers/invest_api.py` exposes `GET /invest/api/investor-flow`.
- Coverage:
  - `app/services/invest_coverage_service.py` has `_investor_flow_surfaces()` and source candidate metadata.
  - `investor_flow` actionability already maps to queue `investor-flow-ingestion` and includes `production_db_write_approval` plus `scheduler_activation_approval` for non-fresh states.
- Existing tests:
  - `tests/test_investor_flow_service.py`
  - `tests/test_investor_flow_snapshots_repository.py`
  - `tests/test_invest_coverage.py` includes investor-flow coverage/source-candidate checks.
  - `tests/test_naver_finance.py` covers `fetch_investor_trends()` parsing.

Missing for ROB-205:

- No reusable investor-flow snapshot builder/job/CLI equivalent to `app/jobs/invest_screener_snapshots.py` + `scripts/build_invest_screener_snapshots.py`.
- No dry-run approval packet with row count/date/symbol scope/idempotency metadata for `investor_flow_snapshots`.
- No TaskIQ/manual task wrapper for investor-flow freshness.
- No recurring activation artifact/gate documentation for the KR freshness schedule.

Baseline verification run in this worktree:

```bash
uv run --group test pytest tests/test_investor_flow_service.py tests/test_investor_flow_snapshots_repository.py -q
```

Result: `7 passed, 14 warnings in 12.16s`.

A first attempt without the `test` dependency group failed because `pytest` was not installed in the project venv; use `uv run --group test pytest ...` for targeted tests.

## 3. Implementation approach

Follow the ROB-204 dry-run-first pattern:

- Builder/service code constructs `InvestorFlowSnapshotUpsert` payloads in memory.
- Job runner returns counts, date distribution, sample payloads, warnings, and idempotency classification.
- `commit=False` is the default everywhere and must not write to DB.
- `commit=True` is the only DB-write path and is reserved for an explicit operator approval after dry-run evidence.
- Request-path endpoints stay read-only and must not call Naver/KIS/broker/network ingestion.

Primary source for K1: Naver Finance `fetch_investor_trends()` because it is already wired/tested and does not require KIS credentials. Preserve `source="naver_finance"` in all rows. Keep KIS as a future source only unless K1 can add it without credentials or live side effects.

## 4. Concrete file plan

### 4.1 Add investor-flow payload builder

Create:

- `app/services/investor_flow_snapshots/builder.py`

Responsibilities:

- Accept `symbols`, `days`, optional `today`, optional injected async `fetcher` for tests.
- For each KR symbol, call `app.services.naver_finance.fetch_investor_trends(symbol, days=days)` through an injectable boundary.
- Convert each returned daily trend row into `InvestorFlowSnapshotUpsert`:
  - `market="kr"`
  - `symbol` normalized by repository later, but keep canonical 6-digit input.
  - `snapshot_date`: parse row `date` (`YYYY-MM-DD`) to `datetime.date`.
  - `foreign_net`: `row["foreign_net"]`
  - `institution_net`: `row["institutional_net"]`
  - `individual_net`: if source row lacks it, derive `-(foreign_net + institution_net)` when both are present.
  - `source="naver_finance"`
  - `collected_at`: supplied once per run for deterministic tests.
- Compute consecutive-day flags over rows sorted newest to oldest:
  - `foreign_consecutive_buy_days` / `sell_days`
  - `institution_consecutive_buy_days` / `sell_days`
  - `individual_consecutive_buy_days` / `sell_days`
  - For each snapshot row, streak should be computed starting at that row and walking older rows until the sign changes/zero/missing.
- Compute same-date ranks within the current build payloads:
  - For each `snapshot_date`, rank positive `foreign_net` descending into `foreign_net_buy_rank`; negative ascending by value into `foreign_net_sell_rank`.
  - Same for `institution_net_buy_rank` and `institution_net_sell_rank`.
  - Leave rank as `None` when the payload set for that date does not include enough symbols or value is zero/missing.
- Return built payloads plus per-symbol warnings for empty/unparseable data.

Suggested shape:

```python
@dataclass(frozen=True)
class InvestorFlowBuildResult:
    payloads: list[InvestorFlowSnapshotUpsert]
    warnings: tuple[str, ...]

async def build_investor_flow_snapshots(...)-> InvestorFlowBuildResult: ...
```

### 4.2 Add dry-run-first job runner

Create:

- `app/jobs/investor_flow_snapshots.py`

Mirror `app/jobs/invest_screener_snapshots.py` style, but KR-only.

Suggested request/result fields:

```python
@dataclass(frozen=True)
class InvestorFlowSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    days: int = 20
    commit: bool = False
    today: dt.date | None = None

@dataclass(frozen=True)
class InvestorFlowSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_date_distribution: dict[str, int]
    idempotency: dict[str, int]
    samples: tuple[InvestorFlowSnapshotSample, ...]
    warnings: tuple[str, ...]
```

Required behavior:

- `_validate_market()` only allows `kr`; raise for `us`/`crypto`.
- `resolve_symbols()` and `resolve_active_universe()` read `KRSymbolUniverse.is_active == True`, ordered by symbol. Do not touch US/crypto.
- Dry-run (`commit=False`) must not call `repo.upsert()` or `session.commit()`.
- For dry-run and commit modes, compute idempotency before writing by checking existing rows matching `(market, symbol, snapshot_date, source)`:
  - `wouldInsert`
  - `wouldUpdate`
  - `duplicatePayloadKeys` if builder produced duplicate payload keys.
- For commit mode, upsert payloads via `InvestorFlowSnapshotsRepository` and commit per batch or once after all batches. Prefer per batch to bound transaction size, consistent with screener job.
- Bound sample output to 10 rows and redact/avoid secrets. Samples can include `market`, `symbol`, `snapshotDate`, `source`, `foreignNet`, `institutionNet`, `individualNet`, `doubleBuy`, `doubleSell`.

### 4.3 Add operator CLI

Create:

- `scripts/build_investor_flow_snapshots.py`

CLI defaults and gates:

- Required/default `--market kr`; reject non-KR.
- `--symbol` repeatable.
- `--limit` default 20 unless `--all`.
- `--all` mutually exclusive with `--symbol` and `--limit`.
- `--days` default 20; reject `days < 1` and cap to a safe max such as 60 unless K1 has a reason otherwise.
- `--batch-size`, `--concurrency` with safe defaults.
- `--commit` opt-in; no `--commit` means dry-run and prints `--dry-run: no rows written.`
- Print approval-packet-friendly summary:
  - market
  - symbols resolved
  - snapshots built
  - committed/dry-run
  - snapshot date distribution
  - idempotency counts
  - warnings
  - sample rows

Example commands to document in the script docstring:

```bash
# bounded dry-run approval packet for first 20 active KR symbols
uv run python -m scripts.build_investor_flow_snapshots --market kr --limit 20 --days 20

# full-universe dry-run approval packet
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100

# approved write only after operator approval
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100 --commit
```

### 4.4 Add TaskIQ manual task wrapper, not auto-scheduled yet

Create:

- `app/tasks/investor_flow_snapshot_tasks.py`

Add:

```python
@broker.task(task_name="build_investor_flow_snapshots")
async def build_investor_flow_snapshots(..., commit: bool = False) -> dict[str, Any]: ...
```

Then update:

- `app/tasks/__init__.py`

Import the task module so workers can invoke the task manually.

Important: do **not** attach a recurring `schedule=[...]` in K1 unless the Kanban task has explicit operator approval. Merely deploying a scheduled task would be scheduler activation. Instead:

- Keep the wrapper schedulable/manual but unscheduled by default.
- Include the proposed recurring schedule in the plan/Linear comment/approval packet, e.g. after KR close on weekdays (`20 16 * * 1-5`, `cron_offset="Asia/Seoul"`) or whichever operator prefers.
- If approval is granted in a later task, make a tiny scheduler-activation patch adding the schedule or enable it in the production scheduler configuration.

### 4.5 Coverage/read-model adjustments

Likely no major code change required because coverage already reads `InvestorFlowSnapshot`.

K1 should still verify/adjust if tests expose gaps:

- `app/services/invest_coverage_service.py`
  - Ensure `investor_flow` surface actionability remains `queue="investor-flow-ingestion"` with both approval gates when missing/stale/partial.
  - Ensure source-of-truth stays `investor_flow_snapshots`, not `naver_finance`.
  - Ensure source candidate for `naver_finance` reports local table rows only and never scrapes live data.
- `app/services/invest_view_model/investor_flow_service.py`
  - Keep request-path read-only; no ingestion fallback.

### 4.6 Optional but useful docs

If K1 has time, add a short operator note under `.hermes/plans/` or script docstring only; do not add broad docs unless requested.

## 5. Test plan for K1

Add/extend tests before or alongside implementation:

1. New `tests/test_investor_flow_snapshot_builder.py`
   - Mock fetcher returns two symbols and multiple dates.
   - Assert payload count/date parsing/source/individual derivation.
   - Assert double-buy/double-sell derived by repository or builder inputs do not override repository derivation unless intended.
   - Assert consecutive buy/sell days for foreign/institution/individual.
   - Assert per-date ranks for foreign/institution buy/sell.
   - Assert empty symbol data yields warnings and skips rows.

2. New `tests/test_investor_flow_snapshot_job.py`
   - Seed `KRSymbolUniverse`; mock builder/fetcher.
   - Dry-run returns `committed=False`, row/date/symbol counts, samples, idempotency, and leaves DB unchanged.
   - Commit mode persists via repository in test DB only and second dry-run reports `wouldUpdate` rather than `wouldInsert`.
   - Non-KR market raises/rejects.

3. New/extended CLI tests, either in a new file or job test:
   - `scripts.build_investor_flow_snapshots.parse_args()` defaults to dry-run.
   - `--all` mutual exclusions.
   - `--commit` flips `dry_run`/commit only explicitly.
   - Invalid days rejected.

4. Extend task tests or add `tests/test_investor_flow_snapshot_tasks.py`
   - Task wrapper default `commit=False` passes through to job and returns camelCase fields.
   - Static test that `app/tasks/investor_flow_snapshot_tasks.py` does not attach `schedule=[...]` until explicit scheduler-activation approval.

5. Existing targeted regression tests:

```bash
uv run --group test pytest \
  tests/test_investor_flow_snapshots_repository.py \
  tests/test_investor_flow_service.py \
  tests/test_invest_coverage.py \
  tests/test_naver_finance.py \
  tests/test_investor_flow_snapshot_builder.py \
  tests/test_investor_flow_snapshot_job.py \
  tests/test_investor_flow_snapshot_tasks.py \
  -q
```

6. Static checks:

```bash
uv run --group dev ruff check \
  app/services/investor_flow_snapshots \
  app/jobs/investor_flow_snapshots.py \
  app/tasks/investor_flow_snapshot_tasks.py \
  scripts/build_investor_flow_snapshots.py \
  tests/test_investor_flow_snapshot_builder.py \
  tests/test_investor_flow_snapshot_job.py \
  tests/test_investor_flow_snapshot_tasks.py
```

If type tooling is part of the branch convention, run focused `ty` only after implementation if it is already stable in this repo; do not block on unrelated repo-wide type debt.

## 6. Dry-run / approval packet procedure after K1 deploy candidate

K1/K2 must not run production writes. After code review and deploy candidate, produce a bounded dry-run packet first, for example:

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --limit 20 --days 20
```

Approval packet should include:

- Command run.
- Git SHA and environment (local/staging/prod read-only).
- `committed=false` confirmation.
- Symbols resolved.
- Snapshots built.
- Snapshot date distribution.
- Idempotency: would insert/update/duplicates.
- Warnings/skips.
- Sample rows.
- Proposed write command, bounded scope, and rollback note.

Only after explicit operator approval may an ops task run the matching command with `--commit` against production. Scheduler activation is a separate approval.

## 7. Approval gates / prohibited actions

These gates must be restated in every K1/K2 handoff:

- Production DB writes/backfills require explicit operator approval after dry-run evidence.
- `--commit` is forbidden in production without approval.
- Recurring scheduler activation/unpause or attaching a `schedule=[...]` that will run in production requires separate explicit operator approval.
- No broker/order/watch/order-intent side effects.
- No live/paper trading orders.
- No secret/env printing.
- No request-path live scraping fallback; `/invest` endpoints remain read-only over durable tables.

## 8. Main risks and mitigations

1. Naver data shape changes or sparse rows.
   - Mitigation: builder skips invalid rows with warnings; tests cover empty/unparseable data.

2. Universe is large; live Naver fetches can be slow/rate-limited.
   - Mitigation: batch/concurrency controls, bounded dry-run first, samples/warnings in packet.

3. Multiple sources for same symbol/date.
   - Mitigation: preserve `source`; repository unique constraint includes source. Read service currently deterministic by `source.asc()` and keeps first source. Do not change source precedence unless explicitly planned.

4. Scheduler activation could accidentally write every weekday.
   - Mitigation: K1 adds manual unscheduled task wrapper only; activation is a later approval-gated change.

5. Branch tracking confusion.
   - Current branch is fast-forwarded to `github/main` (`6024a264`) but tracks local path `origin/main`, so `git status` shows ahead of local `origin/main`. Push/review should target GitHub remote carefully.

## 9. Suggested downstream task sequence

Existing child task `t_76058c44` should execute K1 implementation using this plan.

After K1:

- K2 reviewer: verify branch diff, tests, dry-run safety gates, and no accidental schedule activation.
- K3 deploy/dry-run packet: deploy if approved by normal PR flow, run bounded dry-run only, post packet, and block for explicit production write/scheduler approval.
- K4 ops action only after approval: production `--commit` backfill and then separate scheduler activation if approved.
