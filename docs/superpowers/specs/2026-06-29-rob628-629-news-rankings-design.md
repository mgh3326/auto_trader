# ROB-628 + ROB-629 — MCP 뉴스/랭킹 in-context 사용성 설계

- **작성일**: 2026-06-29
- **이슈**: [ROB-628](https://linear.app/mgh3326/issue/ROB-628) (High), [ROB-629](https://linear.app/mgh3326/issue/ROB-629) (Medium)
- **상태**: 설계 승인됨 (user 2026-06-29), 구현 플랜 작성 단계
- **근거**: 4-에이전트 코드 그라운딩 워크플로(wf_27c2340a) — 모든 주장 file:line 검증 완료

## 배경

2026-06-29 개장 전 "오늘 시장 예측" 세션에서 MCP 뉴스/랭킹 도구 3종이 in-context 사용 불가로 드러남:

- `get_market_news` / `get_market_issues` → 응답 52k~54k자, 토큰 한도 초과로 파일 덤프 → 메인 컨텍스트에서 못 읽고 서브에이전트 우회.
- `get_market_news` news[] 전부 `stock_symbol=null` → 보유종목 촉매를 MCP로 못 뽑아 CDP 네이버 per-stock 페이지 우회.
- `get_top_stocks(ranking_type=foreigners)` → 의미 불명확·잡주만·market_cap 전부 null·외인 net 필드 부재 → CDP 우회.

운영 디시플린: **워크어라운드보다 소스 수정.**

## 범위 & 구조

- **1 플랜 문서, 2 PR.** 둘 다 마이그레이션 0, 순수 read-layer. 파일 0겹침이라 독립 배포·리뷰.
  - **PR-A = ROB-628**: P1 응답크기 정상화 + P2 보유종목 촉매 sweep 도구.
  - **PR-B = ROB-629**: 외인 랭킹 (명명 필드 + buy/sell split + market_cap/필터).
- PR-B는 **장중 KIS 검증 게이트** 보유(머지 전 operator가 장중 실 호출로 확인).

## 그라운딩 핵심 사실 (검증됨)

- 응답 폭증 원인 = `_article_to_dict`(`news_handlers.py:38`)가 **untruncated `summary`** 임베드(본문 `article_content` 아님; RSS는 summary≈전문). 추가 증폭: briefing_filter시 3×limit 오버페치(`:87-90`), included를 `news[]`와 `briefing_sections[]`에 **이중 임베드**(`_briefing_sections_to_dict:51-71`), `excluded_news` 무제한(`:205`). `get_market_issues`는 limit이 issue 개수만 cap하고 issue당 member 기사 전부 임베드(`news_issue_clustering_service.py:313-333,475`).
- ⚠️ 이슈 주장 정정: (1) "FULL article bodies" → 실제 `summary` 필드. (2) "get_top_stocks엔 cursor 있음" → **틀림**, cursor 없음. 실제 페이지네이션 선례는 `screen_stocks_snapshot`(ROB-465).
- briefing이 `stock_symbol`을 미populated DB 컬럼서 직출력(`news_handlers.py:41-42`) → 전부 null. 결정적 심볼태깅 스택(`news_entity_matcher.match_symbols_for_article:141`)은 **이미 존재**하고 웹 /invest feed·get_market_issues가 사용 중이나 briefing MCP만 미배선. ⚠️ KR alias 사전은 **15개뿐**(삼성/하이닉스/NAVER/기아 O, **한화에어로 012450 X**).
- ⚠️ "보유종목 촉매 CDP 강제"는 과장: `get_news(symbol)`는 이미 종목별 lean 네이버 헤드라인+relevance 반환(`fundamentals/_news.py:26-77`).
- KIS `foreigners` = `foreign_buying_rank`→TR `FHPTJ04400000` foreign-institution-total **가집계**(`domestic_market_data.py:168-184`), net-buy-top 하드코딩(`FID_RANK_SORT_CLS_CODE='0'`). market_cap 항상 null = **구조적**(엔드포인트 페이로드에 market-cap 필드 없음; unit test가 `hts_avls` 위조로 은폐 `test_mcp_top_stocks.py:920`). 외인 net qty/amount는 페이로드에 있으나 **mislabel**: `frgn_ntby_qty`→`volume`, `frgn_ntby_tr_pbmn`→`trade_amount`(`analysis_screening.py:122,124-126`).
- ⚠️ ROB-626은 랭킹 소스 아님(per-symbol 네이버 스크레이프, 크로스심볼 랭킹 불가). 옵션 enrichment로만 재사용.

---

## §A1. ROB-628 P1 — 응답 shape 정상화 (`get_market_news` + `get_market_issues`)

### 컴포넌트

1. **공유 절단 유틸** — `news_radar_service._plain_text`(HTML strip + ellipsis 절단, `news_radar_service.py:60-70`)를 `app/services/news_text.py`로 추출, 두 직렬화 경로(`_article_to_dict`, `MarketIssueArticle` 빌드)가 공유. 단일 책임·독립 테스트 가능.

2. **`detail` 파라미터** (`headline_only | summary | full`, **기본 `summary`**):
   - `get_market_news`(`news_handlers.py:225`)·`get_market_issues`(`:257`) 시그니처에 추가, `_article_to_dict`(`:23-48`)·`build_market_issues`→`MarketIssueArticle`(`news_issue_clustering_service.py:313-333`)로 스레딩.
   - `summary` = title/url/source/published_at/symbol-tags + `summary` **~240자 절단**.
   - `headline_only` = summary 제거(필드 drop).
   - `full` = 무절단(명시 opt-in escape hatch).

3. **구조 개선 (param 무관, 항상 적용)** — 최고 ROI, 신규 param 0:
   - `_briefing_sections_to_dict`(`:51-71`): full article dict 재임베드 중단 → **section당 {id, title, count, article_ids, relevance}만**. 기사 본체는 `news[]`에서 참조(최대 중복 증폭원 제거; 포매터의 `BriefingItem.as_dict`가 이미 lean 형태).
   - `excluded_news`: **limit으로 cap** + `excluded_total` 카운트(`:115,152-158,163,205`).
   - briefing_filter 오버페치(3×limit, `:87-90`)는 랭킹 품질 위해 유지하되 출력에만 cap.

4. **하드 응답크기 상한** (안전망):
   - `_get_market_news_impl` 말미(`:186-206`)·`build_market_issues` model_dump 후(`:262-267`) `json.dumps` 크기 측정.
   - 상한(기본 **8k chars**, 상수) 초과 시 뒤쪽 item drop → 기존 `status`/`degraded_reason` 컨벤션(`news_handlers.py:170-184`)으로 **명시 신호** + `truncated_for_size: true` 플래그 + 남은/전체 카운트. **silent drop 금지**(ROB-502 원칙).

5. **페이지네이션 = follow-up 슬라이스** (이번 PR 제외, 플랜에 명시):
   - 절단+구조개선+상한으로 거의 해결되므로 후순위.
   - 구현 시 `screen_stocks_snapshot`(ROB-465) offset 패턴 이식(`get_news_articles`는 이미 offset 지원 `llm_news_service.py:156,207`; `build_market_issues`는 ranked `meaningful` 리스트 슬라이스).

---

## §A2. ROB-628 P2 — 보유/symbols 촉매 sweep 도구

### 신규 MCP 도구 (가칭 `get_holdings_news` — 네이밍 플랜에서 확정)

- **입력**: `symbols: list[str] | None`(생략 시 현재 보유종목 = KIS+토스+수동+크립토 해소), `limit_per_symbol: int`. market은 심볼별 자동 라우팅.
- **출력**: 종목별 **lean 행** = `{symbol, name, market, news: [{title, url, source, published_at, relevance}]}`.
  - KR = 네이버 헤드라인(`symbol_news_service._fetch_naver`), US/크립토 = finnhub(summary+sentiment 포함).
  - `relevance` = `symbol_news_relevance`(ROB-491) verdict(`price_relevance=catalyst` 등).
- **재사용**: `symbol_news_service.fetch_symbol_news`(`:333`) + `symbol_news_store.load_symbol_news`(`:259`). 신규 인제스트 없음.
- **N 바운드**: symbols **~30 cap** + 동시성 제한(기존 `investor_flow_snapshots` builder Semaphore=4 패턴). `fetch_symbol_news`는 DB-backed fail-soft.
- **보유종목 해소**: 기존 portfolio/holdings 서비스 재사용(KIS holdings + Toss + manual_holdings + 크립토).
- **`get_news` advisory 문구 명확화**: 이미 대부분 존재(`news_handlers.py:188-193,212-223`) → 종목별 촉매 surface임을 명시하는 minor 수정.

### Non-goal (follow-up)

- KR `get_news` 인라인 summary/sentiment 합성 = in-proc LLM 금지(런타임 LLM 경계) + ROB-491 판정 Job(scheduleless) 의존. `relevance.price_relevance`를 촉매 신호로 사용.
- briefing 리스트 자체의 심볼 태깅(전 universe 커버)은 별도 follow-up.

---

## §B. ROB-629 — 외인 랭킹 (`get_top_stocks` foreigners)

1. **명명 필드**: `_map_kr_row`(`analysis_screening.py:117-137`)를 **foreigners 전용 매핑으로 분기** → `foreign_net_qty`(`frgn_ntby_qty`)·`foreign_net_amount`(`frgn_ntby_tr_pbmn`)를 **명시 키로 표면**. 기존 `volume`/`trade_amount`에 외인값 stuffing **중단**(다른 랭킹과의 의미 오염 제거).
   - ⚠️ **back-compat 노트**: foreigners 랭킹에서 `volume`/`trade_amount`를 읽던 소비자는 영향(원래 mislabel이었음). 의도적 correctness 변경 — 플랜에 명시.

2. **ranking_type split**: `foreign_net_buy` / `foreign_net_sell` 추가. `foreign_buying_rank`의 `FID_RANK_SORT_CLS_CODE`를 0(buy)/1(sell) 파라미터화(`domestic_market_data.py:168-184`, `fluctuation_rank` 방향 패턴 `:123-166` 미러). `'foreigners'`는 `foreign_net_buy` **alias 유지**(back-compat).
   - `supported_combinations`·dispatch 갱신(`analysis_tool_handlers.py:89-103,136-138`), 도구 description에 의미 명시.
   - **net_buy는 현 동작(저위험)**. **net_sell은 장중 검증 게이트**(아래 §D).

3. **market_cap backfill + 유동성 필터**:
   - backfill: `invest_kr_fundamentals_snapshots.market_cap`(`app/models/invest_kr_fundamentals_snapshot.py:72`) bounded top-N join(폴백 `kr_symbol_universe.shares_outstanding`×price). ROB-512 enrichment 배선 패턴 재사용. 단일 배치 쿼리(per-row 금지).
   - **유동성 필터 기본 ON**(잡주 제외, override param 제공). 필터 결과 빈/스냅샷 부재 시 **status/degraded로 정직 신호**(market_cap null 유지, fabricate 금지).

4. **세션 가드**: 가집계 off-hours fake-0 → `data_state` 가드(`analysis_tool_handlers.py:180-204` gainers/losers ROB-464 패턴 재사용).

### 재사용 안 함

- ROB-626 `build_confirmed_block`(per-symbol 네이버)는 랭킹 1차 소스로 부적합. top-N enrichment(외인소진율 등)로만 **선택적** follow-up.

---

## §C. 에러/degraded 신호 (공통 원칙)

모든 응답 축소 — 절단, hard-cap item drop, 유동성 필터, 세션 stale — 는 기존 `status`/`degraded_reason` 컨벤션으로 **명시 신호**. **silent drop / 데이터 fabricate 절대 금지**(ROB-502/ROB-626 디시플린).

## §D. 테스트 & 검증

- **단위 (PR-A)**: detail 3단계 shape, 절단 경계(~240자), de-dup(briefing_sections에 full dict 0), excluded cap + excluded_total, 하드상한 drop+degraded 신호, full 무절단. sweep 종목 fan-out·N 바운드·동시성·holdings 해소·크로스마켓 라우팅·DB fail-soft.
- **단위 (PR-B)**: foreigners 명명필드(foreign_net_qty/amount), ranking_type split dispatch, `FID_RANK_SORT_CLS_CODE` 0/1, market_cap join + 폴백, 유동성 필터 ON + 빈결과 degrade, 세션 가드. 기존 `test_kis_rankings.py:238-239`·`test_mcp_top_stocks.py:920` 갱신(위조 hts_avls 제거).
- **장중 검증 게이트 (operator, PR-B 머지 전)**: KRX 장중 실 KIS 호출로 (a) `foreign_net_sell`(RANK_SORT='1')이 실제 순매도 상위 반환 확인, (b) 유동성 필터가 잡주 제외·대형주 표면 확인, (c) 명명 필드 값 정합성. 플랜에 체크리스트.
- 마이그레이션 0. `make lint` + `ty check app/` + full suite green.

## 의존성 / 순서

- PR-A·PR-B 상호 독립(파일 0겹침). ROB-626 무의존.
- 권장 순서: **PR-A 먼저**(High·게이트 없음·일일 최대 고통) → **PR-B**(장중 검증 게이트라 KRX 오픈 타이밍). 병렬 worktree 가능.
- 페이지네이션·briefing 전-universe 태깅·get_news KR summary/sentiment·ROB-626 외인소진율 enrichment = **follow-up**(별도 Linear 후보).
