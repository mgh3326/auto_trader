# Benzinga 뉴스 그림자 수집 (hk 6395)

Benzinga News API를 Finnhub company-news와 같은 창·같은 종목에서 비교하는
읽기 전용 그림자 수집이다. DB 쓰기, 뉴스 relevance 파이프라인, MCP `get_news`
변경, 유료 플랜 변경은 범위 밖이다.

## 상태

- 기본 off. `BENZINGA_NEWS_SHADOW_ENABLED` 가 정확히 `true` 일 때만 동작하고,
  그 외에는 어떤 네트워크 호출도 없이 종료 코드 2 로 끝난다.
- 키는 환경 변수 `BENZINGA_API_KEY` 에서만 읽는다. 기본값·파일 경로 인자 없음.
- 출력은 repo 밖 JSONL(`--out-dir`, repo 안 경로는 거부). append-only.
- 스케줄러 등록 없음. `deploy/dagu/at-news-shadow/` 는 등록되지 않는 복사본이다.

## 수집

```bash
BENZINGA_NEWS_SHADOW_ENABLED=true BENZINGA_API_KEY=<키> \
  uv run python -m scripts.benzinga_news_shadow poll \
    --symbols NVDA,AMD --out-dir /tmp/benzinga-shadow --limit 50
```

- 심볼마다 Benzinga `primaryTickers=<심볼>`(커버리지) + `tickers=<심볼>`(증거)
  2건을 같은 패스에서 읽고, Finnhub company-news 1건을 같은 패스에서 읽는다.
  개별 심볼 조회이며 하나의 `tickers=A,B` 호출로 묶지 않는다. 0건도 `counts` 와
  `poll_status` 행에 그대로 남는다. 0건은 "뉴스 없음"이 아니라 그 poll 의 관측이다.
- 기사당 1줄(`record_type: article`): `source`, `provider_id`, `url`,
  `title`, `symbols/stocks`, `query_symbol`, `query_param`(Benzinga 요청 파라미터명),
  원문 `created_raw/updated_raw` + 근거(`created_basis`), 파싱된 UTC(`created_utc`,
  `updated_utc`), 수집 시계 `first_received_at`, `poll_id`.
- 심볼·소스·파라미터당 1줄(`record_type: poll_status`): `ok`/`empty`(건수) 또는
  `error`(예외 클래스명). 실패한 셀은 summary 에서 제외·보고되므로, 실패를
  "뉴스 없음"으로 읽지 않는다. pass 당 1줄씩 `poll_header`(계획 심볼)와
  `poll_completion`(실행 완료 심볼)도 기록한다 — 중간에 죽은 poll 은 completion
  이 없어 summary 가 `incomplete_polls` 로 보고하고 그 셀을 unreliable 로 뺀다.
- `first_received_at` 은 소스별로 그 응답이 도착한 뒤에 찍는다. 두 소스를
  순차로 읽기 때문에 차이는 순서 신호로만 읽는다(정밀 경쟁 측정이 아니다).
- Finnhub 시각은 naive host-local 문자열이므로 host 시간대로 읽어 UTC 로
  바꾼다(`created_basis: finnhub_naive_interpreted_as_local`). KST host 에서
  수동 실행해도 UTC 로 기록된다.
- `--date-from/--date-to`(`YYYY-MM-DD`, UTC 날짜, 양끝 포함)는 Benzinga 에는
  요청 파라미터로, Finnhub 에는 발행일 필터로 동시에 걸린다. 범위를 벗어난
  Finnhub 기사는 버린다(발행일 판독 불가도 버린다).
- `--poll-id` 는 `[A-Za-z0-9._-]+` 만 받는다. 이 패턴을 벗어나면 쓰기 없이
  종료 코드 2.
- 어떤 소스라도 실패하면 종료 코드 1(성공 수집처럼 보이지 않게 한다). 게이트
  off·인자 오류·키 없음은 종료 코드 2.

## KIS 뉴스 제목 소스 (hk 6661)

poll 은 Benzinga·Finnhub 과 같은 패스에서 KIS 뉴스 제목을 `source: kis` 행으로
함께 읽는다. 기본 on. `--skip-kis` 로 끈다(KIS 장애·쿼터 소진 시). `--market kr`
은 국내 헤드라인만 읽는다(Benzinga·Finnhub 없음, `BENZINGA_API_KEY` 를 읽지
않는다).

| query_param | TR | 범위 | 공식 샘플 |
|---|---|---|---|
| `brknews_title` | FHKST01011801 `/uapi/overseas-price/v1/quotations/brknews-title` | 시장 전체, 패스당 1회 | `examples_llm/overseas_stock/brknews_title` |
| `overseas_news_title` | HHPSTH60100C1 `/uapi/overseas-price/v1/quotations/news-title` (`SYMB`) | 심볼별 | `examples_llm/overseas_stock/news_title` |
| `domestic_news_title` | FHKST01011800 `/uapi/domestic-stock/v1/quotations/news-title` (`FID_INPUT_ISCD`) | KR 심볼별 | `examples_llm/domestic_stock/news_title` |

