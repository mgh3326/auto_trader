# ROB-398 surface PR1 — `get_symbol_news_mapping` MCP 도구 (설계)

- **Linear**: ROB-398 ([수집] KR market-data collectors …) — surface 슬라이스 1
- **작성일**: 2026-06-09
- **상태**: 설계 확정 (구현 전)
- **선행(완료)**: Slice 1 #1085(news-symbol mapping read-model + 별칭), Slice 2 #1090(kr_market_ranking), Slice 3 #1095(investor_flow) — 전부 머지+배포

## 1. 배경

ROB-398 **데이터 레이어**는 머지+배포됐으나, 사용자향 뉴스 MCP 도구(`get_news`/`get_market_news`/`search_news`)는 여전히 legacy 핸들러라 read-model/매핑이 노출되지 않음 → 이슈가 Done→In Progress로 되돌려짐.

- 실증(2026-06-01 코멘트): 매수/매도 판단을 바꾼 결정적 뉴스 2건이 전부 DB 밖/매핑 안 됨.
- 핵심 갭: `kr_news_symbol_mapping.query_service.get_symbol_news_mapping()`는 계약이 완비됐으나 ① 기본 `article_provider`가 `_empty_provider`(항상 빈 결과), ② **MCP 도구로 미등록**.

## 2. 목표

심볼 → **매핑된 뉴스**(symbol/mapping_source/confidence/is_primary + url + as_of)를 노출하는 신규 read-only MCP 도구 `get_symbol_news_mapping`. **신규 수집/NER 매핑 로직 0** — DB provider 배선 + 도구 등록 + 소폭 계약 확장(url/summary)만.

## 3. 결정 (locked, 브레인스토밍 Q&A)

- **D1** — 신규 도구 `get_symbol_news_mapping`. 기존 `get_news`/`get_market_news` 미변경(회귀 리스크 최소).
- **D2** — 백킹 = `llm_news_service.get_news_articles_with_fallback(symbol, market, hours, limit)` (exact→related→alias + dedup, 이미 존재).
- **D3** — 출력 = **매핑 + url**, summary는 best-effort. `ArticleView`/`MappedArticle`에 url(+summary) additive 추가.
- **D4** — 정직 리포팅: `data_state ∈ {fresh, stale, unavailable}`. 매핑된 뉴스 없으면 **빈 articles + unavailable + warning**(에러/날조 아님).

## 4. 그라운딩 (코드 앵커, 읽기 전용 참조)

- **query service**: `app/services/kr_news_symbol_mapping/query_service.py::get_symbol_news_mapping(symbol, market="kr", hours=24, limit=20, now=None, ttl_hours=…, article_provider=None)` — `article_provider=None`이면 `_empty_provider`(빈 결과). `ArticleProvider = Callable[[symbol, market, hours, limit], Awaitable[Sequence[ArticleView]]]`.
- **contract**: `app/services/kr_news_symbol_mapping/contract.py`
  - `ArticleView(market, stock_symbol, related_rows: tuple[CandidateRow, ...], title, summary, keywords, as_of)`
  - `SymbolNewsMapping(symbol, market, articles: tuple[MappedArticle, ...], freshness: Freshness)`
  - `MappedArticle(as_of, title, mapped_symbols: tuple[MappedSymbol, ...])`
  - `MappedSymbol(symbol, market, mapping_source, confidence, is_primary, matched_term)`
  - `Freshness(overall: "fresh"|"stale"|"unavailable", latest_as_of, …)`
- **backing**: `app/services/llm_news_service.py::get_news_articles_with_fallback(...) -> NewsLookupResult(articles: list[NewsArticle], match_reasons)`. `NewsArticle`은 `related_symbols`(→`NewsArticleRelatedSymbol{article_id, market, symbol, source, matched_term, score, rank, display_name, raw}`) 관계 + `url`/`title`/`summary`/`keywords`/`article_published_at`/`scraped_at` 보유.
- **등록 패턴**: `app/mcp_server/tooling/news_handlers.py::_register_news_tools_impl` (+ `NEWS_TOOL_NAMES`). `get_market_news`/`get_market_issues`가 여기서 등록됨. 신규 도구는 뉴스 도구 옆에 등록 + `NEWS_TOOL_NAMES` + AVAILABLE 등록 set에 추가.

## 5. 아키텍처 (3 컴포넌트, 각 단일 책임)

**(1) DB ArticleProvider** (신규): `build_db_article_provider(session) -> ArticleProvider`. 반환 클로저 `(symbol, market, hours, limit)`:
- `get_news_articles_with_fallback(symbol, market, hours, limit)` 호출 → `NewsLookupResult`.
- 각 `NewsArticle` → `ArticleView`: `related_rows`=각 기사의 `NewsArticleRelatedSymbol`→`CandidateRow` 매핑, `url`=`article.url`, `as_of`=`article.article_published_at or article.scraped_at`, `title`/`summary`/`keywords` 전달.
- ⚠️ **related_symbols 로딩**: `article.related_symbols` 관계를 세션 밖에서 접근하면 `DetachedInstanceError`. 따라서 provider는 세션 내에서 **확정 로드**해야 함 — `selectinload(NewsArticle.related_symbols)`로 eager-load 하거나, 반환된 article_id 묶음으로 `NewsArticleRelatedSymbol`을 핸들러 세션에서 명시 조회. (구체 방식은 플랜에서 확정.)

