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

## 운영

- 키·구매는 운영자 결정. Dagu 등록은 hk 6395 머지 뒤 설계 세션이 한다.
- Dagu 복사본은 게이트를 `docker run -e BENZINGA_NEWS_SHADOW_ENABLED=true`
  로 컨테이너에 직접 넘긴다(DAG `env:` 는 호스트 셸에만 보인다). 출력은
  `-v /root/at-run/benzinga-shadow:/out` 마운트로 호스트에 남긴다. 호스트
  디렉터리는 appuser(uid 10001)가 쓸 수 있어야 하며 등록 시점에 운영자가
  만든다.
- `summary` 입력이 없거나 `--out-dir` 이 repo 안이면 쓰기 없이 종료 코드 2.
