# ROB-183 — WiseFn KR Earnings Bounded Backfill Approval Packet

> **For agentic workers:** This is an **approval packet**, not an executable implementation plan. No code or DB changes may be applied from this document without **광현님 (robin@watcha.com)** approval recorded in Discord / Linear / Kanban for the exact stage being executed. Stages 1–4 are scope-gated; Stage 0 (dry-run preview) is the only stage executable without further approval, and it produces zero DB / HTTP side effects.

**Goal:** Define a bounded, evidence-backed plan for the first live WiseFn KR earnings backfill, initially scoped to 2026-05, with explicit dry-run commands, idempotency / rollback notes, license / robots risk, and approval gates at every state-changing step.

**Architecture:** Reuse the existing ROB-171 ingestion seams (`scripts/ingest_market_events.py`, `app/services/market_events/{wisefn_helpers,ingestion,normalizers,repository}.py`). No new tables, no schema migration. All idempotency comes from the deterministic `source_event_id` and the `(source, category, market, source_event_id)` partial unique index already created in `alembic/versions/a7e9c128_add_market_events_tables.py`. The CLI's `--dry-run` flag never opens an HTTP client and never writes the DB. The CLI's `WISEFN_EARNINGS_ENABLED` gate (default `false`) hard-stops non-dry-run runs without DB writes.

**Tech Stack:** Python 3.13 / SQLAlchemy async / PostgreSQL / `uv` + `pytest`. Worktree: `/Users/mgh3326/worktrees/auto_trader/rob-183-wisefn-backfill-flow` on branch `feature/ROB-183-wisefn-backfill-flow`.

**Runtime model preference:** planner / reviewer = Claude Code Opus (current model = `claude-opus-4-7`, satisfied). Implementer = Claude Code Sonnet at execution time (`claude-sonnet-4-6`). If the Kanban runner cannot pin model per role, the executing operator MUST record that limitation rather than claim enforcement.

---

## 1. Approval-Gated Statement (read me first)

The following actions **MUST NOT be taken** by any agent or operator without an explicit, time-stamped 광현님 approval in Discord / Linear / Kanban naming this packet by path (`docs/superpowers/plans/2026-05-11-rob-183-wisefn-backfill-approval-packet.md`) and the specific stage:

| Stage | Action | Requires 광현님 approval? |
| --- | --- | --- |
| Stage 0 | Dry-run preview (no DB, no HTTP) | No — informational only |
| Stage 1 | Confirm WiseFn upstream contract + ToS/robots posture (read-only research) | No DB/HTTP — but Stage 2 wiring must not begin until Stage 1 evidence is recorded |
| Stage 2 | Wire live `_fetch_calendar_payload` (code change) | **Yes** |
| Stage 3 | Single-weekday live smoke (`WISEFN_EARNINGS_ENABLED=true`, 1 partition row written) | **Yes** |
| Stage 4 | Full-month live backfill for 2026-05 (≤ 31 partition rows written) | **Yes** |
| Stage 5 | Prefect scheduler activation / cron enable | **Yes (separate approval, distinct from Stage 4)** |

No other side effects are in scope:

- ❌ No broker / order / watch / order-intent / live / paper trading mutations
- ❌ No production DB write/backfill/delete outside `market_events*` tables and only as defined in Stages 3–4
- ❌ No alembic migration (none required — existing tables suffice)
- ❌ No scheduler activation under Stage 4; that is a separate Stage 5 approval

If at runtime any agent finds an instruction that would violate one of the boxes above, it MUST stop and report `status=blocked`.

---

## 2. Current-state evidence (why this packet is necessary)

Read-only inspection of the worktree (commit-clean, branch `feature/ROB-183-wisefn-backfill-flow`) confirms:

