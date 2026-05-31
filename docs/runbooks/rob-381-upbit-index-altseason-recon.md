# ROB-381 — Upbit 디지털자산지수·알트시즌 데이터 소스 정찰 (reconnaissance spike)

**Status:** PR1 reconnaissance only. No production MCP tool / collector built here.
**Parent:** ROB-377 (코인 마켓레짐·파생심리 데이터 소스 — A1/A2 완료) · ROB-369 (crypto 리포트 데이터 공백)
**Verdict:** `implement` — **분할 권장** (아래 [Final verdict](#final-verdict) 참고).
**Date:** 2026-05-31

---

## Why this issue exists (context)

ROB-377은 #1040 (CoinGecko `/global` 기반 crypto market index / dominance / total market cap)와
#1045 (Binance USD-M OI + LSR)로 마켓레짐·파생심리 수용 기준을 충족했다. 그 본문에
후보로 남아 있던 **Upbit `코인동향` / 디지털자산지수 / 알트시즌성 지표**는 공식 공개 API가
아니라 웹 엔드포인트 정찰이 필요한 영역이라, ROB-377 closure blocker로 두지 않고 이 follow-up으로 분리했다.

PR1은 구현이 아니라 **정찰 spike**로 제한한다: 통합 가능 여부를 판단할 근거(엔드포인트, 스키마,
약관/robots 리스크, fixture화 가능성, 최소 계약, fail-open 설계)를 만든다.

## Method & reproducibility

- 도구: gstack `/browse`(headless Chromium)로 `https://www.upbit.com/trends`(= 코인동향) 렌더 +
  네트워크 캡처. 이후 식별된 엔드포인트를 서버사이드 `curl`로 무인증·무쿠키 재현.
- 모든 호출은 **read-only public GET**. 계좌/주문/private API 미사용. secret 미사용·미출력.
- 응답 샘플(공개 데이터, secret 無)은 `tests/fixtures/upbit_index/`에 정제 저장.
- `https://www.ubcindex.com/`(과거 UBCI 사이트)는 **DNS 해석 실패**(`ERR_NAME_NOT_RESOLVED`) →
  현재 디지털자산지수는 `upbit.com/trends` + 아래 데이터 호스트로 통합된 것으로 보임.

## 데이터 접근 경로 (acceptance #1)

코인동향 화면은 전부 **무인증 JSON 엔드포인트**로 구동된다. 데이터는 세 호스트로 나뉜다.

| 데이터 | 엔드포인트 | 호스트 | 인증 | 캐시/특성 |
|---|---|---|---|---|
| 지수 카탈로그 | `/platform/v1/index/master` | `datalab-static.upbit.com` | 없음 | S3, `max-age=60` |
| 지수 최근값(OHLC+change) | `/platform/v1/index/recent` | `datalab-static` | 없음 | S3, `max-age=60` |
| 지수 요약통계(yield/beta/sharpe/winRate) | `/platform/v1/index/summary` | `datalab-static` | 없음 | S3 |
| 지수 카테고리 | `/platform/v1/index/category` | `datalab-static` | 없음 | S3 |
| 지수 시계열(15분봉 OHLC) | `/platform/v1/index/candles/lines/{code}/{YYYY-MM-DD}` | `datalab-static` | 없음 | S3 |
| 기간별 상승률(745마켓 × 7/30/90/180/365일) | `/v1/crix/trends/interval_change_rate` | `crix-api-cdn.upbit.com` | 없음 | live `no-cache` |
| 주간 상승률(BTC마켓 로테이션 랭킹) | `/v1/crix/trends/weekly_change_rate?count=N` | `crix-api-cdn` | 없음 | live |
| 일 매수/매도 체결강도 | `/v1/crix/trends/daily_volume_power?quoteCurrencyCode=KRW&orderBy=bid|ask&count=N` | `crix-api-cdn` | 없음 | live |
| (마켓 메타) crix_master | `/v2/crix_master` | `crix-static.upbit.com` | 없음 | static |

관찰:
- `datalab-static`는 `server: AmazonS3`, `cache-control: max-age=60` — **정적·캐시 가능한 공개 데이터 제품**.
- `crix-api-cdn`은 `cache-control: no-cache` — live API.
- CORS `Access-Control-Allow-Origin` 헤더는 응답에 없음 → **브라우저 fetch는 cross-origin 차단될 수 있으나
  서버사이드 collector(httpx)에는 무관**(CORS는 브라우저 전용).
- rate-limit: `interval_change_rate` 6연속 호출 전부 200, ~40ms. 단시간 제한 미발생(문서화된 한도는 없음 → 보수적 폴링 권장).

## 사용 가능 / 사용 불가 지표 (acceptance #2)

**사용 가능 (high value):**

- **디지털자산 지수** — `index/master` 카탈로그 63개. market(10) 중 알트시즌 직접 관련:
  - `IDX.UPBIT.UBMI` (Upbit Market Index, 2017-10-01~)
  - `IDX.UPBIT.UBAI` (**Upbit Altcoin Index**, 2017-10-01~) ← 알트시즌 직접 프록시
  - `IDX.UPBIT.UTTI`(Top10) / `IDX.UPBIT.UTHI`(Top30) / `UPBIT_COMP` / `UPBIT_ALT` / `UPBIT10` / `UPBIT30`
  - theme(5: `THMIDX*`) / strategy(7: `UBSI00*`) / sector(41: `SCTIDX*`) 인덱스
- **지수 요약통계** — `index/summary`는 지수별 `dailyYield/weeklyYield/monthlyYield/quarterlyYield/yearlyYield`,
  `winningRate`, `volatility`, `beta`, `sharpeRatio`, `alpha`, `trackingError`, `informationRatio` 제공 → 레짐/로테이션 분석에 풍부.
- **기간별 상승률** — `interval_change_rate` 745마켓 × 다기간 returns → **breadth/알트시즌 비율 계산 가능**.
- **로테이션** — `weekly_change_rate` BTC마켓 상승률 랭킹.
- **체결강도(매수/매도 압력)** — `daily_volume_power`.

**사용 불가 / 범위 밖:**
- 청산(liquidations), 온체인, ETF, stable flow — 이 이슈 non-goal.
- 공식 Open API에는 지수 엔드포인트 없음(`api.upbit.com/v1/index` → **404**). 지수는 위 비공식 웹 호스트에만 존재.

## 엔드포인트 안정성 / 약관 / 스크래핑 리스크 (acceptance #3)

- **비공식·비문서**: `crix-api-cdn` / `datalab-static` / `crix-static`는 문서화된 Open API(`api.upbit.com`)가
  아니라 웹 프론트엔드 구동용 내부 엔드포인트. 버전·하위호환 보장 없음, 예고 없이 변경/삭제 가능.
- **robots.txt** (핵심 리스크):
  - `crix-api-cdn.upbit.com/robots.txt` → `User-agent: * / Disallow: /` (**전체 차단**)
  - `www.upbit.com/robots.txt` → `Disallow: /` (Googlebot/Yeti 등 검색엔진만 Allow)
  - `datalab-static.upbit.com/robots.txt` → 404 (robots 파일 없음, S3 버킷)
  - 해석: robots는 1차적으로 크롤러/색인 정책이지만, 본 이슈가 요구한 약관/스크래핑 리스크 평가 관점에서
    `crix-api-cdn`/`www.upbit.com`은 **명시적 disallow 신호**. `datalab-static`(S3 정적 데이터 제품)은 제약 없음.
- **rate-limit 미문서**: 한도 불명 → 보수적 폴링 + 장기 캐시 필수.

## auto_trader 통합 시 최소 계약 (acceptance #4)

기존 ROB-377 패턴을 그대로 따른다 (근거 코드):
- 서비스: `app/services/external/btc_dominance.py` (CoinGecko `/global`, in-memory 캐시 + TTL + fail-open) 미러.
- MCP handler: `app/mcp_server/tooling/fundamentals/_crypto.py`의 **4-layer fail-open**
  (validate → normalize → fetch → catch→`error_payload`) 미러. `app/mcp_server/tooling/shared.py:error_payload`.
- collector(선택): `app/services/investment_snapshots/collectors.py:SnapshotCollectorProtocol`,
  `app/services/action_report/snapshot_backed/collectors/registry.py:production_collector_registry`.
- Upbit REST 베이스: `app/services/brokers/upbit/client.py` (`https://api.upbit.com/v1`).

**제안 tool/collector 이름 후보**
- `get_upbit_index` — 지수 카탈로그/최근값/요약(UBMI/UBAI 등).
- `get_upbit_altseason` — UBAI/UBMI ratio + breadth(알트>BTC 비율) 파생 지표.
- collector: `UpbitIndexSnapshotCollector` (snapshot_kind 예: `crypto_market_index_upbit`).

**반환 payload shape (예시)**
```jsonc
{
  "source": "upbit_datalab",            // 또는 "upbit_open_api" (breadth 자체계산분)
  "as_of": "2026-05-31T01:20:00+09:00", // 응답 timestamp/candleDateTimeKst 유래
  "provenance": "unofficial_web_endpoint", // 정직한 출처 표기
  "indices": {
    "UBMI": {"value": 11101.28, "daily_yield": 0.0058, "weekly_yield": ...},
    "UBAI": {"value": 7159.12,  "daily_yield": ...,    "beta": ..., "sharpe": ...}
  },
  "altseason": {
    "ubai_ubmi_ratio": 0.645,
    "krw_alts_beating_btc_7d_pct": 0.42, // interval_change_rate 또는 /v1/ticker 자체계산
    "method": "interval_change_rate" | "open_api_ticker_derived"
  }
}
```

**source / timestamp / provenance**: `source`는 호스트별로 구분(`upbit_datalab` vs `upbit_open_api`),
`as_of`는 응답 내 timestamp/`candleDateTimeKst`에서 파생, `provenance`에 비공식 엔드포인트임을 명시.

**cache / fail-open / timeout 정책**
- cache TTL: 지수(`datalab-static`)는 응답 `max-age=60`에 맞춰 **60–300s** in-memory 캐시.
  파생 breadth는 5분 캐시.
- fail-open: 실패 시 예외 대신 `error_payload`/`unavailable` 반환(리포트 생성 무중단). 직전 캐시값 fallback.
- timeout: httpx 10s(기존 클라이언트 관행), 1회 재시도 한정.
- partial: 일부 엔드포인트 실패해도 가용 지수만 채워 partial 반환.

## fixture / test 가능 여부 (acceptance #5)

가능. 전부 결정적 JSON이라 파서 테스트가 쉽다. 정제 샘플 저장:
`tests/fixtures/upbit_index/` — `index_master.json`, `index_summary_sample.json`,
`index_recent_sample.json`, `interval_change_rate_sample.json`, `weekly_change_rate_sample.json`,
`daily_volume_power_bid_sample.json`, `index_candles_ubai_sample.json` (모두 공개 데이터, secret 無).
파서 테스트 스켈레톤: `tests/test_rob381_upbit_index_recon.py`.

<a name="final-verdict"></a>
## Final verdict — `implement` (분할)

데이터 가치가 높고(UBAI=알트시즌 직접 프록시, 지수 요약통계 풍부), 무인증·빠르고·fixture화 쉬우며
공식 API엔 지수가 없다. 다만 비공식 엔드포인트 + `crix-api-cdn`/`www.upbit.com`의 robots `Disallow`가
실재하는 리스크다. 따라서 **소스별로 분할**해 구현한다 (operator 승인 verdict).

| 데이터 | PR2 소스 | 근거 |
|---|---|---|
| 디지털자산 지수 / UBAI·UBMI / 요약통계 / 알트시즌 ratio | **`datalab-static.upbit.com`** | robots 제약 없음(S3 공개 데이터 제품), 캐시 가능, 안정적 |
| breadth(알트>BTC 비율) / 기간별 상승률 | **공식 Open API `api.upbit.com/v1/ticker`로 자체 계산** | robots-disallowed `crix-api-cdn` 회피, 이미 보유한 클라이언트 |
| 체결강도(`daily_volume_power`) | **park** | `crix-api-cdn` robots Disallow, 부가가치 낮음 → 보류 |

## Recommendation (다음 단계, PR1에서는 구현 안 함)

1. **PR2**: `datalab-static` 지수 + 공식 `/v1/ticker` breadth 자체계산으로 `get_upbit_index` /
   `get_upbit_altseason` MCP read tool 구현 (ROB-377 4-layer fail-open + in-memory 캐시).
2. **PR3(선택)**: `UpbitIndexSnapshotCollector`를 `production_collector_registry`에 등록 → Hermes crypto market dim 보강.
3. 모든 호출에 식별 가능한 User-Agent + 보수적 폴링(≥60s) + 장기 캐시. robots-disallowed 호스트는 사용하지 않음.

## Non-goals / Boundaries honored (acceptance)

- PR1에서 production MCP tool/collector 미구현 (read-only 문서 + fixture + 파서 테스트만).
- broker/order/watch/order-intent mutation 없음. Upbit private/account/order API 미사용.
- scheduler/TaskIQ/Prefect/cron activation 없음. production DB write/backfill 없음.
- secret 출력/저장/커밋 없음. liquidations/온체인/ETF/stable flow는 범위 밖.
