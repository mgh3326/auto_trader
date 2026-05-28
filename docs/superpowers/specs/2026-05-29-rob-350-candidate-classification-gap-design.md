# ROB-350 follow-up — classify non-buy new-buy candidates (no silent drop)

Date: 2026-05-29
Issue: ROB-350 (auto_trader: KR /invest/reports 매수·매도·신규매수 후보 리포트 고도화)
Branch base: `rob-350` (follow-up slice on top of `73bd9f1d feat(ROB-350): rank new-buy candidates in reports`)

## Problem (gap #1 from code review)

`EvidenceAutoEmitter.propose` (`app/services/action_report/snapshot_backed/auto_emit.py`)
ranks new-buy candidates and emits `buy_review` items, but **silently drops**
any ranked candidate that does not clear the actionable-quote gate:

- `symbol_pair is None` (no symbol snapshot / no quote evidence) → `continue`
- `not _quote_is_actionable(quote)` (status≠ok or zero best/depth) → `continue`
- the whole buy loop is gated by `if candidate_actionable:` so when the
  screener universe usefulness ≠ `"useful"` (stale/empty), **every** candidate
  is skipped.

ROB-350 acceptance criteria require the opposite: *"stale screener, 저유동성,
가격/호가/뉴스 근거 부족 후보는 바로 buy 후보로 올리지 않고 `watch_only` 또는
`rejected`로 분류하고 이유를 남긴다."* Dropped candidates currently vanish with
no row and no reason.

## Decisions (locked with operator)

1. **Taxonomy (Q1):** deterministic verdicts only — `data_gap` + `watch_only`.
   `rejected` / `limit_wait` are directional judgments left to Hermes (matches
   the existing "honest verdict only" discipline in `action_verdict.py`).
   - no quote snapshot → `data_gap` (호가 근거 부족)
   - quote present but not actionable (저유동성) → `watch_only`
   - quote actionable but screener stale (`usefulness != "useful"`) → `watch_only`
   - quote actionable + universe useful → `buy_review`
2. **Trigger (Q2):** **always-on** — runs whenever a `candidate_universe`
   snapshot is in the bundle, regardless of `intraday_floor`. This changes
   non-intraday reports too (they now emit watch_only/data_gap candidate rows),
   which is the intended ROB-350 behavior.
3. **Structure:** Approach A + C — extract a pure `classify_candidate_symbol()`
   helper into `action_verdict.py` (symmetric with `classify_held_symbol`), and
   restructure the auto_emit buy loop into a single "classify every candidate"
   pass.

## Design

### 1. `classify_candidate_symbol` (action_verdict.py)

Pure function, symmetric with `classify_held_symbol`, honest-verdict only:

```python
def classify_candidate_symbol(
    quote: dict[str, Any] | None,
    *,
    universe_useful: bool,
    quote_snapshot_present: bool,
) -> str:
    # 1. no quote snapshot at all          -> "data_gap"
    # 2. quote present, not actionable     -> "watch_only"  (저유동성)
    # 3. quote actionable, universe stale   -> "watch_only"  (스크리너 stale)
    # 4. quote actionable, universe useful  -> "buy_review"
```

`quote_snapshot_present` lets the caller distinguish "no snapshot row" (data_gap)
from "snapshot present but quote not actionable" (watch_only). Never returns
`rejected` / `limit_wait`.

### 2. auto_emit.py — single classification loop

Replace the `if candidate_actionable:` gate + buy emit block with one loop over
`sorted(candidate_order, key=_candidate_sort_key)`:

- `sym in held` → skip (handled by the existing held_and_trending path).
- resolve quote (None if no symbol snapshot), compute verdict via
  `classify_candidate_symbol`.
- `buy_review` honors `max_buy_candidates` cap; overflow beyond the cap
  (edge case — cap normally == candidate_limit == universe size) downgrades to
  `watch_only` with reason `beyond_candidate_budget`.
- emit exactly one `IngestReportItem` per candidate with:
  - `item_kind`: `action` for buy_review, `risk` for watch_only/data_gap
  - `rank`/`priority` stamped for all verdicts (rank-sorted projection works in
    risk/data_gap sections too)
  - `evidence_snapshot["reject_or_wait_reason"]` ∈
    {`quote_missing`, `low_liquidity`, `screener_stale`, `beyond_candidate_budget`}
    (absent for buy_review) + existing candidate_rank/score/source/quote meta
  - `operation="review"`, `apply_policy="requires_user_approval"` (unchanged
    lockdown — no mutation)
- emitted symbols feed `already_proposed`, so news-watch and held_and_trending
  loops dedup as today.

Korean rationale per verdict:
- watch_only/low_liquidity: `신규 후보 관망 N순위 — {sym} (저유동성: spread_bps {x})`
- watch_only/screener_stale: `신규 후보 관망 N순위 — {sym} (스크리너 stale)`
- data_gap: `신규 후보 판단 보류 — {sym} (호가 스냅샷 없음)`

### 3. Schema + ActionPacket projection

- `ActionPacketEntry` (`app/schemas/investment_reports.py`): add optional
  `reject_or_wait_reason: str | None = None` (additive, mirrors the prior
  rank/priority addition).
- `action_packet.py`: `_entry` reads `reject_or_wait_reason` from
  `evidence_snapshot`. Existing routing already sends `watch_only` →
  `risk_reviews` and `data_gap` → `data_gaps`; verify `DataGapEntry` for a
  candidate carries `source=symbol` + `reason=rationale`. `risk_reviews` should
  be rank-sorted like `new_buy_candidates`.
- No migration: classification lives in `evidence_snapshot` JSON only.

### 4. Frontend

- `types/investmentReports.ts`: add `rejectOrWaitReason?: string | null` to
  `ActionPacketEntry`.
- `api/investmentReports.ts`: normalize `reject_or_wait_reason`.
- `components/.../ActionPacketView.tsx`: render the reason as a small chip on
  risk/data-gap rows so the operator sees *why* a candidate was held back.

### 5. Tests

Backend:
- `classify_candidate_symbol`: all 4 branches (unit).
- auto_emit:
  - high-rank candidate with no quote snapshot → `data_gap` row (not dropped).
  - candidate with non-actionable quote → `watch_only` (low_liquidity).
  - stale universe (`usefulness != "useful"`) → all candidates `watch_only`
    (screener_stale), zero `buy_review`.
  - useful universe, mixed quotes → actionable→buy_review, others classified.
  - held candidate is not double-emitted (dedup).
  - regression: existing buy-rank/limit and no_new_buy floor tests stay green.
- action_packet: data_gap candidate → `data_gaps` with symbol+reason; watch_only
  candidate → `risk_reviews` rank-sorted; `reject_or_wait_reason` exposed.

Frontend:
- ActionPacketView renders reject_or_wait_reason chip on risk/data-gap rows.

## Non-goals / safety boundaries (unchanged)

- No broker/order/watch/order-intent mutation; every item stays
  `operation="review"` + `requires_user_approval`. Static import guard intact.
- No `rejected`/`limit_wait` fabrication (Hermes-only).
- No migration, no scheduler, no recurring scraping.
- Toss/Naver supplementary evidence and budget-generous candidate count (§2/§3
  of ROB-350) remain separate follow-up slices, out of scope here.

## Verification

- `uv run --all-groups pytest` on the touched test modules.
- `uv run ruff check app/ tests/`.
- `npx vitest run --pool=forks` on touched frontend tests (threads pool is flaky).
- No-mutation evidence: static import guard test + assert all emitted items are
  review/requires_user_approval in the auto_emit tests.