- 응답은 한 번만 읽는다(연속 조회 `tr_cont`/`CTS` 는 읽지 않는다). brknews 샘플은
  응답당 최대 100건이라고만 적고 연속 조회를 적지 않는다.
- 호출 속도는 TR 별 2/초로 제한한다(`DEFAULT_KIS_API_RATE_LIMITS`).
- **매칭 규칙(brknews)**: `iscd1`..`iscd10` 코드를 `app/core/symbol.py` 의
  `to_db_symbol` 로 DB 형식(`.`)으로 바꿔 polled 심볼과 같으면 매칭한다. 여러
  심볼에 걸린 기사는 심볼마다 한 행, 어느 심볼에도 걸리지 않은 기사는
  `query_symbol: null` 로 한 번 남긴다(`symbol_match: unmatched`).
- **행 식별**: `provider_id = <TR>:<일련번호>` (brknews·국내는 `cntt_usiq_srno`,
  해외뉴스는 `news_key`). TR 접두어가 있어 소스 안에서 번호 공간이 섞이지 않는다.
- **시각(확인되지 않음)**: 샘플은 시간대를 적지 않는다. `data_dt`/`data_tm` 을
  Asia/Seoul 로 읽어 UTC 로 저장하고 `created_basis: kis_kst_unverified` 로 표시한다.
  해외뉴스 샘플은 이 두 필드를 `조회일자`/`조회시간` 으로 적으므로 기사 시각이
  아닐 수 있어 `created_basis: kis_label_query_unverified` 로 표시한다. 해석 불가·
  누락은 `created_utc: null` 과 `created_reason`(`unparsable`/`missing`)이다.
- **원문 보존**: 제목·`dorg`(자료원)·원본 날짜/시각 문자열(`created_raw`)을 남긴다.
  URL 과 본문 요약은 KIS 응답에 없으므로 null 이다.
- 실패는 `poll_status` 오류 행이고 Benzinga·Finnhub 는 계속 읽는다. 종료 코드는
  다른 소스와 같이 1 이다.
- `summary` 의 `kis` 블록: 심볼별 커버리지(`coverage_by_symbol`, KIS·Benzinga·
  Finnhub 의 고유 `provider_id` 수, 중복 제거 전), 제목 겹침(`headline_overlap`,
  정규화 제목만 — KIS 에는 URL 이 없다), 첫 관측 순서(`first_seen_wins`, 두 소스
  이상에 보인 제목에서 가장 이른 `first_received_at` 의 소스), KIS 측 제외 셀
  (`unreliable_polls`). KIS 오류는 KIS 셀만 뺀다. Benzinga·Finnhub 의 기존 비교는
  바뀌지 않는다. 시장 전체 오류는 그 패스의 계획 심볼 전부를 KIS 에서만 unreliable
  로 표시한다.

## 비교

```bash
BENZINGA_NEWS_SHADOW_ENABLED=true \
  uv run python -m scripts.benzinga_news_shadow summary \
    --input /tmp/benzinga-shadow/a.jsonl /tmp/benzinga-shadow/b.jsonl [--poll-id <id>]
```

- `--input` 은 여러 파일 또는 디렉터리(`*.jsonl`)를 받는다. poll 마다 파일이
  따로 생기므로 창 비교는 파일을 모아 읽는다.
- 같은 기사를 (source, provider_id) 기준으로 묶어 가장 이른 관측만 남기고
  나머지는 `duplicates_removed` 로 센다(심볼별 조회가 같은 기사를 두 번
  쓰기 때문이다).
- 겹침은 두 단계로 잰다: 먼저 정규화 URL(소문자 host+path, 식별 쿼리 유지·
  추적 파라미터 제거·정렬), 그 다음 남은 기사끼리 정규화 제목(소문자·공백
  축소)으로 1:1 짝짓기. URL 이 다른 같은 이야기가 제목에서 만난다.
- 소스별 unique 건수, 같은 `poll_id` 안에서 양쪽이 모두 관측된 쌍의
  `first_received_at` 차이(min/p50/max, 초), 소스별 duplicate rate.
- poll 이 다른 쌍의 수신 시각 차이는 비교하지 않는다(같은 시간에 함께
  관측된 경우만 잰다 — 유료 도구 벤치마크 시트 `policy/at-paid-data-tool-benchmark`).
- 어느 한 소스라도 `error` 인 (poll, 심볼) 셀은 `unreliable_symbol_polls` 로
  보고하고 겹침·unique 에서 뺀다. 상대 소스의 기사는 unique 로 세지 않는다.

## tickers=NVDA 동작 (2026-11-25 설계 세션 실측으로 확정)