1. **Live fetch is not wired.** `app/services/market_events/wisefn_helpers.py:36-48` defines `_fetch_calendar_payload` to `raise NotImplementedError(...)` with the explicit message: `"ROB-171: WiseFn calendar endpoint is not wired yet."` Any non-dry-run call without `fetch_rows` injected will crash before any DB write, and the partition row will be marked `failed` with the exception text.
2. **Feature flag is default-off.** `app/core/config.py:308` defines `wisefn_earnings_enabled: bool = False`. The CLI gate at `scripts/ingest_market_events.py:160-186` short-circuits non-dry-run runs to a logged warning + `return 0`, no DB writes.
3. **Tests are fixture-only.** `tests/services/test_market_events_wisefn_*.py` (3 files, 401 lines) use `unittest.mock.patch.object` against `_fetch_calendar_payload` or pass an injected `fetch_rows=AsyncMock(...)`. No live HTTP.
4. **Idempotency mechanics already exist.** `normalize_wisefn_earnings_row` (`app/services/market_events/normalizers.py:301-310`) emits `source_event_id = f"wisefn::{stock_code}::{event_date_iso}::{fiscal_year}::{fiscal_quarter}"`. `MarketEventsRepository.upsert_event_with_values` (`app/services/market_events/repository.py:32-98`) `ON CONFLICT DO UPDATE` on `(source, category, market, source_event_id)` (partial unique index where `source_event_id IS NOT NULL`).
5. **Partition state machine already exists.** `pending → running → succeeded|failed` per `(source, category, market, partition_date)`. Failure paths capture the exception text truncated to 2000 chars and increment `retry_count`.
6. **Weekend gating already exists.** `app/services/market_events/expected_sources.py:41-54` excludes `wisefn` on Saturday / Sunday (KR market closure).
7. **Runbook already covers the dry-run posture.** `docs/runbooks/market-events-ingestion.md:174-261` ("KR earnings (WiseFn, ROB-171)"). This packet adds approval-gate discipline, not new code.

**Conclusion:** No code change is required to safely run **Stage 0** dry-run. Stage 2 (live wiring) requires a small targeted code change inside `wisefn_helpers.py` only, gated by 광현님 approval and the upstream contract confirmation in Stage 1.

---

## 3. Bounded scope

**Calendar:** 2026-05-01 through 2026-05-31 inclusive (31 calendar days; 21 weekdays after the existing weekend gate excludes May 2/3, 9/10, 16/17, 23/24, 30/31).

**Why 2026-05 only:**

- The single-month window matches the existing `--month YYYY-MM` thin wrapper at `scripts/ingest_market_events.py:80-90` and the runbook's published example (`--month 2026-05 --dry-run`).
- Q1 FY2026 KR earnings releases concentrate in late April / early-to-mid May for KOSPI/KOSDAQ filers, so 2026-05 is a natural first slice with realistic-density data once live.
- One month bounds blast radius: ≤ 31 partition rows, ≤ ~hundreds of `market_events` rows for `(source='wisefn', event_date BETWEEN '2026-05-01' AND '2026-05-31')`. Rollback SQL stays trivially narrow (see §6).

**Evidence-driven narrower-range option:** Because `_fetch_calendar_payload` is unwired, the **first live execution after Stage 2 wiring lands MUST be a single weekday** (recommended: **2026-05-13, Wednesday** — mid-week, mid-month, away from holidays; alternatives: 2026-05-07 Thu or 2026-05-14 Thu). This becomes Stage 3. Only after Stage 3 evidence (row counts plausible, no parser warnings spamming logs, no upstream HTTP anomalies) does Stage 4 (full month) proceed.

**Out of scope for this packet:** ROB-171 follow-ups (realized eps/revenue join from DART; Prefect deployment scheduler; UI changes to `/invest/calendar`). These remain documented in the runbook as future work and require separate approval.

---

## 4. Stage 0 — Dry-run preview (executable without further approval)

**Side effects:** None. The CLI's `--dry-run` path at `scripts/ingest_market_events.py:191-197` only logs `[DRY-RUN] would ingest wisefn/earnings/kr for <date>` and increments an in-memory counter; no fetch, no DB.

### 4.1 Required environment posture (verify, do not change)

```bash
# From the worktree root
cd /Users/mgh3326/worktrees/auto_trader/rob-183-wisefn-backfill-flow

# Confirm flag is default-off (read-only)
uv run python -c "from app.core.config import settings; print('wisefn_earnings_enabled =', settings.wisefn_earnings_enabled)"
# Expected: wisefn_earnings_enabled = False
```

If the printed value is `True`, **stop** and report — production env must remain `false` until Stage 3 is approved.

