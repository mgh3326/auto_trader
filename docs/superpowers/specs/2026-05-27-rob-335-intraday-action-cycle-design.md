# ROB-335 — `/invest/reports` 매일 장중 액션 사이클 MVP (설계)

- **Linear**: ROB-335 (High, Backlog) — auto_trader Trading Decision Workspace
- **Date**: 2026-05-27
- **Branch base**: `c31168b3` (ROB-332 / PR #982 머지 직후 `main`)
- **Related**: ROB-332 (run-card citation, 재구현 금지), ROB-336 (Phase 2 US/crypto/NXT), ROB-337 (watch 매수가격 산정)

## 1. 문제 정의

`/invest/reports`는 "예쁜 리포트 UI"가 아니라 **Hermes가 장중에 본 입력/정책/판단을 고정해 매일 반복 가능하게 남기는 감사 로그/재현 컨텍스트**여야 한다. 그러나 최근 리포트는 `items=[]`, `review_sections=null` 또는 smoke/draft 성격으로 끝나 실제 질문에 답하지 못하면서도 technical success로 보였다.

ROB-322가 만든 `build_review_sections()`는 `decision_bucket`이 non-null인 item만 projection하므로, item이 없으면 sections가 비고 `why_no_action` diagnostics가 채워진다는 보장도 없다. 즉 **빈 리포트가 성공으로 보이는 경로가 실재한다.**

이 MVP는 KR/KIS live 중심으로 **빈 리포트 성공을 금지**하고, operator가 한 번 실행하면 다음 4개 질문에 반드시 답하는 장중 ActionPacket을 항상 생성한다.

1. 오늘 보유종목 중 팔거나 줄일 게 있는가?
2. 오늘 보유종목 중 유지/추가매수 금지/추가매수 검토 대상은 무엇인가?
3. 오늘 신규 후보가 있는가? 없다면 왜 없는가?
4. 데이터가 부족하면 어떤 source가 부족해서 판단을 못 했는가?

## 2. 확정된 설계 결정

| 결정 | 내용 | 근거 |
|---|---|---|
| **A. 버킷** | ActionPacket sub-verdict는 locked 5값 `decision_bucket` 위의 sub-label. enum 변경/마이그레이션 없음 | ROB-301 decision_bucket 5-value locked, ROB-322 5-section UI 호환 유지 |
| **B. 영속화** | sub-verdict는 persisted report item의 `evidence_snapshot["action_verdict"]`(JSON)에 저장. ActionPacket section은 read-time projection | 입력(item+evidence+bundle)이 모두 persisted → 결정론적 재계산 = 재현 보장. 마이그레이션 불필요 |
| **C. 분류기 경계** | sub-verdict는 결정론 evidence 규칙이 부여, Hermes push가 refine/override. in-process LLM 없음 | ROB-287 boundary: `/invest/reports`는 Gemini/OpenAI/Grok/Hermes in-process 호출 금지 |
| **C′. 결정론 범위** | 결정론은 정직한 verdict만 직접 부여: `data_gap`/`keep`/`no_add`/`sell_review`. `trim_review`/`add_review`는 Hermes push 또는 명시 정책에 유보 | fake 방향성 신호 금지 ("빈 리포트 성공 금지"와 동일 정신) |
| **D. ROB-337 경계** | `limit_wait`은 evidence 상태만 표면화. 목표 매수가격 계산은 ROB-337로 이양 | watch 매수가격 모델 중복 선점 방지 |
| **E. 슬라이싱** | PR1 백엔드 코어, PR2 프론트 surface. 마이그레이션 없음 → PR0 불필요 | ROB-322 PR1/PR2 선례 |

### 버킷 매핑 (read-time projection)

```
decision_bucket (locked 5)        ActionPacket sub-verdict
--------------------------        -----------------------
new_buy_candidate            ←     buy_review / limit_wait
open_action                  ←     sell_review / trim_review / add_review
completed_or_existing        ←     keep / no_add
risk_watch                   ←     watch_only  (+ risk_reviews)
deferred_no_action           ←     rejected / data_gap
```

## 3. 아키텍처

### §3.1 Report intent 분리 + non-empty invariant

- 기존 `policy_version="intraday_action_report_v1"` 문자열을 공식 intent discriminator로 승격. 별도 컬럼/마이그레이션 없이 generation request + diagnostics JSON에 intent를 기록.
- `intent="intraday_action"` 리포트는 **non-empty invariant**를 강제. 강제 지점은 `app/services/action_report/snapshot_backed/generator.py`의 `classify_items()` 이후 `ingest()` 이전.
- guard는 결과가 비면 **실패가 아니라 보강**: 구조적 섹션(`no_new_buy_candidates`, `no_action_reason`, `data_gaps_for_next_cycle`)을 항상 합성해 `items=[]` / `review_sections=null`이 성공으로 나가지 않게 한다.
- smoke/draft intent는 이 invariant에서 면제 → smoke와 실제 장중 리포트 경로가 명확히 분리.

### §3.2 ActionPacket = read-time projection

신규 모듈 `app/services/investment_reports/action_packet.py` (ROB-322 `review_sections.py`와 동일 view-layer 패턴). persisted item + diagnostics에서 read-time으로 조립.

```
ActionPacket
├── held_actions:             sub-verdict별 그룹 (sell_review/trim_review/keep/no_add/add_review)
├── new_buy_candidates:       sub-verdict별 그룹 (buy_review/limit_wait/watch_only/rejected)
├── risk_reviews:             risk_watch 버킷 item
├── no_action_reason:         why_no_action diagnostics 재사용 (data_insufficient/stale_gated/real_no_action)
└── data_gaps_for_next_cycle: 부족/stale source 목록
```

- sub-verdict는 각 item의 `evidence_snapshot["action_verdict"]`에서 읽음.
- `decision_bucket`이 None인 (pre-ROB-308) item은 ROB-322와 동일하게 projection에서 제외 → fallback `items`/`item_groups`에 유지.

### §3.3 보유종목 결정론 분류기

`app/services/action_report/snapshot_backed/` 에 held-symbol classifier 추가. 입력 = portfolio 스냅샷(KIS primary holdings) + 심볼별 quote/orderbook evidence. **모든 KIS 보유종목**에 verdict 부여:

```
quote/orderbook unavailable           → data_gap        (방향성 판단 안 함)
KIS primary 아님 / sellable_qty = 0   → keep            (매도 불가, advisory)
sellable + actionable quote           → sell_review     (reviewable 표면화, 추천 아님)
그 외 신선·정상                        → keep            (기본값)
trim_review / add_review              → (빈 슬롯) Hermes push가 채움
```

- **Toss/manual은 `reference_holdings`로만** — 절대 KIS sellable로 승격하지 않음 (ROB-297 가드 재사용, 회귀테스트로 고정).
- `user_id=None`이면 portfolio collector가 이미 `primary_source="none"`/`freshness="unavailable"` 반환 → held_actions 대신 "portfolio unavailable" data_gap으로 fail-closed.

### §3.4 신규 후보 결정론 분류기 + screener freshness 게이트

candidate_universe collector 결과에 freshness 게이트 적용:

```
stale-only screener candidate         → 신규매수 후보에서 제외 + 사유 기록
fresh candidate + actionable quote    → buy_review
fresh candidate + 애매한 evidence     → watch_only
quote unavailable                     → data_gap
명시적 제외 사유                       → rejected
limit_wait                            → evidence 상태만 표시 (목표가 계산은 ROB-337)
```

- 후보가 0개거나 전부 stale이면 `no_new_buy_candidates` 섹션에 "신규 후보 없음 + 이유"를 항상 남김 (§3.1 invariant가 보장).

### §3.5 data_gap / market·news 연결

- `data_gaps_for_next_cycle` = bundle `coverage_summary.missing_sources` + 심볼별 unavailable quote + stale screener를 종합.
- market issues/news context(이미 수집됨)를 ActionPacket 판단 근거(`evidence_snapshot`)로 연결. 새 수집기 없음 — 기존 news 스냅샷 참조만.

### §3.6 프론트엔드 surface (PR2)

ROB-322 5-section UI를 확장해 `/invest/reports` 최신 화면에 4개 헤더를 명시 렌더: **오늘의 보유 액션 / 신규 후보 / 리스크 / 데이터 부족**. sub-verdict는 chip으로 표시. ActionPacket projection을 read-model payload로 노출하고 ROB-275/evidence viewer 호환을 유지.

## 4. 안전 경계 (Non-goals)

- 주문 preview/submit/cancel/modify 금지.
- broker/order/watch/order-intent mutation 금지 — DB row 무변화를 테스트/smoke evidence로 증명.
- recurring scheduler/Prefect deployment 활성화 금지 (operator-triggered MVP까지만).
- 전 경로 `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` 게이트 유지.
- Naver/Toss Chrome remote-debug persistence는 별도 후속 — core MCP/DB/read-model 기반으로 먼저 답이 나오게 한다.
- US/crypto/NXT 확장 = ROB-336 Phase 2. 매수가격 모델 = ROB-337.
- `user_id` 없으면 KIS live portfolio는 fail-closed/unavailable — 임의 기본 사용자 대체 금지.
- ROB-332 `validated_run_card` ingest/citation 재구현 금지. 기존 `evidence_snapshot["run_card"]` 동작 보존, run-card verdict/`is_pass_stamp`를 buy/sell 강도로 사용 금지 (optional 보조/감사 evidence로만).

## 5. Acceptance criteria

- 실제 KR/KIS live intraday report 생성 시 `items=[]`로 성공하지 않는다.
- 신규 후보가 없거나 stale-only여도 리포트에 "신규 후보 없음"과 이유가 남는다.
- 보유종목은 최소한 `sell_review/trim_review/keep/no_add/add_review` 중 하나로 분류된다 (결정론 기본은 `keep`, 방향성은 evidence/Hermes).
- quote/orderbook/spread/liquidity unavailable인 종목은 무리하게 buy/sell로 분류하지 않고 data_gap 또는 wait/review로 분류한다.
- stale screener candidate가 "오늘 신규매수 후보"로 노출되지 않는다.
- KIS live와 Toss/reference holdings의 권위가 섞이지 않는 regression test가 있다.
- broker/order/watch/order-intent 관련 DB row 변화가 없음을 테스트 또는 smoke evidence로 남긴다.
- ROB-275/evidence viewer 호환성이 깨지지 않는다.

## 6. 테스트 전략

- **단위:** held classifier, candidate classifier, ActionPacket projection, sub-verdict→bucket 매핑.
- **회귀:**
  1. empty candidate/empty action → no-action/data-gap 섹션 emit (빈 성공 금지).
  2. stale-only screener candidate → live buy 후보에서 제외.
  3. `user_id=None` → fail-closed (portfolio unavailable data_gap).
  4. KIS live vs Toss/reference 권위 미혼합.
- **안전:** broker/order/watch/order-intent DB row 무변화를 테스트/smoke evidence로 기록.
- **로컬 smoke:** read-only KR/KIS live advisory 리포트 1건 생성 후 bundle + UI payload 검사.
- 참고: `frontend/invest` vitest는 `--pool=forks`로 실행 (threads pool flaky). baseline 5건 pre-existing 실패 존재.

## 7. PR 슬라이싱

- **PR1 (백엔드 코어):** §3.1 intent/invariant + §3.3 held classifier + §3.4 candidate classifier + §3.5 data_gap + §3.2 ActionPacket projection + §6 백엔드 테스트.
- **PR2 (프론트):** §3.6 surface 렌더 + read-model payload 노출 + 프론트 테스트.
- 마이그레이션 없음 → PR0 불필요. PR1 머지 후 fresh `main`에서 PR2 시작.

## 8. 미해결/후속

- `trim_review`/`add_review`의 명시 정책 규칙(비중/미실현손익 임계치)은 본 MVP 범위 밖 — Hermes push로 채우고, 필요 시 별도 이슈에서 결정론 정책화.
- 매수가격(목표가) 산정 = ROB-337.
- US/crypto/KR NXT 확장 = ROB-336.
- recurring scheduler/Prefect 등록 = 후속 (operator-triggered MVP 검증 후).
