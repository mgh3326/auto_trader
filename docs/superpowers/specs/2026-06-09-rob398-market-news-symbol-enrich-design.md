# ROB-398 surface PR2 — `get_market_news` symbol enrich (설계)

- **Linear**: ROB-398, surface 슬라이스 2
- **작성일**: 2026-06-09
- **상태**: 설계 확정 (구현 전)
- **선행/독립**: PR1 #1176 (`get_symbol_news_mapping`) — 본 PR은 **origin/main 기준 독립** 브랜치

## 1. 배경

ROB-398 데이터 레이어(Slices 1-3)는 머지됐으나 `get_market_news`가 `news_article_related_symbols` 매핑을 노출하지 않아 각 기사의 종목 매핑이 비어 있음(특히 `browser_naver_mainnews` 시황 기사는 `stock_symbol=null`). PR1은 `get_symbol_news_mapping`(심볼→뉴스) 신규 도구를 추가했고, 본 PR은 `get_market_news`(시장→뉴스)의 **각 기사에 `mapped_symbols`를 추가**한다.

## 2. 목표

`get_market_news` 각 뉴스 아이템에 `mapped_symbols`(symbol/market/mapping_source/confidence/is_primary/matched_term)를 추가. 기존 `stock_symbol`/`stock_name` 보존(additive). 기존 `resolve_article_symbols`(순수, main) 재사용 + full NER.

## 3. 결정 (locked, 브레인스토밍 Q&A)

- **D1** — `get_market_news` 각 기사에 `mapped_symbols` 추가.
- **D2** — 브랜치 **독립 off main** + 공유 배치 로더 `load_related_rows_by_article_ids` 신설(PR1의 private `_load_related_rows`와 일시 병존; 추후 DRY는 별도). 스택-PR 고통 회피.
- **D3** — **full resolve** = persisted related_rows + **live NER**(`resolve_article_symbols(ner_matches=...)`). mainnews도 이름 매칭으로 매핑.
- **D4** — scope = `get_market_news` only. `search_news`/`get_news` KR = 별도.

## 4. 그라운딩 (코드 앵커)

- `app/mcp_server/tooling/news_handlers.py::_get_market_news_impl` (≈73-169): `articles, total = await get_news_articles(...)` → 여러 분기(crypto briefing included/excluded, us/kr briefing, plain)에서 `_article_to_dict(article, ...)` 호출.
- `news_handlers.py::_article_to_dict` (≈22-47): `id/title/url/source/feed_source/market/summary/published_at/keywords/stock_symbol/stock_name`. **`mapped_symbols` 없음.**
- `app/services/kr_news_symbol_mapping/resolver.py::resolve_article_symbols(*, market, stock_symbol, related_rows: Sequence[CandidateRow], ner_matches: Sequence[SymbolMatch]) -> list[MappedSymbol]` — 순수, main. (is_primary: 확정 naver_code 또는 단독 후보만 True.)
- `app/services/news_entity_matcher.py::match_symbols_for_article(title, summary, keywords, market) -> Sequence[SymbolMatch]`.
- `app/models/news.py::NewsArticleRelatedSymbol`(article_id, market, symbol, source, matched_term, score, rank) / `NewsArticle`(id, title, summary, keywords, stock_symbol, market). `get_news_articles(...)`는 `(articles, total)` 반환 — **articles는 detached**.
- `MappedSymbol`/`CandidateRow`: `app/services/kr_news_symbol_mapping/contract.py`.

## 5. 아키텍처 (단일 책임)

**(1) 공유 배치 로더** (신규 `app/services/kr_news_symbol_mapping/related_lookup.py`): `load_related_rows_by_article_ids(article_ids) -> dict[int, tuple[CandidateRow, ...]]`. 자체 세션, `NewsArticleRelatedSymbol.article_id.in_(...)` 조회 → article_id별 `CandidateRow` 그룹핑.

**(2) per-article 매퍼** (신규 헬퍼, news_handlers 인근): `compute_mapped_symbols(article, related_rows) -> list[dict]`: `match_symbols_for_article(title/summary/keywords, market=article.market)` → `resolve_article_symbols(market=article.market, stock_symbol=article.stock_symbol, related_rows=related_rows, ner_matches=...)` → MappedSymbol → dict.

**(3) `_article_to_dict` enrich**: `mapped_by_id` 파라미터 추가 → `item["mapped_symbols"] = (mapped_by_id or {}).get(article.id, [])`. 기존 필드 보존.

**(4) 시임 — `_get_market_news_impl`**:
```
articles, total = await get_news_articles(...)
related_by_id = await load_related_rows_by_article_ids([a.id for a in articles])   # 1 batch query
mapped_by_id  = {a.id: compute_mapped_symbols(a, related_by_id.get(a.id, ())) for a in articles}
# 모든 _article_to_dict(...) 호출부에 mapped_by_id=mapped_by_id 전달
```
- **detached-safe**: `a.id/title/summary/keywords/stock_symbol/market`는 scalar(접근 가능). `a.related_symbols`(관계)는 **미접근** — related_rows는 배치 로더에서.

## 6. 응답 (각 news 아이템)

기존 필드 + `"mapped_symbols": [{symbol, market, mapping_source, confidence, is_primary, matched_term}, ...]` (없으면 `[]`).

## 7. 정직성

`mapped_symbols`는 `resolve_article_symbols` 파생: naver_code=confidence 1.0/primary, candidate=row.score, ner=0.5/is_primary 보수적(복수후보면 전부 False). 매핑 0건 → `[]`. 위조 없음. NER은 별칭사전 기반(시장별), 매칭 없으면 빈 결과.

## 8. 테스트 (read-only, migration 0)

- **배치 로더**: `load_related_rows_by_article_ids`(patched session 또는 integration) — article_id 그룹핑.
- **compute_mapped_symbols**(단위, in-memory article): persisted related_rows + NER 둘 다 반영; **mainnews(persisted 0 → NER 매핑)** 케이스; 매핑 0 → `[]`.
- **`_article_to_dict` enrich**(단위): `mapped_by_id` 전달 시 `mapped_symbols` 필드, 미전달 시 `[]`.
- **`_get_market_news_impl` e2e**(단위, `get_news_articles`+로더 patch): 모든 분기 news 아이템에 `mapped_symbols`.

## 9. 비범위 (별도)

- `search_news`(등록 위치 확인 별도), `get_news` KR(별도), 데이터 수집/별칭사전 변경 0.
- PR1 `db_provider` **미변경**(공유 로더로의 DRY는 두 PR 머지 후 별도).
- migration 0, broker/order/watch/order-intent mutation 0.

## 10. Acceptance criteria

- [ ] `get_market_news` 각 news 아이템에 `mapped_symbols` 필드.
- [ ] persisted related_symbols(naver_code/candidate) + live NER 둘 다 반영.
- [ ] mainnews(persisted 0)도 NER로 매핑(통증 해소).
- [ ] 기존 `stock_symbol`/`stock_name` 보존, 모든 분기(crypto/us-kr briefing/plain/excluded) enrich.
- [ ] 매핑 0건 → `[]`. migration 0, broker/order/watch mutation 0. 단위 테스트 green, ruff clean.

## 11. 안전 경계

read-only. `a.related_symbols` lazy-load 미사용(detached-safe). broker/order/watch/order-intent/scheduler 무접근. 신규 NER/매핑 휴리스틱 도입 없음(기존 `resolve_article_symbols`/`match_symbols_for_article` 재사용).
