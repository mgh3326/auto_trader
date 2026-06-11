# Runbook: `invest_screener_snapshots` (ROB-170)

## 1. Purpose

`invest_screener_snapshots` is a per-symbol, per-trading-day table that precomputes price-derived metrics (`consecutive_up_days`, `week_change_rate`, `latest_close`, `change_amount`, `change_rate`) for the `/invest/screener` `consecutive_gainers` preset.

**Read path:** `/invest/api/screener/results?presetId=consecutive_gainers` calls `_enrich_consecutive_up_days` which reads from `invest_screener_snapshots` first (snapshot-first). If a snapshot is fresh, OHLCV fetch is skipped. If missing/stale, the existing on-demand OHLCV path is used transparently (ROB-168 fallback).

**Write path:** Operator-driven CLI or manually enqueued TaskIQ task only (no recurring scheduler — see §5). Both default to dry-run/no writes; persistence requires an explicit commit flag.

---

## 2. Operator Workflow

### Build snapshots (default: dry-run, no writes)

```bash
# KR — preview top 20 active universe symbols
uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

# KR — full active universe, dry-run (RECOMMENDED before any --commit)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all

# KR — full active universe, persist (REQUIRES OPERATOR APPROVAL)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

# US — full active universe, persist
uv run python -m scripts.build_invest_screener_snapshots --market us --all --commit

# Specific symbols (small surgical refresh)
uv run python -m scripts.build_invest_screener_snapshots \
    --market kr --symbol 005930 --symbol 000660 --commit
```

`--dry-run` (default) prints payloads without writing. `--commit` persists rows
via `INSERT ON CONFLICT DO UPDATE`. `--all` iterates the full active universe in
`--batch-size` chunks (default 200), committing per batch when `--commit` is set.
`--all` is mutually exclusive with `--symbol` and `--limit`.

**Operator approval gating:** never run `--all --commit` against production
without explicit human approval citing dry-run evidence. The recommended
sequence is:

1. `--all` (no commit) → review log of total/built counts and a sample of payloads.
2. Inspect coverage before commit: `curl /invest/api/screener/snapshots/coverage`.
3. Wait for explicit "approved to commit" from a reviewer.
4. `--all --commit` → re-check coverage; expect `dataState="fresh"`.

> **ROB-512:** `_LOOKBACK`이 10→30으로 늘어 `closes_window` 기반 RSI14 enrichment가
> 가능해졌다. 효력은 **새 빌드 파티션부터** — 배포 후 `--market kr --all --commit`
> 재빌드 전까지 기존 파티션의 RSI는 계속 null(정상·정직한 동작)이다.

---

## 3. Coverage Check

### Via CLI (read-only, no writes)

```bash
uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
```

Output includes: universe size, coveringToday, stale, missing, lastComputedAt, dataState.

### Via HTTP endpoint

```bash
curl "http://localhost:8000/invest/api/screener/snapshots/coverage?market=kr"
curl "http://localhost:8000/invest/api/screener/snapshots/coverage?market=us"
```

Returns 200 even when the table is empty (`snapshotsCoveringToday=0`, `dataState="missing"`).

---

## 4. Fallback Semantics

`ScreenerFreshness.dataState` on the screener response indicates snapshot read quality:

| `dataState` | Meaning |
|-------------|---------|
| `fresh`     | All rows served from snapshots dated today with `≥5` closes |
| `partial`   | Rows served but `closes_window` has 2–4 entries (week_change_rate not computable) |
| `stale`     | Snapshot exists but is older than today's trading date or computed >36h ago |
| `missing`   | No snapshot rows found; all data came from on-demand OHLCV fallback |
| `fallback`  | Mixed: some rows used snapshots, ≥1 row used on-demand fallback |

If the table is empty, the screener response is byte-equivalent to the ROB-168 baseline — only `freshness.dataState` differs (it becomes `"missing"`).

---

## 5. Scheduler — Deferred

**No recurring scheduler entry is active.** The table is filled on operator
demand. A TaskIQ wrapper exists for controlled manual enqueueing:

- task name: `build_invest_screener_snapshots`
- module: `app.tasks.invest_screener_snapshot_tasks`
- default behavior: `commit=false`, so the task returns counts, warnings, and a
  small sample without database writes
- write behavior: `commit=true`, which must only be used after dry-run evidence
  and explicit operator/reviewer approval

The TaskIQ task is intentionally schedule-free; it is a queueable activation
surface, not recurring automation. Recurring automation (e.g. nightly
TaskIQ/Prefect job) requires:

- A separate Linear ticket
- At least one or two days of operator-run smoke evidence (coverage diagnostic
  output captured before/after `--all --commit`)
- Explicit reviewer approval citing that evidence

Do not introduce a recurring scheduler in the same PR as the snapshot read-path
wiring or the operator-CLI changes — they are intentionally split so the
scheduler activation can be reviewed against a known-stable manual baseline.

---

## 6. Safety Boundary

- **Read/model/UI/data-layer only.** No broker, order, watch, or order-intent mutations.
- The CLI defaults to `--dry-run`. Accidental invocation without `--commit` is a no-op.
- Migration is table-create only — no `ALTER` of existing tables.
- The repository's `upsert` is the only write path; direct `INSERT/UPDATE/DELETE` is forbidden.

---

## 7. US Activation Procedure (ROB-204)

All steps below require a reviewer-approved Linear thread on ROB-204. No production write
occurs until Phase 4; Phases 0–3 are fully read-only.

### Phase 0 — Pre-flight (read-only)

```bash
# Baseline US coverage diagnostic — confirm dataState=missing, universe count
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
```

### Phase 1 — Populate `is_common_stock` (one-time)

```bash
# Dry-run: print row delta proposed
uv run python -m scripts.sync_us_common_stock_flags

# Commit (requires operator approval — single transaction, additive column only)
uv run python -m scripts.sync_us_common_stock_flags --commit
```

Expected: ~3,000–4,000 rows `is_common_stock=true`; ~7,000–9,000 rows `is_common_stock=false`.

### Phase 2 — Bounded US dry-run (no DB writes)

```bash
# Common-stocks-only, dry-run, full active common-stock universe
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only

# Smaller sampled dry-run (first 50 common stocks)
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --common-stocks-only --limit 50
```

### Phase 3 — Reviewer approval round (Linear)

Post the dry-run summary (symbols_resolved, snapshots_built, skipped, snapshot_date_distribution,
warnings sample, first 10 rows) to Linear ROB-204 and await explicit "approved to commit" reply.

### Phase 4 — Bounded US commit

```bash
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only --commit

# Re-check coverage
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
```

### Phase 5 — Spot-check `/invest/screener?market=us`

- UI: open `/invest/screener`, toggle 미국, confirm `consecutive_gainers` returns >0 rows.
- API: confirm `freshness.dataState="fresh"` and non-empty `results[]`.

### US valuation Finnhub fallback (ROB-434, default-off)

`market_valuation_snapshots` US builds can backfill valuation fields that yahoo
`.info` left null (operator "ROE rows 0" / Invalid Crumb) from Finnhub
`company_basic_financials`. **Disabled by default.** To enable for a build:

1. Set `FINNHUB_API_KEY` (operator secret manager — never commit).
2. Set `MARKET_VALUATION_FINNHUB_FALLBACK_ENABLED=true`.
3. Run `uv run python -m scripts.build_market_valuation_snapshots --market us --common-stocks-only --concurrency 4` (dry-run first). The summary prints `finnhub backfill` per-field counts and `non-null field coverage`.

Without the key or flag the build is byte-identical to today (yahoo-only,
fail-closed). `source` stays `yahoo`; per-field provenance is in
`raw_payload._field_provenance`. Finnhub free tier ~60/min — keep `--concurrency`
modest. Fallback fires only on a per-symbol gap, never per-symbol unconditionally.

---

## 8. Prefect Deployment (DEFERRED — ROB-204)

A Prefect flow `invest_screener_snapshots_us` is importable from
`app/flows/invest_screener_snapshots_us_flow.py`. The deployment manifest is intentionally
not added in the ROB-204 PR. Activation is a separate operator action after Phase 4–5
stability across at least 24 hours and an explicit reviewer approval.

Intended schedule: `30 21 * * 1-5` UTC (≈17:30 ET, ~30 min after the regular US session close).
The flow body honors `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` (default `False` → dry-run).

---

## 9. ROB-276 — 쌍끌이 매수 (Toss screenId=18 parity)

### Overview

- **Preset**: `double_buy` (KR only, snapshot-only, read-only).
- **User-facing**: 카드 이름 `쌍끌이 매수`, description `기관과 외국인이 동시에 매수하는 종목`.
- **Replaces UI label**: 이전에 `쌍끌이 매수`로 잘못 라벨링되어 있던 거래량 기반 preset(`high_volume_momentum`)은 ID 자체가 `kr_high_volume_surge` 로 변경되었고 이름은 `거래량 급증` 으로 분리됨. 구 ID `high_volume_momentum` 는 더 이상 존재하지 않으며 해당 ID 로 들어온 요청은 `unknown preset` 응답을 받음.