설계 세션 실측: `tickers=NVDA` 는 0건인데 `primaryTickers=NVDA` 는 NVDA 기사를
돌려준다. `tickers` 필터는 기사의 대표(first-listed) 티커에만 걸리고 부차 언급은
매칭하지 않는다. CLI 는 그래서 심볼마다 `primaryTickers` 1건(커버리지)과
`tickers` 1건(증거)을 각각 조회하고, 양쪽 결과를 `query_param` 과 함께 기록한다.
summary 는 합집합(중복 제거 뒤)으로 겹침을 재고 `benzinga_by_query_param` 에
파라미터별 건수를 남긴다.

## Save+ 뉴스 비교 소스·관심도 기록 (hk 6694)

Save+ 는 로그인 없는 앱 백엔드 `api.prod.fivelines.co.kr` 만 읽는다. 이 API 는
비공식이고 언제든 바뀔 수 있다. `www.saveticker.com` 웹 API(Cloudflare), 인증이
필요한 호스트·경로, SSE 는 사용하지 않는다. DB ingest 와 `get_news` 연결은 후속
hk 6695 범위다.

```bash
BENZINGA_NEWS_SHADOW_ENABLED=true \
  uv run python -m scripts.benzinga_news_shadow poll \
    --symbols NVDA,AMD,0256C0.KR --out-dir /tmp/benzinga-shadow \
    --saveticker --saveticker-pages 3
```

- `--saveticker` 로 켜고 `--skip-saveticker` 로 끈다. 수집은 기본 off 이며,
  Save+ 호출이 실패하면 해당 요청의 `poll_status` 에 `status=error` 와 예외 클래스가
  남고 CLI 는 실패로 끝난다. Benzinga/Finnhub/KIS 는 계속 polling 한다. 실패를 빈
  피드로 보지 않는다.
- `/api/news` 최신 페이지와 `/api/news/top-stories` 를 읽는다. 페이지 수 1–10,
  페이지 크기 1–100, 페이지 사이 최소 0.5초 간격, timeout, redirect 거부를 적용한다.
  제한된 `.saveticker-seen-ids.json` sidecar 에서 이전 ID 를 처음 만나면 뉴스 페이지
  탐색을 멈추되, 경계 항목은 해당 poll 의 관심도 행으로 다시 기록한다.
- 제공 `tickers[].symbol` 만 `app/core/symbol.py::to_db_symbol` 로 정규화한다.
  캡처에는 `NVDA`, `DIS`, `0256C0.KR` 등이 있어 미국 티커는 그대로, 한국 티커는
  캡처된 `.KR` 접미와 DB `.` 구분자를 유지한다. 매칭된 폴링 심볼마다 기사 행을 쓰며,
  미일치 항목도 `query_symbol=null` 로 남긴다.
- 각 항목은 원문 `created_at` 과 파싱된 UTC 시각, 참여 수치와 라벨을 기록한다.
  알 수 없는 wire 필드는 `raw_item` 에 보존한다. 잘못된 항목은 버리고
  `malformed_count` 에 기록한다.
- 한 provider item 마다 poll 당 `record_type=attention` JSONL 행을 하나 써서
  `observed_at`, `view_count`, `comment_count`, `like_stats`, `vote_stats`,
  `is_top_story`, labels, tickers 를 남긴다.
- summary 는 ticker 와 소스별 coverage·첫 관측 순서를 보고한다. 한국어 번역 제목은
  Save+ 내부에서만 정규화 제목으로 겹침을 비교한다. 다른 소스와는 제목을 대조하지
  않고 ticker 가 겹치며 provider 생성 시각 차이가 10분 이내일 때 겹침으로 센다.

`deploy/dagu/at-news-shadow/` 의 Benzinga/Finnhub/KIS 주기는 기존 30분 간격을 유지한다.
Save+ 전용 3분 설정은 `deploy/dagu/at-saveticker-shadow/` 에 따로 두며 두 복사본 모두
등록하지 않는다. 실제 운영 등록은 설계 세션/운영자 소관이다.

## 운영

- 키·구매는 운영자 결정. Dagu 등록은 hk 6395 머지 뒤 설계 세션이 한다.
- KIS 소스는 DAG 복사본(`deploy/dagu/at-news-shadow/`)을 바꾸지 않아도 기본 on 으로
  들어간다. 그 DAG 환경(`.env.api`)에 KIS 자격증명과 토큰 캐시(Redis)가 있는지
  등록 전에 확인하고, 없으면 `--skip-kis` 를 명령에 넣는다(KIS 오류는 종료 코드 1).
- Dagu 복사본은 게이트를 `docker run -e BENZINGA_NEWS_SHADOW_ENABLED=true`
  로 컨테이너에 직접 넘긴다(DAG `env:` 는 호스트 셸에만 보인다). 출력은
  `-v /root/at-run/benzinga-shadow:/out` 마운트로 호스트에 남긴다. 호스트
  디렉터리는 appuser(uid 10001)가 쓸 수 있어야 하며 등록 시점에 운영자가
  만든다.
- `summary` 입력이 없거나 `--out-dir` 이 repo 안이면 쓰기 없이 종료 코드 2.
