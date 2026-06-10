# ROB-491: get_news 관련성 파이프라인 — 수집·저장·비동기 판정 spec

- 작성: 2026-06-10 (설계 세션, 운영자 결정 반영)
- 상태: 설계 확정, 구현 전
- 관련: ROB-501 (in-process Gemini 제거), ROB-502 (news-ingestor 폐기), ROB-469 (뉴스 경로 취약성), ROB-468 (cleanup-first 게이팅)

## 1. 문제와 운영자 결정

KR `get_news`는 네이버 종목뉴스 단일 소스 on-demand fetch이며, 네이버가 종목 코드에
묶어주는 기사 중 실제 관련성이 낮은 잡뉴스가 다수다 (실측: NAVER -9.7% 급락일에
8건 중 6건이 "에버랜드 판다" 기사). 1차로 구현된 deterministic 키워드 블랙리스트
필터는 사례 후행적이라 노이즈가 보일 때마다 분기만 늘어나는 구조 — 근본 해결이
아니라는 운영자 판단.

**확정된 방향 (2026-06-10 운영자 결정):**

1. auto_trader는 **결정적 수집·저장·상태 표시 레이어**로 유지한다. LLM 관련성
   판정은 auto_trader in-process에서 수행하지 않는다.
   - in-process Gemini는 사용하지 않으며 provider 제거는 ROB-501로 분리.
2. get_news는 수집한 뉴스를 **DB에 저장**하고, 관련성 판정은 **Hermes류 비동기
   Job**이 수행해 결과를 DB에 write-back한다 (기사·종목당 1회 판정, 영속 캐시).
3. 판정 전(pending) 기사는 응답에 **포함하되 상태를 표시**한다 (recall 보존 —
   급락 당일 최신 기사가 pending일 가능성이 높음).
4. 판정 결과는 JSONB 우회가 아닌 **정식 컬럼의 신규 전용 테이블**에 저장한다.
5. 외부 news-ingestor 서비스는 폐기 예정 (ROB-502). `news_articles`의 공급원은
   get_news 직접 upsert (+ 향후 멀티 소스)로 교체된다.

## 2. 아키텍처

```
get_news(symbol, market=kr)                      [동기, MCP 도구]
  1. 네이버 피드 fetch (윈도우 전체)
  2. external_id(URL의 article_id+office_id) set-difference
     → 신규 기사만 news_articles upsert (url unique)
     ※ 피드 순서("앞=최신")는 신뢰하지 않는다 — 클러스터링·후행 태깅·윈도우
       제한 때문에 순서가 아닌 집합 차이로 신규를 감지한다.
  3. (article, market, symbol) 링크를 symbol_news_relevance에 pending으로 insert
     + 결정적 힌트(alias 매치 등) 첨부
  4. 응답: DB 조회 — status=excluded만 제외, confirmed/pending은
     relevance 블록을 달아 published_at desc로 반환

비동기 판정 Job (Hermes류, 레포 밖 LLM 세션)             [비동기]
  1. GET  /trading/api/news-relevance/pending      ← 미판정 링크 조회 (read-only)
  2. LLM 판정 (기사 제목/요약/힌트 기반)
  3. POST /trading/api/news-relevance/ingest/bulk  ← 판정 write-back (token-authed)
```

판정 소유권: **auto_trader 코드는 어떤 기사도 자동으로 제외하지 않는다.**
deterministic 신호(alias 매치, 투자 키워드)는 `hints`로 저장되는 참고 정보일 뿐,
`excluded` 전이는 오직 ingest 엔드포인트를 통한 외부 판정으로만 발생한다.

### 기존 1차 슬라이스(미커밋) 처리

- **폐기**: `_KR_TITLE_NOISE_TERMS` 하드코딩 블랙리스트, `other_symbol_only` 즉시
  제외, MCP 경계에서의 silent 제외 (사례 후행적 분기 코드 전부).