### 4.2 Whole-month dry-run command

```bash
uv run python -m scripts.ingest_market_events \
  --source wisefn --category earnings --market kr \
  --month 2026-05 --dry-run
```

**Expected stdout (last line, JSON):**
```json
{"source":"wisefn","category":"earnings","market":"kr","from_date":"2026-05-01","to_date":"2026-05-31","dry_run":true,"succeeded":31,"failed":0}
```

**Expected log lines:** 31 lines of `[DRY-RUN] would ingest wisefn/earnings/kr for 2026-05-DD` (one per day, inclusive May 1–31). The weekend gate in `expected_sources.py` is consulted by the *expected coverage report*, not by the CLI loop itself — the CLI iterates every day in the range, which is the existing ROB-171 design and is correct for a backfill (some emitters publish weekend revisions).

**Exit code:** `0`.

**Verification command for the operator (record output as evidence):**
```bash
uv run python -m scripts.ingest_market_events \
  --source wisefn --category earnings --market kr \
  --month 2026-05 --dry-run 2>&1 | tee /tmp/rob-183-stage0-dryrun.log
```

### 4.3 Equivalent explicit-range form (for parity check)

```bash
uv run python -m scripts.ingest_market_events \
  --source wisefn --category earnings --market kr \
  --from-date 2026-05-01 --to-date 2026-05-31 --dry-run
```

Stdout JSON `from_date`/`to_date` must match `2026-05-01`/`2026-05-31` exactly. This proves the `--month` wrapper is in fact the documented thin alias and not a divergent code path.

### 4.4 Tests (read-only, fixture-driven, no live HTTP)

```bash
uv run python -m pytest \
  tests/services/test_market_events_wisefn_normalizers.py \
  tests/services/test_market_events_wisefn_helpers.py \
  tests/services/test_market_events_wisefn_ingestion.py \
  tests/test_market_events_cli.py \
  -v
```

**Expected:** all pass (currently fixture-only). Record the pytest summary line and the count of `PASSED` items in the Kanban evidence comment.

### 4.5 Static analysis (optional but recommended)

```bash
uv run ruff check app/services/market_events/wisefn_helpers.py app/services/market_events/ingestion.py app/services/market_events/normalizers.py scripts/ingest_market_events.py
uv run ty check app/services/market_events/wisefn_helpers.py app/services/market_events/normalizers.py
```

**Expected:** clean. Record output.

---

## 5. Expected row counts

**Honest state:** unknown until Stage 1 (upstream contract confirmation) lands evidence.

**Floor / ceiling estimate (informational only, not commitments):**

- KR listed universe: ~2,400 securities (KOSPI ~800, KOSDAQ ~1,600).
- Earnings cluster window for Q1 FY2026 in 2026-05: most large-caps file mid-to-late May (15-day post-quarter-end deadline driving 5/13–5/15 spikes for Q1 ending 3/31; KOSPI bigs may pre-schedule).
- **Per-weekday plausible range:** ~10–80 schedule rows on quiet days, **spike day plausible 80–200 rows** if WiseFn surfaces multiple corp-name aliases.
- **Whole-month plausible range:** **~400–1,500 rows** with mode ~700.

**Hard ceiling at which the operator MUST stop and escalate:** if a single day's `upserted` count exceeds **500**, treat it as parser drift / upstream payload shape change and stop the Stage 4 run. Investigate before continuing.

These numbers update once Stage 3 single-day evidence lands.

---

## 6. Idempotency & rollback

### 6.1 Idempotency (already implemented, no action needed)

- **Deterministic key:** `source_event_id = wisefn::{stock_code}::{event_date_iso}::{fiscal_year}::{fiscal_quarter}` (`normalizers.py:301-310`).
- **Index:** partial unique index on `(source, category, market, source_event_id)` where `source_event_id IS NOT NULL` (created in `alembic/versions/a7e9c128_add_market_events_tables.py`).
- **Upsert path:** `repository.upsert_event_with_values` uses `pg_insert(...).on_conflict_do_update(...)` with the column tuple above. Existing rows are updated, never duplicated.
- **Partition state machine:** `(source='wisefn', category='earnings', market='kr', partition_date=<DDD>)` keyed via `market_event_ingestion_partitions`. Re-running a partition flips `pending → running → succeeded|failed`; success clears `last_error`; failure increments `retry_count` and stores truncated error text. Re-running a `succeeded` partition is safe — it idempotently upserts the same rows.

