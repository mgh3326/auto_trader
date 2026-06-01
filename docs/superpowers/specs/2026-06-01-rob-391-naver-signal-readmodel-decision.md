# ROB-391 — Naver/Toss CDP 신호 read-model 승격: 설계 결정 노트

- **이슈:** ROB-391 (오케스트레이션 ROB-394의 5번, 마지막)
- **날짜:** 2026-06-01
- **상태:** **설계 결정 — 이번엔 구현 안 함(fail-open stub 유지) + 미래 read-model schema 제안**
- **base:** `origin/main` `61e90ead` (ROB-392 Slice A 머지 직후)

## 결정 요약 (TL;DR)

운영자가 9222 CDP로 직접 읽는 Naver/Toss 신호(외인소진율·투자자 매매동향·종목토론 심리·셀사이드 코멘트)를
번들 evidence로 승격하는 것은 **방향성이 옳다**. 다만 ROB-391은 이슈 본문이 명시한 대로 **"최소 수집 범위 +
durable read-model schema를 먼저 제안하고, 자동 수집이 위험·과하면 fail-open stub을 유지한 채 설계
issue/comment로 중단"** 한다.

**이번 결정:**

1. **fail-open stub을 그대로 유지한다** (`naver_remote_debug` / `toss_remote_debug` / `browser_probe` →
   `unavailable`). 코드 변경·DB migration 없음.
2. 아래 **durable read-model schema + operator-ingestion contract**를 미래 구현 이슈를 위해 제안한다.
3. **자동 in-request CDP 수집은 의도적으로 미연결**로 둔다 — 그 이유를 §4에 명시.

근거: 안전 경계(승인 없는 prod migration 금지, operator CDP를 request path에 묶지 않기, Naver/Toss를
KIS/account authority와 동급 tier로 취급 금지)와 비용/위험 대비 가치. Naver 신호는 **supplementary /
low-trust cross-check**이며, 운영자 결정의 보조일 뿐 KIS/계좌 권위를 대체하지 않는다.

## 1. 현황 (근거)

* `app/services/action_report/snapshot_backed/collectors/optional_stubs.py` — 세 collector
  (`NaverRemoteDebugStubCollector` / `TossRemoteDebugStubCollector` / `BrowserProbeStubCollector`)가
  모두 `_FailOpenStubBase`를 상속, 매 호출 `unavailable` 반환. reason:
  `"...probe is operator-driven only; automated probe is intentionally not wired"`.
* `app/schemas/investment_snapshots.py:25-40` — `naver_remote_debug` / `toss_remote_debug` /
  `browser_probe` snapshot kind가 **이미 SnapshotKind literal에 존재**. → 승격 시 enum/migration 불필요
  (read-model 테이블만 신규).
* 번들 진단은 `data_quality_audit.gaps`에 `external_cross_check_unavailable`(severity info)로만 기록.

## 2. 제안: durable read-model schema (미래 구현 이슈)

기존 ingestion foundation 패턴과 정합(아래 §3). 신규 테이블 1개로 시작.

### 2.1 최소 수집 범위 (이슈 §제안과 일치)

* **외인소진율** (foreign ownership exhaustion %, 예: NAVER 36.94%)
* **일자별 외국인/기관 순매수** (daily foreign / institutional net-buy, 예: 05.29 기관 +877,348)
* **종목토론 상위 감성** (discussion board top sentiment — pos/neg/neu 분류 + 표본 수)
* **셀사이드 코멘트 헤드라인** (sell-side comment headline + 출처 + 코멘트 일자; 저작권상 **헤드라인/요약만**,
  본문 전문 금지 — research_reports ROB-140 가드와 동일 원칙)

### 2.2 제안 테이블 `naver_stock_signal` (개념 스키마)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | PK | |
| `symbol` | str (idx) | DB 심볼 형식(`to_db_symbol`) |
| `trading_date` | date (idx) | 신호 기준 거래일 |
| `foreign_ownership_pct` | numeric \| null | 외인소진율 |
| `foreign_net_buy` | bigint \| null | 외국인 순매수(주) |
| `institution_net_buy` | bigint \| null | 기관 순매수(주) |
| `discussion_sentiment` | enum(pos/neg/neu) \| null | 종목토론 상위 감성 |
| `discussion_sample_count` | int \| null | 감성 표본 수(신뢰도 가늠) |
| `sellside_headlines` | JSONB | `[{headline, source, comment_date}]` (헤드라인/요약만) |
| `source` | str | `"naver_cdp"` 등 provenance |
| `collected_at` | datetime | operator 수집 시각 |
| `raw_excerpt` | JSONB \| null | redact 후 최소 원문 발췌(검증용; 전문 금지) |