- **재활용**: URL dedupe(set-difference의 기초), alias 사전 매치·투자 키워드
  신호 → `hints` 생성기로 격하 (`symbol_news_relevance.py`는 hint 빌더로 개조).

## 3. 데이터 모델 (migration 1)

### 신규 테이블 `symbol_news_relevance`

(article, symbol) 관계의 전체 수명을 한 행이 소유한다.

| 컬럼 | 타입/제약 | 설명 |
|---|---|---|
| `id` | BigInt PK | |
| `article_id` | FK → news_articles.id, CASCADE | |
| `market` | String(20), check kr/us/crypto | |
| `symbol` | String(40) | unique(article_id, market, symbol) |
| `feed_source` | String(40) | provenance, 예: `naver_item_news` |
| `first_seen_at` | datetime | 피드에서 최초 발견 시각 |
| `status` | String(20), check `pending`/`confirmed`/`excluded`, default `pending` | |
| `relationship` | String(20) nullable, check `direct`/`material_indirect`/`incidental`/`unrelated` | 판정 결과 |
| `relevance` | String(10) nullable, check `high`/`medium`/`low` | 판정 결과 |
| `price_relevance` | String(20) nullable, check `catalyst`/`explainer`/`background`/`none` | 판정 결과 |
| `score` | Float nullable | 판정 confidence |
| `reason` | Text nullable | 판정 근거 (자유 텍스트) |
| `judged_by` | String(100) nullable | 판정 주체/모델 식별자 |
| `judged_at` | datetime nullable | |
| `hints` | JSONB nullable | 결정적 신호 (alias 매치 등). 비권위적 참고용 — 유일한 JSONB |
| `created_at` / `updated_at` | datetime | |

- 인덱스: `(market, symbol, status)`, `(status, first_seen_at)` (pending 조회용).
- `news_articles`는 무변경 재사용 (url unique upsert). **`news_articles.stock_symbol`
  컬럼에 피드 출처를 쓰지 않는다** — 피드에 있었다는 것은 provenance일 뿐이고,
  종목 연결의 단일 소유자는 이 신규 테이블이다.
- `news_article_related_symbols`는 무변경 (news-ingestor legacy, ROB-502에서 정리).

### 상태 전이

```
(fetch 시 insert) → pending
pending → confirmed   (ingest: relationship ∈ {direct, material_indirect} 등 포함 판정)
pending → excluded    (ingest: unrelated / low 판정)
confirmed ↔ excluded  (재판정 허용 — 멱등 upsert, judged_at 갱신)
```

## 4. get_news 계약 변경

- 입력 시그니처 무변경 (`symbol`, `market`, `limit`).
- KR 경로: 위 아키텍처 적용. **US/crypto(Finnhub) 경로는 이번 범위에서 무변경.**
- 응답 `news[]` item: 기존 `source_item` 필드 유지 + `relevance` 블록 추가:
  ```json
  {
    "title": "...", "url": "...", "source": "...", "datetime": "...",
    "relevance": {
      "status": "pending | confirmed",
      "relationship": null, "relevance": null, "price_relevance": null,
      "score": null, "reason": null, "judged_by": null, "judged_at": null,
      "hints": {"alias_match": ["네이버"]}
    }
  }
  ```
- `status=excluded` 기사는 기본 제외. 응답 메타에 `excluded_count` 표기.
  `include_excluded` 옵션은 후속 (1차 비범위).
- 정렬: DB 기준 `published_at desc` (피드 순서 비신뢰). limit은 응답 기사 수에 적용.
- DB에 캐시된 기사가 현재 피드 윈도우 밖이어도 응답에 포함될 수 있다 (의도된
  동작 — 윈도우 밀림 보완).

### 실패 시 동작 (fail-open)

- 네이버 fetch 실패 → **DB 캐시만으로 응답** + 응답 메타에 degraded 표시
  (`fetch_error`). ROB-469에서 지적된 뉴스 경로 폴백 부재의 부분 완화.
