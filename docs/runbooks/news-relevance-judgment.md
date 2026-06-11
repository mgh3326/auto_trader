# News Relevance Judgment Job (ROB-491)

## 개요

get_news(KR)가 수집·저장한 기사의 종목 관련성 판정을 **외부 LLM Job**(Hermes류
세션 또는 operator 수동 실행)이 수행해 write-back하는 절차. auto_trader는
판정하지 않으며 어떤 기사도 자동 제외하지 않는다. recurring 스케줄러
(cron/Prefect) 연결 없음 — ROB-506의 TaskIQ enqueue는 get_news 호출 시에만
발생하며 default-off다. production 활성화는 별도 operator gate.

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

## TaskIQ 비동기 판정 worker (ROB-506)

`get_news`(KR)가 새 pending link를 만들면 `news_relevance.judge_pending`
task를 enqueue한다 (fail-open — enqueue 실패해도 get_news는 성공). Task는
pending batch를 외부 Hermes-호환 judgment webhook에 POST하고, 응답에
inline `judgments`가 있으면 기존 ingest 규칙(서버 status 파생)으로
적용한다. 응답이 dispatch-only(2xx, judgments 없음)면 외부 세션이 위의
ingest/bulk 경로로 write-back할 때까지 pending이 유지된다. 실패/검증 실패
시에도 pending 유지 — excluded로 오판정되는 경로는 없다.

### 활성화 (default-off)

| env | default | 의미 |
| --- | --- | --- |
| `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED` | `false` | off면 enqueue 없음 + commit-mode task는 `disabled` 반환 |
| `NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL` | `""` | 외부 judgment endpoint. 미설정 시 client `skipped` |
| `NEWS_RELEVANCE_JUDGMENT_TOKEN` | `""` | outbound Bearer 토큰 (로그/결과에 출력 안 됨) |
| `NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S` | `120` | webhook 호출 timeout |
| `NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT` | `50` | run당 pending batch 상한 (하드캡 200) |

`HERMES_WEBHOOK_URL`/`HERMES_TOKEN`(ROB-265 알림)과
`NEWS_RELEVANCE_INGEST_TOKEN`(inbound write-back 인증)과는 별개 설정이다.
inline 응답을 안 쓰는 Hermes 구성이라면 write-back을 위해 기존
`NEWS_RELEVANCE_INGEST_TOKEN`도 함께 설정되어 있어야 한다.

### 수동 smoke (worker 로컬 실행)

```bash
# 1. 의존 서비스 + worker
docker compose up -d            # postgres, redis
make taskiq-worker              # uv run taskiq worker app.core.taskiq_broker:broker app.tasks

# 2. dry-run (flag off에서도 허용 — client 호출/DB write 없음)
uv run python - <<'PY'
import asyncio
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

print(asyncio.run(run_news_relevance_judgment(market="kr", dry_run=True)))
PY
# 기대: {"status": "dry_run" | "no_pending", "fetched_pending": N, ...}

# 3. commit-mode (operator gate: flag + webhook 설정 후)
#    get_news(MCP)로 pending을 만든 뒤 worker 로그에서
#    "news_relevance judgment run: ... status=judged|dispatched" 확인.

# 4. 검증 — 기존 §Job 절차 4와 동일: get_news 재호출로
#    excluded_count 증가 / confirmed relevance 블록 확인.
```

### Task result 필드

`fetched_pending`, `judged`, `applied_confirmed`, `applied_excluded`,
`skipped_unrequested`, `invalid_judgments`, `link_not_found`,
`client_mode`, `dry_run`, `http_status`, `reason`. 토큰 값은 어디에도
포함되지 않는다.

## US / crypto (Finnhub) — ROB-510

ROB-510부터 `get_news(market="us"|"crypto")`도 KR과 동일하게
`news_articles` + `symbol_news_relevance`에 set-difference upsert 후 DB
상태로 응답한다 (pending 표시, excluded 제외). 판정 파이프라인(worker /
GET pending / POST ingest/bulk)은 market 파라미터로 이미 지원되며 별도
배선 변경 없음 — pending 적체 점검 시 `market=us`, `market=crypto`도 함께
조회할 것.

- feed_source: `finnhub_company_news`(us) / `finnhub_general_news`(crypto)
- crypto는 Finnhub general 피드(심볼 키 아님)라 unrelated 비율이 높을 수
  있다 — `relationship=unrelated`/`relevance=low` → excluded 파생은 KR과
  동일.
- Finnhub fetch는 시도당 `FINNHUB_NEWS_TIMEOUT_S`(기본 8s) ×
  `FINNHUB_NEWS_MAX_ATTEMPTS`(기본 3) 재시도. 전 실패 시 응답은
  `degraded: true` + `fetch_error` + DB 기사(stale) 폴백.
- degraded 폴백으로 DB에서 복원된 항목은 sentiment가 없을 수 있다
  (sentiment는 미영속 — 신선 fetch 응답에만 포함).

## 트러블슈팅

- 403 "not configured" → 서버에 토큰 env 미설정.
- 401 → 토큰 불일치 / 헤더 이름 확인.
- pending이 비어 있음 → get_news가 최근 호출된 적 없는 종목이면 수집된 행이
  없는 것이 정상 (수집은 get_news 호출 시에만 발생).
