# ROB-398 Slice 1 — 뉴스-종목 매핑 read-model + 별칭 확장 설계

- **이슈**: ROB-398 (오케스트레이션 ROB-411 C라인) — Slice 1 of 4
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-02
- **선행/관련**: ROB-397(symbol_analysis 계약), ROB-396(analyze 결정성). 관련 ROB-389(candidate_universe), ROB-391(naver collector 승격).

---

## 0. ROB-398 분해 (4 슬라이스)

ROB-398은 단일 spec에 너무 커서 독립 출하 가능한 슬라이스로 나눈다:

1. **Slice 1 (본 문서)** — 뉴스-종목 매핑 read-model + 별칭 확장.
2. Slice 2 — Naver sise 랭킹 snapshot collector (greenfield, screen_stocks/stale momentum 대체).
3. Slice 3 — 투자자 매매동향 collector (외인/기관 순매수·체결강도, 397 flow 피드).
4. Slice 4 — Toss screener (내부 API 선확보 후).

## 1. 배경 — 기존 인프라와 갭

탐색 결과 매핑 백본의 상당 부분이 이미 존재한다:

- `KR_ALIASES`(`app/services/news_entity_alias_data.py`, 9개) + `news_entity_matcher.match_symbols_for_article`(NER/별칭, `reason∈{alias_dict,candidate_metadata,exact_symbol}`, 모호 시 종목별 dedup 다중 반환).
- `news_articles` + 다대다 `news_article_related_symbols`(`article_id,market,symbol,source,matched_term,score,rank,raw`, UNIQUE `(article_id,market,symbol,source)`). `is_primary`/명시적 `mapping_source`/`confidence` 컬럼은 없음(`source`/`score`가 근사).
- Naver 종목뉴스 fetcher `naver_finance.news.fetch_news(code)`(`&code=` = 매핑전략①, 확정). durable 적재 주경로는 news-ingestor 브리지(`/trading/api/.../ingest/bulk`, `raw.stock_candidates`→related_symbols).

**갭**: 매핑 provenance가 한 곳에 통합돼 있지 않다. ① `naver_code` 확정매핑은 `article.stock_symbol`에만, ② ingestor 후보는 `related_symbols`에, ③ 별칭 matcher는 **조회 시점에만**(영속 X). 소비자가 "이 기사/종목 매핑의 출처·신뢰도·일차성·모호성·신선도"를 일관되게 못 본다.

## 2. 목표·범위

기존 데이터 위 **read-model 뷰**로 매핑 provenance를 통합한다. **migration 0, read-only, backfill/scheduler 없음, 신규 ingest write 없음.** ROB-398 완료기준(종목 코드 매핑·뉴스 매핑·별칭 충돌 처리·freshness metadata)을 read 레이어에서 충족.

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 없음, production DB backfill/commit ingest 없음, scheduler activation 없음, Toss/Naver는 reference/calibration, stale/부재는 숨기지 않고 노출.

## 3. 아키텍처 — 신규 `app/services/kr_news_symbol_mapping/`

기존 3개 매핑 신호를 provenance-rich 통합 뷰로 합치는 read-only query_service.

| mapping_source | 출처 | confidence |
|---|---|---|
| `naver_code` | `article.stock_symbol`(Naver `&code=` 등 확정) | 1.0 |
| `candidate` | 영속 `news_article_related_symbols`(ingestor 후보) | `row.score` 정규화(없으면 rank 파생) |
| `ner` | 조회시점 `match_symbols_for_article`(KR_ALIASES) | 고정 밴드 0.5 |

핵심 단위:

- `resolve_article_symbols(article) -> list[MappedSymbol]` — per-article provenance (세 소스 통합, dedup by symbol, source별 최고 conf 유지).
- `get_symbol_news_mapping(symbol, *, market="kr", hours=24, limit=20) -> SymbolNewsMapping` — symbol-centric, freshness 포함.