- DB 불가 → 기존 동작(저장 없는 on-demand 반환)으로 degrade. 도구가 DB 때문에
  죽지 않는다.

## 5. 판정 Job 계약 (PR2)

### `GET /trading/api/news-relevance/pending`

- 쿼리: `market`(기본 kr), `limit`(기본 50, cap 200), `symbol`(선택).
- 반환: pending 링크 + 기사(제목/요약/source/published_at/url) + hints.
- read-only. 인증은 기존 AuthMiddleware 정책을 따른다.

### `POST /trading/api/news-relevance/ingest/bulk`

- token-authed: `NEWS_RELEVANCE_INGEST_TOKEN` env + 헤더
  `X-News-Relevance-Ingest-Token` (Hermes ingest / research-reports ingest와 동일
  패턴: 토큰 미설정 → 403, 잘못된 토큰 → 401, default-off).
- body: `judgments[]` — `{article_id, market, symbol, relationship, relevance,
  price_relevance, score?, reason, judged_by}`.
- 검증: enum check, (article_id, market, symbol) 링크 존재 확인. 위반은 항목별
  에러 배열로 일괄 반환 (investment_report_create 패턴).
- 멱등: 동일 키 재판정은 overwrite + `judged_at` 갱신.
- status 파생: `relationship=unrelated` 또는 `relevance=low` → `excluded`,
  그 외 → `confirmed`. (파생 규칙은 서버가 소유 — Job이 status를 직접 쓰지 않음.)

### Job 실행 주체

- 레포 밖 Hermes류 LLM 세션(스케줄드 Claude 루틴 또는 operator 수동 실행).
- **auto_trader는 scheduleless** — TaskIQ/cron/Prefect 연결 없음 (기존 방침 동일).
- 런북: `docs/runbooks/news-relevance-judgment.md` (판정 가이드라인 + 호출 절차).

## 6. 테스트

- unit: external_id set-difference upsert(중복/순서 무관/멱등), 상태 전이 파생
  규칙, pending 포함 envelope 계약, fail-open 두 경로(fetch 실패/DB 실패),
  hints 빌더.
- 기존 1차 슬라이스 테스트 10개: 블랙리스트/제외 의존 테스트는 새 계약으로 재작성,
  dedupe·envelope 테스트는 유지·개조.
- ingest 엔드포인트: 토큰 403/401/200, 검증 에러 일괄 반환, 멱등 overwrite.

## 7. PR 분할

- **PR1** — 수집·저장·상태 표시: migration(신규 테이블) + get_news KR 경로 개조
  + 기존 슬라이스 재작업. Job 없이도 동작 (전부 pending 표시 — 현재보다 나쁘지
  않고, 잡뉴스가 silent하게 사라지는 일도 없음).
- **PR2** — 판정 ingest: pending GET + ingest POST + 런북. 활성화는 operator가
  토큰 설정 시.

## 8. 비범위 (이번 설계에서 제외)

- 멀티 소스 수집 (다음금융, 증권사 RSS, DART 연계) — 이슈의 2단계. 같은
  테이블·상태 모델을 provider 추가로 재사용하는 것이 전제.
- 네이버 기사 본문 resolver (판정 정확도 향상용) — 후속.
- US/crypto 경로 적용 — 후속.
- `include_excluded`/debug 옵션 — 후속.
- in-process Gemini 제거 → ROB-501. news-ingestor 폐기 → ROB-502.
- `get_market_news` 공급원 재결정 → ROB-502 §3.

## 9. 안전 경계

- 브로커/주문/감시 mutation 없음. DB 쓰기는 ① get_news의 뉴스 캐시 upsert,
  ② token-authed ingest 엔드포인트의 판정 upsert — 두 경로뿐이며 모두 서비스
  레이어 경유.
- auto_trader 코드가 LLM을 호출하는 경로 없음 (판정은 전적으로 외부 Job 소유).
- 스케줄러 활성화 없음.
