# ROB-510 — get_news US/crypto(Finnhub) 신뢰성 + DB 파이프라인 통합 spec

- **이슈**: [ROB-510](https://linear.app/mgh3326/issue/ROB-510) `[data] get_news US(Finnhub) 간헐 타임아웃 — 재시도/백오프 + 짧은 TTL 캐시`
- **상태**: 설계 확정 (2026-06-11, user 승인)
- **migration**: 0 (스키마는 ROB-491에서 이미 market-generic)

## 1. 문제

`get_news(market="us")`(Finnhub)가 세션 중 간헐적으로 3~4회 연속 타임아웃해
catalyst-first 의사결정 게이트를 막는다 (6/09·6/10 이틀 연속 재현, 40-에이전트
풀-북 리뷰에서 NVDA/AMZN 4/4 타임아웃 등). 현재 US/crypto 경로는:

- **단발 호출, 재시도 없음** — `symbol_news_service.py` US/crypto 분기는
  `asyncio.wait_for(..., timeout=5.0)` 한 번이 전부. Finnhub SDK 기본
  타임아웃(10s)보다 래퍼(5s)가 먼저 끊는다.
- **DB 미경유** — KR(ROB-491)과 달리 저장/판정/degraded 폴백이 전혀 없어
  실패 시 빈손(`status="error"`)으로 끝난다.

## 2. 설계 결정 (user 확정)

| 결정 | 내용 |
|---|---|
| DB 구조 | Redis TTL 캐시 대신 **KR과 동일한 DB 저장 구조** (news_articles + symbol_news_relevance) |
| 판정 흐름 | US/crypto 기사도 **pending → 외부 판정** (KR과 동일; 자동 제외 금지 원칙 유지) |
| 신선도 게이트 | **없음** — 매 호출 fetch (10~15분 캐시는 catalyst-first 목적과 충돌). DB는 판정 입력 + 실패 시 degraded 폴백 용도 |
| 스코프 | **US + crypto 둘 다** (`_fetch_finnhub` 공유 경로, 스키마 `'crypto'` 이미 허용) |
| 타임아웃 | 시도당 상향(5s → 8s, env 조절) + 재시도 동의 |

이슈 acceptance의 "TTL 캐시 히트"는 신선도 게이트 생략 결정에 따라
"실패 시 DB degraded 폴백"으로 재해석한다 (Linear에 코멘트로 남길 것).

## 3. 현재 코드 (근거)

- `app/services/symbol_news_service.py`
  - `fetch_symbol_news` (286~) — KR 분기는 fetch → `_kr_persist_and_load` →
    DB 캐노니컬 서빙 + degraded 폴백 (299~371). US/crypto 분기는 단발
    `wait_for(5s)` 직행 (373~402).
  - `_kr_persist_and_load` (205~248) — upsert + `load_symbol_news` +
    ROB-506 enqueue. KR 하드코딩.
  - `_maybe_enqueue_judgment` (181~202) — `market="kr"` 하드코딩 (194).
  - `_stored_to_article` (153~178) — `provider="naver"`, `market="kr"`,
    `_naver_external_id` 하드코딩.
- `app/services/symbol_news_store.py`
  - `upsert_kr_feed_articles` (73~153) — `market: "kr"` values 하드코딩
    (93, 126). 그 외 `list_pending`/`apply_judgment`/`load_symbol_news`는
    이미 market 파라미터.
- `app/services/finnhub_news.py` — `fetch_news_finnhub` (48~91): US는
  `company_news(symbol)`, **crypto는 `general_news("crypto")` (심볼 키 아님)**.
  재시도/타임아웃 파라미터 없음 (SDK 기본 10s).
- `app/jobs/news_relevance_judgment.py`, `app/tasks/news_relevance_judgment_tasks.py`,
  `app/services/news_relevance_judgment_client.py`, `app/routers/news_relevance.py`
  — 모두 이미 market 파라미터 지원. 변경 불요(런북 외).
- `app/models/news.py` (`market` 제약 `'kr'|'us'|'crypto'`),
  `app/models/symbol_news_relevance.py` (동일 제약, uq `(article_id, market, symbol)`).
- `app/services/symbol_news_relevance.py` `build_relevance_hints` — 비-KR은
  alias-match 폴백이 이미 존재.

## 4. 변경 사항

### 4.1 Finnhub fetch 신뢰성 (`app/services/finnhub_news.py`)

`fetch_news_finnhub`에 재시도+백오프+시도당 타임아웃 내장 (다른 소비처도
자동 수혜):

- **시도당 타임아웃**: `asyncio.wait_for(asyncio.to_thread(fetch_sync), timeout=<시도당>)`.
  기본 8.0s, env `FINNHUB_NEWS_TIMEOUT_S`.
- **재시도**: 최대 3시도 (env `FINNHUB_NEWS_MAX_ATTEMPTS`, 기본 3),
  지수 백오프+지터 (0.5s → 1s 기준, `openclaw_client.py`의 tenacity
  `AsyncRetrying` 패턴 차용).
- **재시도 대상**: `TimeoutError`, 네트워크성 예외, `FinnhubAPIException`
  5xx/429. **비재시도**: 4xx(429 제외), `ValueError`(키 미설정), `ImportError`.
- 최악 총 소요 ≈ 8+0.5+8+1+8 ≈ 25.5s — MCP 도구 응답으로 bounded.
- 설정은 `app/core/config.py` settings에 추가, `env.example` 갱신.

`symbol_news_service`의 US/crypto 분기에서는 기존 외곽
`asyncio.wait_for(5.0)`를 제거한다 (시도당 타임아웃이 재시도를 무력화하지
않도록 타임아웃 소유권을 provider로 이동). KR Naver 경로의 `timeout_s=5.0`
동작은 불변.

### 4.2 DB persist 일반화 (`app/services/symbol_news_store.py`)

- `upsert_kr_feed_articles` → `upsert_feed_articles(db, market, symbol, items, *, feed_source)`로
  일반화 (market values 하드코딩 제거). KR 호출부는
  `market="kr", feed_source=KR_FEED_SOURCE` 유지.
- feed_source 상수 추가: `FINNHUB_COMPANY_FEED_SOURCE = "finnhub_company_news"`
  (us), `FINNHUB_GENERAL_FEED_SOURCE = "finnhub_general_news"` (crypto).
- `FeedArticleInput`에 `summary: str | None = None` 추가 →
  `NewsArticle.summary`에 저장 (Finnhub은 summary 제공; degraded 폴백 시
  복원용). KR은 None 그대로.
- hints는 기존 `build_relevance_hints(symbol, market, title)` 그대로 호출
  (비-KR alias 폴백 사용; US 키워드 사전 추가는 스코프 외).

### 4.3 US/crypto persist-and-load (`app/services/symbol_news_service.py`)

- `_kr_persist_and_load` → `_persist_and_load(symbol, market, provider, feed_source, fetched, limit, fetched_at)`로
  일반화. `_stored_to_article`도 provider/market 파라미터화
  (external_id: naver는 `_naver_external_id`, finnhub은 `_url_hash`).
- `_maybe_enqueue_judgment(market, symbol, new_pending)` — market 스레딩
  (us/crypto pending도 ROB-506 worker가 판정).
- `fetch_symbol_news`의 US/crypto 분기를 KR과 동일 구조로 재작성:
  1. `_fetch_finnhub` (내부 재시도) try/except — 실패 시 `fetched=None` + `fetch_error`
  2. `_persist_and_load` — 성공 fetch upsert 후 DB 캐노니컬 상태 서빙
     (excluded 제외 + `excluded_count`, pending은 relevance 블록 표시)
  3. fetch 실패 + DB에 기사 있음 → `degraded=True` + `fetch_error` (KR 동일 의미론)
  4. fetch 실패 + DB 빈손 → 기존처럼 `status="error"`
  5. DB 불가(폴백) → fetch 결과를 `_PENDING_RELEVANCE` 오버레이로 직접 서빙
     (KR 343~371 동일 패턴; hints는 market 전달)
- crypto 주의: `general_news` 피드라 같은 기사가 여러 심볼 링크로 생길 수
  있음 — URL unique + `(article_id, market, symbol)` uq가 의도대로 처리.
  심볼별 관련성은 판정이 결정 (crypto가 판정 가치 최대).

### 4.4 envelope 영향 (`app/mcp_server/tooling/fundamentals/_news.py` — 코드 변경 없음)

- US/crypto 응답에 KR처럼 `relevance` 블록과 의미 있는 `excluded_count`가
  생긴다 (additive).
- 신선 fetch 항목은 `raw_by_url`의 source_item이 그대로 나가
  sentiment/related 보존. degraded 폴백으로 DB에서 복원된 항목은
  title/url/source/datetime/summary만 보장 (sentiment 미영속 — 허용 한계,
  런북 명기).
- handler의 degraded 표면(`degraded: true` + `fetch_error`)은 기존 코드
  재사용 — 변경 없음.

### 4.5 ROB-506 worker / 런북

- 코드 변경: enqueue market 스레딩(4.3)만으로 us/crypto가 기존 worker에
  합류 (job/task/client/라우터는 이미 market-generic).
- `docs/runbooks/news-relevance-judgment.md`에 us/crypto 섹션 추가
  (판정 기준은 market 무관 동일 contract, crypto는 general 피드라
  unrelated 비율이 높을 것으로 예상).

## 5. 안전 경계

- 브로커/주문/감시 mutation 없음. migration 0.
- 모든 DB 쓰기는 `symbol_news_store` 경유 (ROB-491 원칙).
- auto_trader 코드는 어떤 기사도 자동 제외하지 않음 — status는 서버 파생
  (`derive_status`)만.
- enqueue는 fail-open, store 불가 시 도구는 fetch 결과로 degrade (KR 동일).
- Finnhub 재시도는 429에 백오프로만 대응 — 쿼터 소진 시 공격적 재시도 금지
  (max 3시도 bounded).

## 6. Acceptance (이슈 대비)

| 이슈 acceptance | 본 설계 |
|---|---|
| 단발 타임아웃이 자동 재시도로 흡수 | 4.1 재시도+백오프 |
| 동일 심볼 반복 조회가 TTL 캐시 히트 | **재해석**: 신선도 게이트 생략 (user 결정) — 매 호출 최신 fetch, 실패 시에만 DB 서빙 |
| 연속 실패 시 명시적 degraded 신호 + stale 캐시 폴백 | 4.3 — `degraded: true` + `fetch_error` + DB 기사 반환 |

## 7. 테스트

- `tests/services/test_finnhub_news.py` (신규): 타임아웃 1회 → 재시도 성공,
  3회 연속 실패 → 예외 전파, 4xx 비재시도, 백오프 호출 횟수.
- `tests/services/test_symbol_news_service.py` (확장): US/crypto
  persist-and-load 경로, fetch 실패 → degraded DB 폴백, DB 불가 →
  pending 오버레이 직접 서빙, enqueue market 스레딩, KR 무회귀.
- `tests/services/test_symbol_news_store.py` (확장): `upsert_feed_articles`
  market='us'/'crypto' 멱등성, summary 영속, KR 기존 테스트 무회귀.
- `tests/mcp_server/tooling/test_get_news_envelope.py` (확장): US envelope에
  relevance/excluded_count 표면, degraded 표면.

## 8. 스코프 외 (후속)

- US 키워드/alias 사전 (`_KR_EXTRA_INVEST_HINT_TERMS`의 US 대응물)
- 신선도 게이트 (필요 시 별도 이슈로 재논의)
- crypto 브리핑 스코어러(`crypto_news_relevance_service.py`)와의 통합 —
  본 건과 무관한 인메모리 경로