### Filter logic (locked: Interpretation A)

```text
market = kr
investor_flow_snapshots.foreign_net > 0
investor_flow_snapshots.institution_net > 0
COALESCE(invest_screener_snapshots.change_rate, 0) >= 0
ORDER BY change_rate DESC NULLS LAST, symbol ASC, source ASC
```

- 최신 `investor_flow_snapshots` partition 과 최신 `invest_screener_snapshots` partition 을 symbol 로 join.
- `_is_kr_toss_common_stock` 가드로 ETF/ETN/우선주/SPAC/펀드성 종목 제외.
- 다중 `source` (e.g. `naver_finance` + `kis`) 가 존재할 때 `ORDER BY source.asc()` 로 결정론적 dedupe.

### Decision 1 lock 근거 (2026-05-20, Task 0)

- Live DB 검증은 환경 제약으로 수행하지 못함 (docker/colima 미사용, host Postgres 권한 부재).
- Toss reference 캡처는 ROB-276 본문의 5개 심볼 (`011000, 439960, 083500, 042520, 042420`) 뿐이라 캡처 size 가 < 50% 의미를 갖기에 부족.
- Plan 의 safer-fallback rule ("둘 다 < 50% 커버 또는 동률 → A lock") 에 따라 A 채택.
- 구조적 근거: `InvestorFlowSnapshot.double_buy` 가 이미 `foreign_net > 0 AND institution_net > 0` 의 derived 컬럼이라 별도 backfill 없이 재사용 가능.
- Live A/B 검증은 Task 4 의 diagnostic (`--interpretation both`) 으로 실제 DB + 더 큰 Toss capture 확보 후 수행. B 가 명백히 우세하면 후속 PR 에서 lock 을 전환 (helper 본체만 교체; preset metadata/freshness 로직은 유지).

### Freshness — 의존성 분리

- **price 스냅샷 stale**: row 의 `_screener_snapshot_state == "stale"` (price_snapshot_date ≠ flow_snapshot_date) → warning `"시세 스냅샷이 직전 영업일 기준이라 일부 데이터가 1일 지연되었습니다."`
- **flow 스냅샷 stale**: 모든 row 의 `flow_snapshot_date < today_trading_date(market)` (KST) → warning `"수급 스냅샷이 직전 영업일 기준이라 외인/기관 정보가 1일 지연되었습니다."`
- **둘 다 missing**: 최신 partition 자체 부재 → `dataState=missing` + warning `"수급 또는 시세 스냅샷이 아직 적재되지 않아 쌍끌이 매수 후보를 표시할 수 없습니다."`
- KST/trading-date 산정은 `app/services/invest_screener_snapshots/freshness.today_trading_date` 사용; `dt.date.today()` 직접 사용 금지.

### Diagnostic

```bash
uv run python -m scripts.diagnose_invest_screener_toss_parity \
  --market kr \
  --preset double_buy \
  --interpretation both \
  --toss-symbols-file /path/to/toss_ref.json \
  --limit 80
```

- `--interpretation` ∈ `{a, b, both}` (default `both`).
- Toss reference 는 **항상 파일 기반** (CSV/JSON/plain-symbol-per-line). HTTP fetch 금지.
- 출력 JSON 에 `interpretationA`, `interpretationB` 블록이 항상 emit (skip 모드에서는 `null`).
- 각 블록: `count`, `overlapCount`, `missingFromAutoTrader`, `extraInAutoTrader`.
- `lockedInterpretation: "A"` 와 Decision 1 note 가 payload 에 항상 포함.
- B 해석은 diagnostic 전용 (current vs previous trading day self-join). production 에 wire 되지 않음.

### Safety boundary (이번 PR)

- **No DB migrations** (모델/마이그레이션 추가 없음).
- **No new ingestion job / TaskIQ task / Prefect deployment / scheduler activation.** 기존 `investor_flow_snapshots`, `invest_screener_snapshots` 적재 경로만 사용.
- **No broker / order / watch / order-intent mutation path.**
- **No Toss runtime scraping.** Toss 데이터는 항상 수동 캡처 파일.
- **Snapshot-only routing.** generic provider fallback 없음 — 스냅샷 부재 시 명시적 `dataState=missing` + warning.

### Known gaps

- Decision 1 lock 은 구조적 (safer-fallback) — live overlap 수치로 empirically 검증되지 않음. Section §9 의 diagnostic 으로 후속 검증 후, 필요 시 follow-up PR 에서 B 전환.
- 다중 `investor_flow` source 가 활성화되면 `source.asc()` tiebreak 가 결정론을 보장하지만, source 별 net 값 분기가 큰 경우 데이터 product 차원 의사결정이 필요.
- 구 ID `high_volume_momentum` URL/북마크 호환성 없음 — frontend preset list 진입이 주된 경로라 영향 작음.

