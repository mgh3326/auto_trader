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

- 심볼마다 Benzinga `tickers=<심볼>` 1건 + Finnhub company-news 1건을 같은
  패스로 읽는다. `tickers=NVDA` 가 0건을 돌려준 관측(hk 6395)이 있어 심볼을
  하나의 `tickers=A,B` 호출로 묶지 않는다. 0건도 `counts` 에 그대로 남긴다.
  0건은 "뉴스 없음"이 아니라 그 poll 의 관측이다.
- 기사당 1줄: `source`, `provider_id`, `url`, `title`, `symbols/stocks`,
  `channels`, 원문 `created_raw/updated_raw`, 파싱된 UTC(`created_utc`,
  `updated_utc`), 수집 시계 `first_received_at`, `poll_id`.
- 키는 로그·예외·출력 어디에도 남지 않는다. 전송은 `follow_redirects=False`.

## 비교

```bash
BENZINGA_NEWS_SHADOW_ENABLED=true \
  uv run python -m scripts.benzinga_news_shadow summary \
    --input /tmp/benzinga-shadow/<poll>.jsonl
```

- 정규화 URL(소문자 host+path, scheme·query·fragment 제거) 우선, 없으면
  정규화 제목(소문자·공백 축소)으로 겹침을 잰다.
- 소스별 unique 건수, 같은 `poll_id` 안에서 양쪽이 모두 관측된 쌍의
  `first_received_at` 차이(min/p50/max, 초), 소스별 duplicate rate.
- poll 이 다른 쌍의 수신 시각 차이는 비교하지 않는다(같은 시간에 함께
  관측된 경우만 잰다 — 유료 도구 벤치마크 시트 `policy/at-paid-data-tool-benchmark`).

## tickers=NVDA 가설 (미확인)

설계 세션 관측: `tickers=NVDA` 는 0건인데 같은 기사가 `tickers=AAPL` 에는
잡힌다. 이 워커는 API 를 호출할 수 없어 가설만 둔다: `tickers` 필터가 기사의
대표(first-listed) 티커에만 걸리고 부차 언급은 매칭하지 않을 수 있다.
CLI 는 그래서 심볼별 개별 조회 + 명시적 0건 기록으로 둔다. 설계 세션 확인 후
이 문단을 갱신한다.

## 운영

- 키·구매는 운영자 결정. Dagu 등록은 hk 6395 머지 뒤 설계 세션이 한다.
- `summary` 입력이 없거나 `--out-dir` 이 repo 안이면 쓰기 없이 종료 코드 2.