**Operational implication:** the operator MAY re-run Stage 3 / Stage 4 commands without manual cleanup. The system converges to the latest WiseFn payload.

### 6.2 Emergency rollback (Stage 4 only, post-approval)

If an anomaly is discovered after a live Stage 4 run, the rollback is bounded SQL scoped to `(source='wisefn', event_date BETWEEN '2026-05-01' AND '2026-05-31')`:

```sql
-- (a) Inspect first, do not execute blindly. RUN AS read-only first:
SELECT COUNT(*) AS event_count
FROM market_events
WHERE source = 'wisefn'
  AND category = 'earnings'
  AND market = 'kr'
  AND event_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';

SELECT COUNT(*) AS value_count
FROM market_event_values v
JOIN market_events e ON v.event_id = e.id
WHERE e.source = 'wisefn'
  AND e.category = 'earnings'
  AND e.market = 'kr'
  AND e.event_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';

SELECT partition_date, status, event_count, retry_count, last_error
FROM market_event_ingestion_partitions
WHERE source = 'wisefn'
  AND category = 'earnings'
  AND market = 'kr'
  AND partition_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
ORDER BY partition_date;

-- (b) Rollback (only after 광현님 approves the rollback specifically):
BEGIN;

DELETE FROM market_event_values
WHERE event_id IN (
  SELECT id FROM market_events
  WHERE source = 'wisefn'
    AND category = 'earnings'
    AND market = 'kr'
    AND event_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
);

DELETE FROM market_events
WHERE source = 'wisefn'
  AND category = 'earnings'
  AND market = 'kr'
  AND event_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';

UPDATE market_event_ingestion_partitions
SET status = 'pending',
    started_at = NULL,
    finished_at = NULL,
    event_count = 0,
    last_error = NULL,
    retry_count = 0
WHERE source = 'wisefn'
  AND category = 'earnings'
  AND market = 'kr'
  AND partition_date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';

-- Verify counts are zero, then:
COMMIT;
-- or ROLLBACK; if any count is unexpected
```

**Note:** the rollback is intentionally scoped by both `source='wisefn'` AND the 31-day window — it cannot collide with other ingestion sources (DART disclosures, Finnhub US earnings, ForexFactory economic events) since those filter on different `source` values. The `market_event_values` DELETE is currently a no-op because WiseFn schedule rows never emit `market_event_values` (the normalizer returns an empty list — see `normalizers.py:355,376`), but the DELETE is included for safety in case a future ROB-171 follow-up adds realized values.

---

## 7. License / robots / ToS risk

**Status:** **OPEN** — must be resolved in Stage 1 before Stage 2 wiring.

**Concerns:**

1. **WiseFn / WiseReport publishing license.** WiseFn (와이즈에프엔) and WiseReport are commercial Korean financial data aggregators. Whether their earnings calendar (실적발표 예정) is published under a permissive scraping policy, requires a paid API key, or requires a corporate redistribution license is **not yet confirmed in repo evidence**. The runbook (`docs/runbooks/market-events-ingestion.md:246-250`) explicitly calls this out as a ROB-171 follow-up: *"confirm WiseFn / WiseReport endpoint, auth posture, per-row schema, and ToS / scraping permissions"*.
2. **robots.txt.** Not yet inspected because the upstream URL has not been chosen / documented. A real domain MUST be fetched and checked before any HTTP traffic to it. Record the contents (date-stamped) under `docs/evidence/ROB-183/robots-<host>-<YYYY-MM-DD>.txt` or as a Linear attachment.
3. **PII / copyright.** Schedule rows (corp_name, fiscal_year, fiscal_quarter, release_type, time_hint) are public/factual and unlikely to carry PII. If the payload includes broker analyst names, target prices, or research excerpts, those fall under the existing ROB-140 research-reports copyright guard (`app/schemas/research_reports.py` — `full_text_exported`/`pdf_body_exported` rejection). The WiseFn normalizer does **not** consume those fields today; if the live payload introduces them, the operator must add a redaction step before persisting `raw_payload_json`.
4. **Rate / robots posture for repeated month-long pulls.** Once live, a monthly refresh is gentle (≤ 31 requests/run, typically 1 batch/day with the planned Prefect cadence). Single-month backfills are de-minimis. Spike days during a many-month historical backfill are not in scope here.
5. **Sensitive-key redaction is already wired.** `repository.upsert_event_with_values` runs `_redact_sensitive_keys` on `raw_payload_json` (`app/services/market_events/repository.py:38-41`) before persistence; same path used by Alpaca paper ledger. This protects against accidental API-key echoing if WiseFn payloads ever embed credentials.

