# News Relevance Judgment Job (ROB-491)

## 개요

get_news(KR)가 수집·저장한 기사의 종목 관련성 판정을 **외부 LLM Job**(Hermes류
세션 또는 operator 수동 실행)이 수행해 write-back하는 절차. auto_trader는
판정하지 않으며 어떤 기사도 자동 제외하지 않는다. 스케줄러(TaskIQ/cron/Prefect)
연결 없음 — 이 런북의 절차는 항상 레포 밖에서 호출된다.

- 데이터: `news_articles` + `symbol_news_relevance` (상태 `pending` →
  `confirmed`/`excluded`, 전이는 ingest 경로로만)
- 서비스: `app/services/symbol_news_store.py`
- 라우터: `app/routers/news_relevance.py`

## 활성화 (default-off)

- `NEWS_RELEVANCE_INGEST_TOKEN` 설정 (미설정 시 `/trading/api/news-relevance/*`
  전 호출 403). 운영 secret manager에 배치, repo에 commit 금지.
- 헤더: `X-News-Relevance-Ingest-Token` (override:
  `NEWS_RELEVANCE_INGEST_TOKEN_HEADER`)
- GET pending도 토큰 필요 (기사 배치가 노출되므로 fail-closed).

## Job 절차

1. **pending 조회**

   ```bash
   curl -s -H "X-News-Relevance-Ingest-Token: $TOKEN" \
     "https://<host>/trading/api/news-relevance/pending?market=kr&limit=50"
   ```

   응답 항목: `article_id`, `market`, `symbol`, `url`, `title`, `source`,
   `published_at`, `first_seen_at`, `hints`.

2. **판정** — 항목별 기준:
   - `relationship`: `direct`(종목 직접 보도) / `material_indirect`(밸류체인·
     투자처·계열 등 실질 연관) / `incidental`(스치는 언급) / `unrelated`(무관)
   - `relevance`: `high`/`medium`/`low` — 해당 종목 투자 판단에의 유용성
   - `price_relevance`: `catalyst`(가격 변동의 원인) / `explainer`(변동 해설) /
     `background` / `none`
   - `hints`는 결정적 참고 신호일 뿐 (alias_match 있으면 direct 가능성이 높지만
     보장 아님 — 예: 판다 기사 본문의 "네이버 카페" 언급)
   - `reason`에 판단 근거를 1~2문장으로 남길 것

3. **write-back** (배치 ≤200)

   ```bash
   curl -s -X POST -H "X-News-Relevance-Ingest-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     "https://<host>/trading/api/news-relevance/ingest/bulk" \
     -d '{"judgments": [{"article_id": 123, "market": "kr", "symbol": "035420",
          "relationship": "direct", "relevance": "high",
          "price_relevance": "catalyst", "score": 0.9,
          "reason": "급락 원인 직접 보도", "judged_by": "hermes"}]}'
   ```

   - status는 서버가 파생한다: `relationship=unrelated` **또는**
     `relevance=low` → `excluded`, 그 외 → `confirmed`. Job이 status를 직접
     보내지 않는다.
   - 멱등: 같은 (article_id, market, symbol) 재판정은 overwrite + `judged_at`
     갱신.
   - 응답 `errors[]`의 `link_not_found`는 재시도 대상 아님 (링크 부재) — 무시
     가능. enum 위반은 422로 일괄 거부 (loc에 항목 index 포함).

4. **검증**
   - 판정 후 `get_news(symbol)` 호출 → `excluded_count` 증가 + 해당 기사 미노출
     + confirmed 기사의 `relevance` 블록에 판정 필드 채워짐 확인.

## 트러블슈팅

- 403 "not configured" → 서버에 토큰 env 미설정.
- 401 → 토큰 불일치 / 헤더 이름 확인.
- pending이 비어 있음 → get_news가 최근 호출된 적 없는 종목이면 수집된 행이
  없는 것이 정상 (수집은 get_news 호출 시에만 발생).