**(2) MCP 핸들러** (신규 `app/mcp_server/tooling/fundamentals/_symbol_news_mapping.py`): `handle_get_symbol_news_mapping(symbol, market="kr", hours=24, limit=20)`:
- 세션 획득(`AsyncSessionLocal`, screener_snapshot_tool 패턴) → `build_db_article_provider(session)` → `get_symbol_news_mapping(symbol, market, hours, limit, article_provider=provider)` → `SymbolNewsMapping` → 응답 dict 포맷(§6).

**(3) 등록**: `news_handlers.py`(또는 fundamentals)에서 `get_symbol_news_mapping` 등록 + `NEWS_TOOL_NAMES`/AVAILABLE set 추가.

**계약 확장 (additive, default None → 하위호환, migration 0)**:
- `ArticleView.url: str | None = None`
- `MappedArticle.url: str | None = None`, `MappedArticle.summary: str | None = None`
- `query_service`가 `MappedArticle` 생성 시 `url=view.url, summary=view.summary` 전달.

## 6. 응답 shape

```json
{
  "symbol": "035420",
  "market": "kr",
  "data_state": "fresh",
  "latest_as_of": "2026-06-09T01:23:00+09:00",
  "articles": [
    {
      "title": "네이버클라우드, 젠슨황 GTC 언급",
      "url": "https://n.news.naver.com/...",
      "summary": null,
      "as_of": "2026-06-09T01:23:00+09:00",
      "mapped_symbols": [
        {"symbol": "035420", "market": "kr", "mapping_source": "naver_code",
         "confidence": 1.0, "is_primary": true, "matched_term": null}
      ]
    }
  ],
  "warnings": []
}
```

## 7. 정직 리포팅

- `data_state` = `freshness.overall`. `stale`/`unavailable`일 때 한글 warning 추가.
- 매핑된 뉴스 0건 → `articles: []` + `data_state="unavailable"` + warning("해당 종목에 매핑된 뉴스가 없습니다 …"). **에러 아님, 빈 행 위조 아님.**
- `confidence`/`is_primary`/`mapping_source`는 read-model에서 파생(naver_code=primary/고신뢰, candidate/ner=낮음). 추정/날조 금지. summary는 없으면 null(KR Naver upstream 한계 정직 노출).

## 8. 테스트 (read-only, migration 0)

- **provider 매핑(단위)**: `NewsArticle`(related_symbols + url + published_at) → `ArticleView`(related_rows/url/as_of). 페이크/픽스처로 `get_news_articles_with_fallback` 주입.
- **계약 passthrough(단위)**: url/summary 있는 `ArticleView` → `get_symbol_news_mapping`이 `MappedArticle.url`/`.summary` 전파.
- **핸들러 응답 shape(단위)**: 주입 provider로 mapped_symbols/data_state/url/summary 필드 검증.
- **통합(db_session)**: `NewsArticle` + `NewsArticleRelatedSymbol` seed → 도구가 confidence/primary 포함 매핑 + url 반환.
- **empty→unavailable(단위/통합)**: 빈 articles + data_state=unavailable + warning, 에러 없음.

## 9. 비범위 (별도 슬라이스/이슈)

- **get_market_news symbol enrich** — 다음 슬라이스.
- **get_news KR dedup/timestamp/summary** — dedup만 싸고 timestamp·summary는 Naver 리스트 소스가 date-only+본문 없음(upstream 한계, 더 풍부한 소스 필요) → 별도.
- **search_news 등록 위치 확인** — Explore가 NEWS_TOOL_NAMES에서 못 찾았으나 operator는 실행됨(등록이 다른 곳) → 별도 확인.
- **paper_001 MCP 재시작/승격** — operator 작업.
- 신규 데이터 수집/NER 변경 0. **migration 0**. broker/order/watch/order-intent mutation 0.

## 10. Acceptance criteria

- [ ] `get_symbol_news_mapping` MCP 도구 등록(AVAILABLE_TOOL_NAMES 포함, 스키마: symbol/market/hours/limit).
- [ ] symbol에 매핑된 뉴스가 있으면 `mapped_symbols`(symbol/mapping_source/confidence/is_primary/matched_term) + title + url + as_of 반환, `data_state ∈ {fresh, stale}`.
- [ ] 매핑 없으면 `articles: []` + `data_state="unavailable"` + warning (에러/날조 아님).
- [ ] DB provider가 `get_news_articles_with_fallback` 기반(exact→related→alias+dedup).
- [ ] `ArticleView.url` + `MappedArticle.url`/`.summary` additive(default None, 기존 테스트 무영향).
- [ ] migration 0, broker/order/watch mutation 0. 단위 + 통합 테스트 green, ruff clean.

## 11. 안전 경계

read-only. DB write/backfill 없음(조회만). scheduler/cron 무접근. broker/order/watch/order-intent 무접근. 기존 매핑 로직(query_service) 재사용 — 신규 NER/매핑 휴리스틱 도입 없음.