**Stage 1 deliverable:** a short Linear comment / Kanban evidence note containing (a) the chosen WiseFn endpoint URL, (b) a copy of its robots.txt at fetch time, (c) the ToS clause (or paid-API contract reference) that authorizes our use, (d) declared auth posture (anonymous? cookie? API key? OAuth?). Until that lands, Stage 2 is blocked.

---

## 8. Approval-gated stage walkthrough

### Stage 0 — Dry-run preview (no approval required)

Executes §4 commands. Records exit codes, stdout JSON, pytest summary in Kanban evidence. Produces zero side effects.

### Stage 1 — Upstream contract confirmation (research only, no DB/HTTP)

- Plain-language confirmation of WiseFn endpoint, auth, ToS, robots.txt.
- Pin the upstream row schema in a docstring update to `wisefn_helpers.py` (no production behavior change; docstring-only PR if the package allows). **No live HTTP yet.**
- Output: Linear comment with the four bullets in §7 ("Stage 1 deliverable").
- **Blocking gate to Stage 2:** 광현님 review + thumbs-up.

### Stage 2 — Live `_fetch_calendar_payload` wiring (CODE CHANGE, requires approval)

- Replace `NotImplementedError` body with an `httpx.AsyncClient` call to the endpoint confirmed in Stage 1.
- Add minimal HTTP-level tests (using `httpx.MockTransport` or `respx`) covering: success, non-200, malformed payload (`items` not a list), per-row date mismatch.
- Add the live secret (if any) to `app/core/config.py` as `wisefn_api_key: str | None = None` with a corresponding env var. Default `None` — required in production only when `wisefn_earnings_enabled=True`.
- `WISEFN_EARNINGS_ENABLED` remains `false` in production until Stage 3.
- **Tests required to pass before merge:**
  - `tests/services/test_market_events_wisefn_helpers.py` (existing fixture path)
  - new HTTP-level tests (Stage 2 deliverable)
  - `tests/test_market_events_cli.py` (existing CLI gate tests)
  - `tests/services/test_market_events_wisefn_ingestion.py` (existing partition tests)
- **Out of scope for Stage 2:** scheduler, Prefect deployment, UI changes.

### Stage 3 — Single-weekday live smoke (requires approval)

- Operator session sets `WISEFN_EARNINGS_ENABLED=true` **only for the duration of the run**. After completion, the env var is unset / set back to `false`.
- Single command:
  ```bash
  WISEFN_EARNINGS_ENABLED=true uv run python -m scripts.ingest_market_events \
    --source wisefn --category earnings --market kr \
    --from-date 2026-05-13 --to-date 2026-05-13
  ```
- Recorded evidence: the JSON summary line (`succeeded=1`, `failed=0`), the partition row state after the run, the count of `market_events` rows where `(source='wisefn', event_date='2026-05-13')`, and a 10-row sample inspection.
- Investigate any of: `failed != 0`, `succeeded != 1`, row count > 500, parser warning log spam, `raw_payload_json` redaction misses. Halt before Stage 4 if any anomaly.

### Stage 4 — Full-month live backfill for 2026-05 (requires approval, distinct from Stage 3)

- Operator session sets `WISEFN_EARNINGS_ENABLED=true` only for the duration of the run.
- Command:
  ```bash
  WISEFN_EARNINGS_ENABLED=true uv run python -m scripts.ingest_market_events \
    --source wisefn --category earnings --market kr \
    --month 2026-05
  ```