* **Unique**: `(symbol, trading_date, source)` — upsert 키.
* **freshness**: ROB-389에서 정립한 `expected_kr_baseline_date` 기준 경과일로 `data_state` 산출(재사용).
* `toss` 신호도 동일 테이블에 `source="toss_cdp"`로 수용하거나 별도 테이블 — 1차는 Naver만.

## 3. 제안: ingestion / read 경로 (request path와 분리)

기존 ROB-128(market_events) / ROB-140(research_reports) ingestion foundation과 동일 구조:

```
operator-local CDP(9222, read-only)
   │  (운영자가 수동/배치로 실행)
   ▼
scripts/ingest_naver_stock_signals.py   ← operator CLI (--file payload.json [--dry-run])
   │  (token-authed HTTP ingest도 가능: research_reports bulk 패턴)
   ▼
NaverStockSignalRepository.upsert        ← 모든 write는 repository 경유
   ▼
naver_stock_signal (durable read-model)
   ▲
NaverRemoteDebugCollector.collect        ← read-model을 READ (stub 대체)
   │  데이터 있으면 supplementary evidence snapshot(naver_remote_debug),
   │  없으면 기존처럼 fail-open unavailable
   ▼
report bundle (low-trust cross-check tier)
```

* **operator-ingestion이 분리점**: CDP 스크레이핑은 operator-local에서 read-only로 수행되고, 결과만
  read-model에 적재. **report 생성 request path는 read-model을 읽기만** 한다(CDP를 직접 구동하지 않음).
* **default-disabled / no scheduler**: market_events/research_reports처럼 TaskIQ 반복 스케줄 없음. 운영
  recurrence는 `robin-prefect-automations`에 paused-by-default로 두고 unpause는 승인 게이트.
* **저작권**: 셀사이드 코멘트는 헤드라인/요약만(전문 reject) — research_reports 스키마 가드 재사용.

## 4. 왜 이번엔 자동 수집을 미연결로 두는가 (stub 유지 근거)

* **request path 비결합 원칙**: 이슈가 "operator-driven CDP scraping을 request path에 직접 묶지 않는다"를
  명시. in-request 자동 CDP는 fragile(렌더 타이밍·로그인·DOM 변화)·operator-env 의존이라 리포트 생성의
  신뢰성을 떨어뜨림.
* **승인 없는 migration 금지**: read-model 테이블은 alembic migration을 동반하는데, ROB-394 안전 경계는
  "승인 없는 prod DB migration"을 금지. 신규 테이블 도입은 별도 승인·이슈가 적절.
* **tier 오인 위험**: Naver/Toss는 low-trust cross-check. 번들에 freeze되면 Hermes/운영자가 KIS/계좌
  권위와 혼동할 여지 → 승격 시 tier 라벨링(`trust="low"`, `authority="supplementary"`)을 read-model과
  collector에 함께 설계해야 안전. 이는 단독 이슈에서 신중히.
* **비용 대비 가치**: 이번 NXT 리포트 1건에서 결정적이었으나, 반복적 결정성(이슈 contract의 승격 기준)은
  표본이 더 필요. 먼저 schema를 고정하고 operator ingest로 몇 회 적재해 결정성을 축적한 뒤 collector 연결이
  순서상 안전.

## 5. 후속 (권고)

* **신규 이슈 (ROB-391 Slice B 또는 별도)**: §2 테이블 + repository + operator CLI ingest + read-only
  query/router + runbook (ROB-128/140 패턴). default-disabled, migration은 PR 포함하되 operator가 별도
  `alembic upgrade`.
* 그 다음 이슈: `NaverRemoteDebugStubCollector`를 read-model을 읽는 실 collector로 교체(데이터 없으면
  fail-open 유지) + tier 라벨링(`trust=low` / `authority=supplementary`).
* `toss_remote_debug` / `browser_probe`는 Naver 검증 후 동일 패턴 확장 또는 stub 유지.

## 6. 안전 경계 (이번 결정 준수)

* read-only. broker/order/watch/order-intent mutation 없음.
* **코드/스키마 변경 없음** — stub 유지, 신규 테이블·migration 없음.
* operator CDP는 read-only·operator-local, request path 비결합(제안 단계에서 명시).
* Naver/Toss를 KIS/account authority와 동급 tier로 취급하지 않음(승격 시 low-trust 라벨 필수).
* scheduler/Prefect 등록·활성화 없음.

## 7. ROB-394 시퀀스 종결

ROB-388~392는 각각 PR/검증/잔여가 정리됨(아래). ROB-391은 본 결정 노트로 **설계 제안 + stub 유지**가
명확해졌으므로, ROB-394 오케스트레이션의 완료 조건("각 이슈에 PR/검증/잔여 blocker 정리, 다음 실행 순서가
더 이상 불명확하지 않음")을 충족한다.