## 10. Crypto Activation Procedure (ROB-443 Phase 0)

코인 스크리너(`crypto_high_volume`/`crypto_oversold`/`crypto_momentum`)는 **읽기 경로가 이미 스냅샷(`invest_crypto_screener_snapshots`)을 우선**하지만, 스냅샷을 채우는 **스케줄(Prefect flow)이 없어** 매번 라이브 tvscreener 로 폴백한다(`freshness.dataState=missing`, `source=screening_service`). Phase 0 는 빌드를 채워 KR/US 처럼 snapshot-backed 로 전환한다.

> 코인은 별도 테이블/CLI 사용: `invest_crypto_screener_snapshots` + `scripts/build_invest_crypto_screener_snapshots.py` (equity 의 `build_invest_screener_snapshots.py` 와 다름).

### Phase 0 — Pre-flight (read-only)

```bash
# 전체 Upbit 유니버스 dry-run (DB write 없음, 행 수/샘플 확인)
uv run python -m scripts.build_invest_crypto_screener_snapshots --all
```

### Phase 0 — Populate (REQUIRES OPERATOR APPROVAL)

```bash
# 전체 Upbit KRW 유니버스 persist (dry-run 증거 + 승인 후에만)
uv run python -m scripts.build_invest_crypto_screener_snapshots --all --commit
```

빌드 후 라이브 확인: 코인 프리셋 응답의 `freshness.primary.source` 가 `invest_crypto_screener_snapshots`, `dataState` 가 `fresh` 여야 한다(`_CRYPTO_MIN_FRESH_ROWS=20` 이상 + 오늘 KST 파티션). 미만이면 `partial`, 부재면 `missing`(라이브 폴백).

### Phase 0 — Prefect flow (스케줄, 등록은 별도)

- 플로우: `app/flows/invest_crypto_screener_snapshots_flow.py` (`invest_crypto_screener_snapshots_flow`).
- 쓰기 게이트: `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` (KR/US 와 공유). 기본 `False` → dry-run/rollback. operator 가 Prefect worker env 에서 켜야 persist.
- **배포 등록은 이 PR 에 포함되지 않음** (US flow 와 동일 안전 게이트). 등록은 `robin-prefect-automations` 에서 paused-by-default 로 추가 후 unpause 승인.

### Safety boundary (crypto)

- **No DB migration** (Phase 0; 파생 컬럼은 Phase 1 ROB-443).
- read-only 발굴. broker/order/watch/order-intent mutation 0. DB write 는 `InvestCryptoScreenerSnapshotsRepository` 의 upsert 만.
- snapshot commit/스케줄 활성화는 operator 승인 게이트. 매수 신호 아님(스크리닝 컨텍스트).


## 11. US market_valuation / us_fundamentals 일일 갱신 (ROB-508)

### Overview
US 펀더멘털 프리셋 4종(`high_yield_value`/`undervalued_growth`/`profitable_company`/`undervalued_breakout`)이 직전 거래일 기준 `dataState: fresh`로 서빙되도록 Prefect 일일/주간 갱신을 수행한다.

- **Market Valuation Snapshots (US)**: 일일 갱신 (`flows/auto_trader/market_valuation_snapshots_us.py`)
  - **스케줄**: 화–토 08:30 KST (US 정규장 마감 후, 06:10 KST US invest screener 빌드와 분리)
  - **환경 게이트**: `MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED` (기본값 `False` → dry-run, `True` 시 DB persist)
  - **수동 빌드 fallback 커맨드**:
    ```bash
    uv run python -m scripts.build_market_valuation_snapshots --market us --all --common-stocks-only --commit
    ```
- **US Financial Fundamentals Snapshots**: 주간 갱신 (`flows/auto_trader/us_fundamentals_snapshots.py`)
  - **스케줄**: 매주 일요일 09:00 KST
  - **수동 빌드 fallback 커맨드**:
    ```bash
    uv run python -m scripts.build_us_fundamentals_snapshots --all --commit
    ```

### Freshness 확인 방법
스크리너 응답의 `freshness.primary.snapshotDate` 가 직전 거래일인지, `dataState`가 `"fresh"` 인지, 펀더멘털 preset rows의 `priceLabel` 이 `"-"`가 아닌 실제 가격(close)으로 표시되는지 확인한다.