- Expected JSON summary: `succeeded=31, failed=0` (the CLI iterates calendar days; weekend partitions will likely return zero rows, which is correct and is recorded as a `succeeded` partition with `event_count=0`).
- Total row-count ceiling: **1,500 events**. Hard-stop ceiling per day: **500 events** (see §5). If any daily upsert count exceeds 500, the operator MUST cancel the run, capture the offending payload, and escalate.
- Post-run verification: same SELECTs as §6.2 part (a) — record counts + partition states.

### Stage 5 — Scheduler activation (separate approval, NOT bundled with Stage 4)

- Out of scope to design here. The runbook (`docs/runbooks/market-events-ingestion.md:255-257`) calls out monthly Prefect cadence as the natural fit. Activation is a distinct change and requires a distinct approval comment from 광현님 referencing this packet's Stage 5.

---

## 9. Evidence trail / next-action checklist

The Kanban / Linear evidence comment for ROB-183 MUST include, in order:

- [ ] Path of this packet committed: `docs/superpowers/plans/2026-05-11-rob-183-wisefn-backfill-approval-packet.md`
- [ ] Worktree path: `/Users/mgh3326/worktrees/auto_trader/rob-183-wisefn-backfill-flow`
- [ ] Branch: `feature/ROB-183-wisefn-backfill-flow`
- [ ] Commit hash of this packet (post-commit)
- [ ] Stage 0 dry-run stdout JSON line (`--month 2026-05 --dry-run`)
- [ ] Stage 0 pytest summary (`tests/services/test_market_events_wisefn_*.py`, `tests/test_market_events_cli.py`)
- [ ] Confirmation that `settings.wisefn_earnings_enabled` is `False` in production
- [ ] Explicit statement: "Stages 2–5 require 광현님 approval per stage."
- [ ] Runtime model record: planner = Opus (this session), implementer = Sonnet (when Stage 2 is executed). If Kanban cannot enforce per-stage model selection, log that limitation.
- [ ] Discord / Linear pointer to where 광현님's approval will be captured.

---

## 10. Self-review

**Spec coverage:**
- ✅ Inspect WiseFn/WiseReport ingestion code read-only → §2 with file:line cites
- ✅ Inspect production settings read-only → §2.2 (`wisefn_earnings_enabled = False`) + §4.1 verify command
- ✅ Bounded approval packet → §1 + §8 stage walk
- ✅ Target 2026-05 only unless evidence suggests narrower → §3 explains why a narrower-than-month first live execution (Stage 3) is recommended
- ✅ Exact dry-run / read-only commands → §4
- ✅ Expected row counts if known → §5 (honest "unknown until Stage 1"; floor/ceiling estimate)
- ✅ Idempotency / rollback notes → §6
- ✅ License / robots risk → §7
- ✅ Strict statement that commit/backfill and scheduler activation require 광현님 approval → §1 + §8 + repeated at every stage header
- ✅ Worktree-only safety → §1 boxes + §9 evidence
- ✅ No broker / order / watch / order-intent / live / paper trading side effects → §1 boxes
- ✅ Planner=Opus, implementer=Sonnet preference → header + §9 evidence
- ✅ Evidence in Kanban / Linear → §9

**Placeholder scan:** none — every command, SQL, and threshold is concrete.

**Type / name consistency:** `wisefn_earnings_enabled` (config), `WISEFN_EARNINGS_ENABLED` (env var), `_fetch_calendar_payload` (seam), `ingest_kr_earnings_wisefn_for_date` (service), `normalize_wisefn_earnings_row` (normalizer), `source_event_id` (key), `wisefn::{stock_code}::{event_date_iso}::{fiscal_year}::{fiscal_quarter}` (deterministic id format) — all match `wisefn_helpers.py`, `ingestion.py`, `normalizers.py`, `repository.py`, `config.py:308`, and `scripts/ingest_market_events.py:64,104,164-169`.

**Risks I am explicitly NOT addressing here (deferred to other tickets):**
- Realized eps/revenue join from DART quarterlies (ROB-171 follow-up #2).
- Prefect deployment (ROB-171 follow-up #3 and this packet's Stage 5).
- UI consumption (`/invest/calendar` is already wired; no change needed once Stage 4 lands).

---

**End of packet. No further executable instructions follow.**
