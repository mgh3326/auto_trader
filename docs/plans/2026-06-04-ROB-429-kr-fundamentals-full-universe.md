# ROB-429 — KR fundamentals tv snapshot full-universe loading + read-path full-partition + commit guard

> **상태:** spec (구현 전). 근본원인 코드 grounding + 라이브 probe 정량화 완료.
> **기준일:** 코드 2026-06-04 `origin/main` c2118bfc (#1115 PR-A+PR-B, #1116 PR-C 머지 후). 라이브 probe 2026-06-04.
> **분류:** Bug (배포 후 parity 미달) + hardening. **스코프 A+B** (원 이슈 A만 → read-path B 확장).
> **목표:** `/invest/screener` 국내 fundamentals 프리셋이 Toss와 **비교가능한 종목 집합**(comparable+honest)을 내도록 (A) 스냅샷 full-universe 적재 + (B) read-path full-partition 평가. exact 종목 일치는 비목표(C, follow-up).

## Context

ROB-428(#1115 PR-A+PR-B, #1116 PR-C) 머지·배포 후에도 KR fundamentals 프리셋 결과가 Toss와 크게 다름. operator가 backfill을 돌렸으나 스냅샷에 **200행만** 적재됐고, read-path도 시총 상위만 평가해 Toss의 중소형주 매칭이 표시 불가.

## Current State (verified 2026-06-04, code-grounded + live probe)

| 측정 | 값 | 근거 |
|---|---|---|
| active KR universe | ~3,909 | `active_universe_count(market="kr")` |
| `invest_kr_fundamentals_snapshots` 최신 파티션 | 2026-06-04 | operator backfill |
| **persisted rows** | **200 (5.1%)** | A 버그 |
| tvscreener KR **full fetch** | **4,250** | 라이브 probe |
| 저평가성장주(per≤20·rev_yoy≥10·eps_yoy≥20) full universe 매칭 | **181 ≈ Toss 187** | 라이브 probe |
| 그 매칭 중 non-top-cap | **179/180** | A(200행)+B(market_cap cand_cap)에 배제 |

### 근본 원인 (3)
- **A (지배적, 스냅샷 데이터 cap)** — `app/services/invest_kr_fundamentals_snapshots/provider.py:95` `query_limit = limit or 200`. job(`all_symbols→limit=None`) + CLI(`--all`)을 써도 `None or 200 = 200` → tvscreener `set_range(0,200)` + `rows[:200]` → 200행만. operator full backfill이 이 버그를 침. (ROB-426 `limit or N` 가드우회와 동종)
- **B (2차 병목, read-path cap)** — `app/services/invest_view_model/kr_fundamentals_tv_screener.py:340` `cand_cap = max(limit*8,200)` + `order_by(market_cap desc).limit(cand_cap)` → 스냅샷이 full이어도 시총 상위 N개만 후보 평가 → 중소형주 제외. **A만 고쳐도 B가 소형주를 다시 거름.**
- **C (본질적, 작음, follow-up)** — YoY proxy(eps_yoy/revenue_yoy) vs Toss 3년평균 TTM, TradingView vs Toss 벤더 → 개수는 거의 같으나(181≈187) 종목 overlap 일부 상이. honest "comparable" 유지.

## Decisions (locked)
- **D1** B = **full-partition 평가**(cand_cap 제거): fundamentals 로더가 파티션 전체 row를 in-memory predicate 평가 후 spec.sort_by 정렬 + display limit.
- **D2** **totalCount/returnedCount API 포함**: 응답에 full-partition 매칭 총개수 + 표시개수.
- **D3** commit guard = **ROB-426 `snapshot_commit_guard.assert_min_coverage` 재사용 + coverage floor 0.80 + `--allow-partial` 오버라이드**.
- **D4** 산출물 = worktree `auto_trader.rob-429` 문서 + Linear ROB-429 코멘트.

## Proposed Change

### A1 — full-universe provider semantics (migration 0)
`provider.py`: `query_limit = limit or 200` 제거. **full 모드(limit=None)**는 전체 fetch — tvscreener StockScreener `set_range`를 충분히 큰 상한(예: 10_000)으로 설정하거나 range 미지정으로 전체 반환; `rows[:query_limit]` 슬라이스는 limit=None일 때 미적용. `--limit N` diagnostic 모드는 bounded 유지. job `KrFundamentalsSnapshotBuildRequest`: `all_symbols=True` → 전체, 기본 `limit=200`은 diagnostic 명시일 때만.

### A2 — production commit guard (migration 0)
job `run_kr_fundamentals_snapshot_build`: `--commit` 직전 `snapshot_commit_guard.assert_min_coverage(count=would_upsert, universe_count=await active_universe_count(session, market="kr"), market="kr", metric="kr_fundamentals", min_ratio=0.80)` 호출 → `PartialCommitBlocked` 시 commit 차단. `--allow-partial`(또는 `--diagnostic`) 플래그로만 우회. dry-run 결과 dict에 `active_universe_count`/`fetched`/`would_upsert`/`coverage_ratio`/`commit_allowed`/`block_reason` 포함. `BuildRequest`에 `allow_partial: bool=False` 추가.

### A3 — read-path honesty (이미 PR-B에 일부 존재 — 보강)
proxy metric(YoY=3y-avg proxy) 경고는 PR-B의 `EARNINGS_STREAK_SKIP_WARNING` 패턴과 함께 유지/문서화. exact Toss parity 함의 금지(comparable 명시).

### B1 — read-path full-partition 평가 (migration 0)
`kr_fundamentals_tv_screener.py`: `cand_cap = max(limit*8,200)` + market_cap-ordered `.limit(cand_cap)` **제거** → healthy 파티션 전체 row 로드(SELECT WHERE snapshot_date=partition), `_is_kr_toss_common_stock` 필터 + `_passes_thresholds` 전체 평가. `included` 전체에서 `spec.sort_by` desc 정렬 후 `display limit` 적용. (~4250행 in-memory 평가는 저렴; 안전 상한이 필요하면 cand_cap 대신 매우 큰 partition-size 상한.)

### B2 — totalCount/returnedCount (migration 0)
`FundamentalsScreenResult`에 `total_matched: int` 추가(predicate 통과 전체 수, display limit 적용 전). `app/schemas/invest_screener.py` `ScreenerResultsResponse`(extra=forbid)에 `totalCount: int | None = None` + `returnedCount: int | None = None` 추가. `screener_service.build_screener_results`가 fundamentals 경로에서 `totalCount=result.total_matched`, `returnedCount=len(results)` 설정. 프론트 `types/screener.ts` + (선택) UI 렌더는 후속 가능하나 API/스키마엔 포함.

### operator track (runbook, 코드 아님)
`docs/runbooks/` 갱신: dry-run(`--all`, coverage 출력) → 승인 → `--commit --all`(guard 통과) → 11-preset authenticated smoke. 200행/저커버리지 commit은 `--allow-partial` 명시 없이는 차단됨을 문서화.

## Acceptance Criteria
1. `uv run python -m scripts.build_invest_kr_fundamentals_snapshots --all` (dry-run) 가 **full-universe 규모 fetched count**(수천)와 `active_universe_count`/`coverage_ratio`/`commit_allowed` 메타를 출력(200 아님).
2. `--commit`(또는 `--commit --all`)이 coverage < 0.80이면 `PartialCommitBlocked`로 차단; `--allow-partial`로만 우회.
3. read-path가 파티션 전체를 평가 → 동일 partition·predicate에서 **시총 하위 종목도 결과에 포함**(테스트로 소형주 1건 이상 검증); cand_cap에 의한 누락 없음.
4. `/invest/screener` fundamentals 응답에 `totalCount`(predicate 통과 총수) + `returnedCount`(표시 수)가 채워진다.
5. PR #1116 프리셋(`high_yield_value`/`undervalued_breakout`) 포함 모든 fundamentals 프리셋이 tvscreener 스냅샷 경로 유지.
6. exact Toss 종목 일치는 비목표; full coverage·full partition 시 매칭 개수가 Toss 수준(저평가성장주 ~187)에 근접함을 확인 가능(harness/probe).
7. broker/order/watch mutation 0, migration 0, Toss 자동 scraping 0. production `--commit`/backfill은 operator 승인.

## Testing Plan
| Layer | What | Count |
|---|---|---|
| Unit | provider: `all_symbols=True`/`limit=None`가 200으로 안 떨어짐(set_range 상한/전체 fetch), `--limit N` diagnostic는 N | +2 |
| Unit | commit guard: coverage<0.80 → PartialCommitBlocked, `--allow-partial` 우회, dry-run 메타(coverage/allowed/reason) | +3 |
| Unit | read-path full-partition: 소형주(저시총)가 predicate 통과 시 결과 포함, cand_cap 누락 없음, sort+limit | +2 |
| Unit | totalCount/returnedCount: total_matched=predicate 통과수, returnedCount=len(results) | +2 |
| Integration | build_screener_results fundamentals 응답에 totalCount/returnedCount + 채워진 row | +1 |

## Rollback Plan
read-path/provider/job 코드 변경 + 스키마 additive 필드 → revert PR. 스냅샷 데이터는 operator가 이전 파티션 유지(파괴적 변경 없음). totalCount 필드는 optional(None default)이라 consumer 무해.

## Effort
A1 ~1-2h · A2 ~2h · B1 ~2h · B2 ~2h(스키마+service+TS) · runbook ~1h · tests 포함.

## Files Reference
| File | Change |
|---|---|
| `app/services/invest_kr_fundamentals_snapshots/provider.py:95` | `limit or 200` 제거, full=전체 fetch |
| `app/jobs/invest_kr_fundamentals_snapshots.py` | `allow_partial` + commit 전 `assert_min_coverage`(0.80) + dry-run 메타 |
| `scripts/build_invest_kr_fundamentals_snapshots.py` | `--allow-partial`(/`--diagnostic`) 플래그, coverage 출력 |
| `app/services/invest_view_model/kr_fundamentals_tv_screener.py:339-352` | cand_cap 제거 → full-partition 평가; `total_matched` 계산 |
| `app/services/invest_view_model/fundamentals_screener.py` | `FundamentalsScreenResult.total_matched: int` 추가 |
| `app/services/invest_view_model/screener_service.py` | fundamentals 경로에서 totalCount/returnedCount 설정 |
| `app/schemas/invest_screener.py` | `ScreenerResultsResponse.totalCount/returnedCount` (optional) |
| `frontend/invest/src/.../types/screener.ts` | totalCount/returnedCount 인터페이스(렌더는 후속 가능) |
| `app/services/snapshot_commit_guard.py` | 재사용(변경 없음) |
| `docs/runbooks/...` | dry-run→승인→`--commit --all`→smoke |
| `tests/...` | 위 testing plan |

## Out of Scope (follow-up)
- **C: exact 3년평균 TTM parity** — tvscreener 3Y/CAGR 필드로 proxy 개선 또는 DART 파생; 벤더 차이로 exact 불가. honest 유지.
- totalCount의 **프론트 UI 렌더링**(Toss식 "검색된 N개" 표시) — API는 이 스펙에 포함, UI는 후속 가능.
- crypto/US 동일 패턴, scheduler 활성화.

## Related
ROB-428(#1115/#1116, tvscreener KR 스냅샷 + read-path) · ROB-426(snapshot_commit_guard PR2b 재사용) · ROB-280(refresh cadence).

## Appendix — probe 재현
`TvScreenerKrFundamentalsProvider().fetch_rows(limit=6000)` → 4250행; 저평가성장주 predicate 적용 → 181 매칭(≈Toss 187), 179/180 non-top-cap. (full universe 데이터는 존재; 200-cap이 적재를 막았을 뿐.)
