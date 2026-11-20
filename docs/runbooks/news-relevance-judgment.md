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

## 판정 plumbing 헬퍼 (ROB-889)

수동 판정 루프(§Job 절차)를 반복·안전하게 만드는 default-disabled CLI.
**LLM 미포함** — pending fetch / 페이로드 검증 / ingest POST의 HTTP glue만 담당한다.
판정 자체는 세션(Claude/Hermes)이 수행(ROB-501 LLM boundary 준수).

```bash
# 1. pending 조회 (read-only, 토큰 필요)
uv run python -m scripts.news_relevance_judge_smoke --mode fetch \
    --market kr --limit 50 --host https://<host> > pending.json

# 2. <세션이 pending.json을 판정해 judgments.json 작성>

# 3. 로컬 검증 (네트워크/토큰 불필요 — 서버 422를 미리 잡음)
uv run python -m scripts.news_relevance_judge_smoke --mode validate \
    --file judgments.json
#    → {"status":"valid","judgments":N,"would_exclude":X,"would_confirm":Y}
#    would_exclude 은 서버 파생 규칙(unrelated|low)의 로컬 미리보기다.

# 4. 제출 (DB mutation → --confirm 필수; 없으면 dry_run 미리보기)
uv run python -m scripts.news_relevance_judge_smoke --mode submit \
    --file judgments.json --host https://<host> --confirm
```

- 토큰은 `NEWS_RELEVANCE_INGEST_TOKEN` env 에서 읽고 **절대 출력하지 않는다**(키 유무만 보고).
  헤더 이름은 `NEWS_RELEVANCE_INGEST_TOKEN_HEADER`(기본 `X-News-Relevance-Ingest-Token`).
- host 기본값은 `NEWS_RELEVANCE_SMOKE_HOST` 또는 `http://localhost:8000`.
- `submit`은 `--confirm` 없으면 검증+미리보기만 하고 아무것도 보내지 않는다(fail-safe).
- 판정 JSON은 서버와 동일한 pydantic 계약(`NewsRelevanceIngestRequest`)으로 검증한다 —
  enum/shape 위반은 POST 전에 걸린다. `status`는 서버가 파생하므로 절대 보내지 않는다.

## 판정 task와 자동 트리거 (ROB-506, hk 5753 에서 자동 트리거 제거)

🔴 **`get_news` 는 더 이상 TaskIQ 를 enqueue 하지 않는다 (hk 5753).** 새 pending link
가 생겨도 아무것도 보내지 않으며, 대기 행은 응답에 `pending` 으로 그대로 보인다. 이전에는
`news_relevance.judge_pending` 을 enqueue 해 외부 Hermes-호환 webhook 을 깨웠지만, 레포의
worker 를 은퇴시키면서 그 깨우기를 없앴다. 판정은 외부 세션이 하는 루프(아래 「외부 판정 루프」)로만
진행된다. 이 변경으로 `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED` 는 commit-mode task 의
게이트로만 남는다.

`news_relevance.judge_pending` task 정의와 `run_news_relevance_judgment` 본문은
`app/tasks`·`app/jobs` 에 남아 있지만 호출하는 production 경로가 없다. 이를 실행하려면
운영자가 아래 「수동 smoke」의 Python 스니펫으로 직접 호출한다. 그 본문은 pending batch 를
외부 Hermes-호환 judgment webhook 에 POST 하고, 응답에 inline `judgments` 가 있으면 기존
ingest 규칙(서버 status 파생)으로 적용한다. 응답이 dispatch-only(2xx, judgments 없음)면
외부 세션이 ingest/bulk 로 write-back 할 때까지 pending 이 유지된다. 실패/검증 실패 시에도
pending 유지 — excluded 로 오판정되는 경로는 없다.

### 외부 판정 루프 (권장)

외부 세션이 `GET /trading/api/news-relevance/pending` 을 주기적으로 읽고, 판정을 만들어
`scripts.news_relevance_judge_smoke` 의 `--mode validate` → `--mode submit --confirm` 으로
ingest 한다. 이 경로는 자동 제외를 하지 않고 LLM 판정도 레포 밖에서 한다(ROB-501).
polling 주기와 호출 주체는 운영자 결정이다. 레포는 스케줄을 등록하지 않는다.

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

### 수동 smoke (in-process, worker 없음)

```bash
# 1. 의존 서비스
docker compose up -d            # postgres, redis

# 2. dry-run (flag off에서도 허용 — client 호출/DB write 없음)
uv run python - <<'PY'
import asyncio
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

print(asyncio.run(run_news_relevance_judgment(market="kr", dry_run=True)))
PY
# 기대: {"status": "dry_run" | "no_pending", "fetched_pending": N, ...}

# 3. commit-mode (operator gate: flag + webhook 설정 후)
#    get_news(MCP)로 pending을 만든 뒤 위 스니펫을 dry_run=False 로 한 번 실행하고
#    출력의 "status" 가 judged|dispatched 인지 확인한다 (TaskIQ worker 로그 아님).
#    🔴 이 스니펫은 NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED 를 보지 않는다(flag 는 task 래퍼만 검사).
#    dry_run=False 는 flag·webhook 설정을 운영자가 확인한 뒤에만 실행한다.

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
- ROB-579: when `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=true`, a `get_news`
  call enqueues `news_relevance.judge_pending` if the canonical DB response
  still contains visible `pending` rows. This covers both newly inserted links
  and links created during an earlier worker/webhook outage. Duplicate enqueue
  is acceptable: the job re-queries pending rows and exits `no_pending` after
  another worker has applied judgments.

US dry-run smoke:

Run this after a US `get_news` call has created pending rows for the symbol.

```bash
uv run python - <<'PY'
import asyncio
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

print(asyncio.run(run_news_relevance_judgment(
    market="us",
    symbol="AMZN",
    dry_run=True,
)))
PY
```

Expected: `status` is `dry_run` with `fetched_pending > 0` when AMZN has pending
rows; otherwise `no_pending`. No webhook call or DB write happens in dry-run.

## 트러블슈팅

- 403 "not configured" → 서버에 토큰 env 미설정.
- 401 → 토큰 불일치 / 헤더 이름 확인.
- pending이 비어 있음 → get_news가 최근 호출된 적 없는 종목이면 수집된 행이
  없는 것이 정상 (수집은 get_news 호출 시에만 발생).