frozen dataclass:

```python
@dataclass(frozen=True)
class MappedSymbol:
    symbol: str
    market: str
    mapping_source: str        # "naver_code" | "candidate" | "ner"
    confidence: float          # 0.0..1.0
    is_primary: bool
    matched_term: str | None

@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    latest_as_of: datetime | None
    stale_reason: str | None

@dataclass(frozen=True)
class SymbolNewsMapping:
    symbol: str
    market: str
    articles: tuple[MappedArticle, ...]   # 기사별 매핑 + as_of
    freshness: Freshness
```

## 4. provenance·is_primary·모호성 (확정 의미론)

- mapping_source 우선순위: `naver_code(1.0) > candidate > ner`.
- **is_primary 파생** (per article, 결정적):
  - 확정 소스(`naver_code`)가 있으면 그 매핑이 `is_primary=True` (모호성 없음).
  - 그 외, 단일 후보만 있으면 그 후보가 `is_primary=True`.
  - **NER 모호**(한 기사가 이름충돌로 ner 다중 종목 매칭, 확정/후보 disambiguator 없음): **is_primary 전부 False** — 후보 N개 + confidence 그대로 노출, 강제 단일 금지.
- 같은 symbol이 여러 source로 매핑되면 최고 우선순위 source의 confidence로 합치되 `raw`에 출처별 보존.

## 5. freshness

- per-article `as_of` = `article.published_at`(없으면 `scraped_at`).
- `SymbolNewsMapping.freshness`:
  - 매핑 기사 0건 → `overall="unavailable"`, `stale_reason="no_mapped_news"`.
  - 가장 신선한 매핑 기사 `as_of`가 TTL(기본 24h, 모듈 상수) 내 → `fresh`.
  - TTL 초과 → `stale`, `stale_reason="older_than_ttl"`.
- machine-readable. 소비자가 Korean-facing copy에 활용하도록 reason 동봉. 숨김 없음.

## 6. 별칭 확장 (큐레이티드, precision-first)

- `KR_ALIASES`에 주요 KR 종목·약칭·영문·그룹명 고정탐지 추가(현 9개 → 확장; 시총 상위/뉴스 빈출 위주, false-positive 회피 위해 모호한 짧은 토큰 제외).
- **복합/그룹 별칭 시연**: "삼전닉스" → 005930·000660 두 엔트리가 같은 alias를 공유 → matcher가 둘 다 반환 → §4 모호성 경로(is_primary 보류)로 연결.
- DB `stock_aliases`(StockAliasService) matcher 와이어링은 **후속**(범위 억제, false-positive 검증 부담).

## 7. 테스트 (TDD)

1. `naver_code` 확정(article.stock_symbol==symbol) → mapping_source="naver_code", confidence=1.0, is_primary=True.
2. `candidate`(related_symbols row) → mapping_source/confidence(score 파생)/rank 반영.
3. `ner` 단일 매칭 → mapped, 모호성 없으면 is_primary=True.
4. **NER 모호성**(예 "삼전닉스"→005930+000660, 확정/후보 없음) → 후보 2개, **is_primary 전부 False**.
5. freshness: 최근 as_of→fresh, TTL 초과→stale(reason), 0건→unavailable(reason).
6. 별칭 확장: 신규 큐레이티드 별칭 매칭(positive) + 무관 텍스트 미매칭(false-positive 회귀).
- DB는 기존 테스트 패턴(fake/in-memory article objects)으로, 실 네트워크 없음.

## 8. 비목표 (YAGNI)

- ingest 시점 영속화 / migration / `is_primary`·`mapping_source` 컬럼 (read-time 파생).
- 새 snapshot_kind / 번들 통합 (기존 `news` collector가 후속 슬라이스에서 이 read-model 소비 가능).
- Naver 랭킹·투자자 매매동향·Toss screener (Slice 2~4).
- DB `stock_aliases` matcher 와이어링.
