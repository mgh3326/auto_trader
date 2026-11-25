# 세션 분봉 수집 런북 (hk 6145)

KR·US 닫힌 1분봉을 `research.kr_candles_1m` / `research.us_candles_1m` 에 세션 표지·최초 수신 시각과 함께 쌓는다. 계약은 `docs/agent-contracts/market-data-and-screener-contracts.md` 「세션 분봉 타임라인」.

## 1. 적용 순서 (운영자·설계 세션)

1. 코드 배포 뒤 마이그레이션 `20261124_hk6145_session_minutes` 적용(`uv run alembic upgrade head`). 추가형이며 `public.*_candles_1m` 은 바뀌지 않는다.
2. 고정 목록은 기본값이 벤치마크다(hk 6299): KR `069500,229200`, US `SPY,QQQ,SMH`. 다른 종목을 넣을 때만 env 를 둔다(`SESSION_MINUTES_FIXED_SYMBOLS_KR/US`, 쉼표 구분 — 값을 주면 기본값을 대체한다). 선택: `SESSION_MINUTES_SCREENER_TOP_N`(기본 20), `SESSION_MINUTES_MAX_SYMBOLS_KR/US`(기본 150, 상한은 바꾸지 않는다), 단계 예산 `SESSION_MINUTES_BUDGET_FIXED`(50)·`SESSION_MINUTES_BUDGET_INTRADAY`(40, KR 만)·`SESSION_MINUTES_BUDGET_SCREENER`(60). pick log 는 나머지다.
3. 수동 확인(DB 에 쓴다): `python -m scripts.collect_session_minutes --market kr --dry-run` 으로 대상·읽기 수를 확인한 뒤 `--dry-run` 없이 1회 실행.
4. DAG 사본(`deploy/dagu/at-session-minutes/`) 등록은 위 1–3 뒤에 설계 세션이 한다(결정 `decision/2026-11-24/at-session-timeline` 항목 1). 저장소는 등록하지 않는다.

## 2. 수집기

```bash
uv run python -m scripts.collect_session_minutes --market kr|us [--symbols 005930 000660] [--dry-run]
```

- 출력 JSON: `inserted`, `already_present`, `skipped_forming`, `skipped_invalid`, `null_value`, `errors`, `error_details`(종목·venue·예외 클래스만), `targeted_symbols`(이번 실행의 대상 전부), `targets_tiers`(단계별 `budget`·`allowance`·`candidates`·`selected`·`cut`), `targets_cuts`(잘린 종목마다 `symbol`·`tier`·`reason`, reason 은 `budget_cut` 하나), `targets_by_origin`, `targets_truncated`. US 실행은 추가로 `targets_decisions`(거래일·달러 하한·종목별 최종 결정과 점수 입력, hk 6554)를 낸다. 종료 코드 0 = 정상, 1 = 오류 있음.
- 단계 순서(hk 6299): ① **고정** = 보유(KIS·수동) + 고정 목록, 절대 자르지 않는다(예산 50 을 넘어도 전부 받고 아래 단계가 줄어든다). ② **KR 장중 승격** = 오늘의 6154 최초 포착, 최초 포착 시각 → 순위 순, 예산 40. 오늘 분봉이 이미 있는 종목은 예산에서 보호되어 같은 날 내려가지 않는다. ③ **장전 스크리너** = 프리셋별 상위 N 의 합집합, 예산 60. ④ **pick log** = 나머지. 쓰지 않은 예산은 다음 단계로 넘어간다(예: 고정이 2개만 쓰면 48 이 승격으로 간다). US 의 ② 는 hk 6554 점수 순서다(오늘 모든 실행의 최고 순위 → 거래대금 하한 버킷, 자산 `right_or_warrant` 제외, 계약 `market-data-and-screener-contracts` 의 hk 6554 절).
- `targets_truncated` 가 0 이 아니면 상한 또는 단계 예산에 걸린 것이다. 잘린 목록은 `targets_cuts` 에 있다.
- 닫힌 봉만 쓴다(`시작 + 1분 + 10초 <= 요청 직전`). 같은 봉을 다시 받으면 `already_present` 이고 `retrieved_at` 은 최초 값 그대로다.
- KIS 분봉 한 번 호출은 최근 구간만 돌려준다(KR 최대 30행, US 최대 120행). 5분 간격 DAG 에서는 겹치지만, 실행이 한동안 멈추면 그 구간은 비며 소급 채움은 없다(진단의 `missing_minutes` 로 보인다).

- KR `value` 는 증명될 때만 채운다(직전 행 차분 또는 venue 첫 분). 그 밖의 분은 봉은 저장하되 `value` 가 NULL 이고 `null_value` 로 센다. 🔴 KR 연속 집계(5m/15m/30m/1h)의 `value` 는 1분봉 `value` 가 NULL 인 분이 있으면 **부분합**이다(`SUM` 이 NULL 을 무시). 집계 값을 거래대금으로 쓰기 전에 진단의 `value_null` 을 확인한다.

## 3. 커버리지 확인

```bash
uv run python -m scripts.diagnose_session_coverage --market kr|us --date 2026-11-24 \
    [--symbols 005930 000660] [--collect-report RUN.json [RUN2.json ...]]
```

`--collect-report` 는 수집기가 출력한 JSON(한 줄에 한 실행, 같은 시장)이다. 선택이다. US 는 그 거래일의 `targets_decisions` 가 `decisions` 로 나온다(hk 6554; 키가 없는 과거 실행은 건너뛴다). 없으면 `requested_zero_row` 키가 나오지 않고 나머지 출력은 같다(`futures-minutes.md` 의 `--symbols` 명령은 그대로 동작한다). `dry_run` 보고는 근거에서 빠진다. 실행별 `runs`(단계 수·잘린 목록)가 나오고, 행이 0개인 요청 종목마다 닫힌 사유 하나가 `requested_zero_row` 에 붙는다.

| 사유 | 뜻 |
|---|---|
| `fetch_error` | 어느 실행에서든 이 종목의 읽기 오류가 있었다(실패는 '거래 없음'이 아니다). |
| `no_trades_reported` | 대상이었고 오류 없이 읽었는데 저장된 분이 없다(거래 없음 또는 형성 중). |
| `cut_by_budget` | 대상이 되지 못하고 단계 예산·상한에 잘렸다. |
| `not_selected` | 이 실행들의 어느 목록에도 없었다. |

읽기 전용(8초 timeout). 종목 × venue × session_segment 별 `rows`, `expected_minutes`, `missing_minutes`, `value_null`/`value_null_ratio`(세션별 NULL `value` 분 수·비율), `latency_p50_s`/`latency_p95_s`(= `retrieved_at − (time_utc + 1분)`). 백필·승격(`%backfill%`, `promote-%`) 배치 행은 제외한다. `UNKNOWN` 세그먼트 행은 예상 분이 0 으로 따로 나온다.

## 3a. NXT 세션 경계 (hk 6298)

NTX 표지는 NXT 공식 시간을 따른다(출처: nextrade.co.kr 거래시스템 안내, 2026-11-25 확인, hk 6039 c32602).

| NTX 분(봉 시작) | 표지 | 비고 |
|---|---|---|
| 08:00–08:49 | `NXT_PRE` | 프리마켓 |
| 08:50–08:59 | `UNKNOWN` | 매매 없는 구간 |
| 09:00–15:19 | `NXT_OVERLAP` | 본장 09:00:30–15:20. 09:00 봉은 09:00:30 이후 체결을 담는다 |
| 15:20–15:59 | `UNKNOWN` | 15:20–15:40 은 NXT 연속매매 없음, 15:40–16:00 은 KRX 종가매매와 겹침 |
| 16:00–19:59 | `NXT_POST` | 애프터마켓(15:40 시작이지만 16:00 까지는 위 이유로 `UNKNOWN`) |
| 20:00 이후 | `UNKNOWN` | |

- 정상 거래일의 NTX 예상 분은 프리 50 + 본장 380 + 애프터 240 = 670 이다. `UNKNOWN` 분은 예상 분에 들지 않으므로 결손이 아니다.
- **15:40–16:00 구분 확인 (hk 6298)**: KIS 주식당일분봉조회(`FHKST03010200`, 공식 샘플 `examples_llm/domestic_stock/inquire_time_itemchartprice`·`domestic_stock_functions.py`의 `inquire_time_itemchartprice`)의 output2 필드는 `stck_bsop_date`·`stck_cntg_hour`·`stck_oprc`·`stck_hgpr`·`stck_lwpr`·`stck_prpr`·`cntg_vol`·`acml_tr_pbmn` 이며 매매 방식(연속/종가)을 구분하는 필드는 없다. 레포 frame 도 이 열들만 남긴다(`_MINUTE_FRAME_COLUMNS`). 그래서 15:40–16:00 은 `UNKNOWN` 이다. `FID_COND_MRKT_DIV_CODE=NX` 조회도 NXT 데이터만 주는 것이지 체결 건별 구분 필드가 아니다. 구분 필드가 문서로 확인되면 이 절과 `session_segment.py` 를 함께 바꾼다.
- 기존 저장 행은 재라벨하지 않는다(백필 없음). 이 변경 이전에 저장된 15:20 이후 NTX 행은 옛 표지(`NXT_OVERLAP`/`NXT_POST`)로 남아 진단의 `rows` 에 들어갈 수 있다.
- 프로모션 경로(`research_candles/promotion.py`의 `classify_session_segment`)는 별도 분류기이며 이 변경 대상이 아니다.

## 3b. 미국 데이마켓 — Toss 1분봉 (hk 6322)

미국 데이마켓(토스 `dayMarket`, 한국 시각 대략 10:00–17:50, 서머타임에 따라 ET 20:00–04:50)은 KIS 해외 분봉이 주지 않으므로 토스 `GET /api/v1/candles?interval=1m` 로 받는다. 설계 결정: 한 통합 피드이므로 실행 venue 를 모르고, venue 는 `TOSS_INTEGRATED` 한 값이다(`NASD`/`NYSE`/`AMEX` 로 쓰지 않는다). 토스 분봉의 `timestamp` 는 봉의 **끝**이고 `[timestamp − 1분, timestamp)` 이므로 저장하는 봉 시작은 `timestamp − 1분`(예: 15:10 끝 봉 = 15:09 시작)이다.

- **마이그레이션 2개(운영자 적용, 자동 배포는 보류한다)**: `20261125_hk6322_us_toss_venue`(venue CHECK 에 `TOSS_INTEGRATED` 를 NOT VALID 로 추가, `us_candles_1m.value` NOT NULL 해제 — 토스 분봉은 봉별 거래대금이 없어 NULL 이다)와 후속 `20261125_hk6322_us_venue_val`(`VALIDATE CONSTRAINT`). 1번을 먼저 적용하고 2번을 적용한다. 되돌리기(downgrade)는 `TOSS_INTEGRATED` 행이나 NULL `value` 행이 있으면 거부한다.
- **달력이 정한다**: 수집 실행마다 `GET /api/v1/market-calendar/US` 를 한 번 받는다(`get_toss_market_calendar`, 조회 날짜는 KST 오늘이고 전·다음 영업일이 같은 응답에 들어 있다). dayMarket 창 안의 봉은 `US_DAY`, 달력의 pre/regular/after 창 안의 토스 봉은 `US_PRE`/`US_REGULAR`/`US_POST`, 어느 창에도 없으면 `UNKNOWN` 으로 저장한다(추측하지 않는다). 휴장일(모든 세션 null)은 창이 없어 `UNKNOWN` 이다. 하드코딩된 데이마켓 시각은 없다.
- **달력을 못 받으면 토스를 읽지 않는다**: 보고서에 `toss: calendar_unavailable` 과 `CalendarUnavailable` 오류가 남고 종료 코드는 1 이다(KIS 쪽 수집은 그대로 한다). `TOSS_API_ENABLED` 가 꺼져 있으면 `toss: disabled` 이고 오류는 없다.
- **KIS 와 토스는 섞이지 않는다**: KIS 행(`NASD` 등)과 토스 행(`TOSS_INTEGRATED`)은 venue 가 달라 PK 가 충돌하지 않고, 출처 `KIS`/`TOSS` 로 따로 보인다. 수집기는 달력의 dayMarket 창이 연 뒤 15분이 지나면 KIS 미국 읽기를 건너뛴다(`kis_skipped_day_market`). 창이 열린 직후 15분은 KIS 를 계속 읽는다: KIS 시간외는 창이 열리는 20:00 ET 에 끝나므로 마지막 KIS 봉(19:54–19:59 ET)을 그때 읽어야 한다. pre/regular/post 는 KIS 가 그대로 맡는다.
- **읽는 범위**: 실행마다 종목별로 현재 시각 3시간 전부터 현재까지(`TOSS_US_LOOKBACK`, 최대 4페이지), 형성 중인 봉은 `is_closed`(시작 + 1분 + 10초) 규칙으로 건너뛴다. 재실행은 `ON CONFLICT DO NOTHING` 이라 0행이고 `retrieved_at`(최초 수신)은 그대로다. 토스 `before` 커서는 현재 시각 + 2분으로 보낸다(형성 중인 봉을 받아 버리기 위해; 클라이언트의 "커서보다 새 봉" 검사를 피한다).
- **DAG 사본**: `deploy/dagu/at-session-minutes/at-session-minutes-us.yaml` 은 KIS 창 04:00–20:10 ET 에 데이마켓 창을 더했다 — 일–금 20:00–23:55 ET 와 월–토 00:00–03:55 ET. 세 줄은 겹치지 않는다. 등록은 여전히 설계 세션이 한다.
- **확인 명령(한 줄)**: 오늘 미국 세션마다 종목·출처·venue 별 첫 봉과 마지막 봉(봉 시작, UTC·KST):

  ```bash
  uv run python -m scripts.diagnose_session_coverage --market us --date 2026-11-25 --first-last [--symbols NVDA]
  ```

  읽기 전용(8초 timeout). `TOSS_INTEGRATED` 행은 `US_DAY` 세그먼트로, KIS 행과 따로 나온다. 쓰지 않는다.
- **알려진 한계(운영자 스모크로 확인)**: ① BRK.B 같은 점 표기 심볼을 토스가 받는지 `[unverified]`(수집기는 DB 표기 그대로 보낸다; 실패는 종목별 오류로 남는다). ② 토스 `adjusted=false` 로 읽는다(KIS 원시 가격과 맞춘다). ③ 이 경로는 KR 이 아니다 — KR 세션과 선물은 건드리지 않는다. ④ 토스 행은 기대 분(expected) 계산에 들어가지 않으므로(`expected_minutes` 는 KIS 표지만 센다) 커버리지 진단은 `--first-last` 로 본다. ⑤ `research.us_candles_1m` 을 읽는 다른 소비자(retro `get_bars`, 신선도 `candles_1m.us`)는 이제 `TOSS_INTEGRATED` 행을 함께 본다.

## 4. 주의

- US `session_date` 는 ET 거래일(20:00 ET 롤오버)이라 KST 달력일과 다를 수 있다.
- `research.us_candles_1m` 의 연속 집계(5m/15m/30m/1h)는 timescaledb 가 있는 DB 에서만 만들어진다. 갱신은 운영자가 명시적으로 한다(자동 refresh 정책 없음).
- 이 기능은 주문·승인·정책 경로와 무관하다. 스케줄러·systemd 등록을 저장소가 하지 않는다.

## 5. Go↔Python 분봉 전환 비교 (hk 6345)

설계 `docs/design/6321-go-quote-collector.md` §6(side-by-side replay). Go `market-tap` 이 쓴
JSONL 과 research 분봉 표를 `(time_utc, symbol, venue)` 로 맞춰 **읽기만** 한다.

```bash
uv run python -m scripts.compare_go_py_minutes --market kr --date 2026-11-24 \
  --go-jsonl /var/lib/fillwire/market-tap/bars.jsonl [--symbols 005930 000660]
```

- 종료 코드: `0` PASS, `3` BLOCK, `4` INCONCLUSIVE(비교 근거 없음), `1` Python 읽기 실패,
  `2` 사용법·Go 파일 오류. 출력 JSON 은 `verdict`, `inconclusive_reasons`, `blocking`,
  `listed`, `cutover`(전환 표), `failure_code_map`.
- Python 쪽은 세션 수집기가 쓴 행(`batch_id` 가 `session-minutes-` 로 시작)만 비교한다. 같은
  표에 들어오는 Go ingest 행(hk 6343)과 promotion·backfill 행은 제외하고 `py_rows_excluded_non_collector`
  로 센다. 이 규칙은 관례다. ingest 가 호출자 제공 `batch_id` 를 받으므로 `session-minutes-` 를 고른
  호출자의 행은 통과한다. 그 경우는 이 CLI 가 잡지 못한다.
- INCONCLUSIVE 는 다음 중 하나일 때다: 한쪽 행이 0개, 겹치는 봉이 0개, 요청 종목이 양쪽 어디에도
  없음. 0 행을 "세션이 모두 덮였다"로 읽으면 안 된다. 이 결과는 PASS 와 같은 근거로 쓰지 않는다.
- SELECT 만 쓴다(`research.kr_candles_1m` 또는 `research.us_candles_1m`). `READ ONLY` 트랜잭션,
  `statement_timeout 8s`. 쓰기·등록·브로커 호출 없음. 정적 테스트
  `tests/scripts/test_compare_go_py_minutes.py` 가 이를 고정한다.
- Go JSONL 은 `--date` 의 거래일과 `--market` 의 venue(KR `KRX`·`NTX`, US `NASD`·`NYSE`·`AMEX`)만 비교한다.

**판정(운영자 2026-11-25 완화 기준)**

| 항목 | 막는가 | 규칙 |
|---|---|---|
| 통째로 없는 종목 (양방향) | 막음 | 한쪽에만 있는 종목 |
| 통째로 없는 세션 (양방향) | 막음 | 한쪽에만 있는 (종목, venue, 거래일) |
| 세션별 행 수 | 아님(목록) | `\|go-py\| <= max(2 봉, 큰 쪽의 2% 올림)` |
| 닫힌 봉 OHLCV 일치 | 아님(목록) | 겹치는 키의 open·high·low·close·volume 비교. 불일치는 키를 목록에 남김 |
| value 차이 | 아님(목록) | 한쪽만 NULL 이거나 값이 다름 |
| 수신 지연 차이 | 아님 | Go JSONL 에 수신 시각이 없어 `unavailable`. 아래 참고 |
| 부분 결손 | 아님(목록) | 한쪽에만 있는 행(통째로 빠진 세션 안의 행은 세지 않음) |

🔴 **판정 해석 두 가지는 운영자 확인이 필요하다**: ① 양방향 차단(Python 에만 있는 세션도 막는다.
"같은 종목·세션 커버"가 PASS 조건이라 방향을 뺐다). ② 닫힌 봉 OHLCV 표본 불일치는 목록만
남기고 PASS 를 바꾸지 않는다(한 봉 불일치는 PASS 와 목록 기록이 수락 기준). 표본 불일치를 막는
규칙으로 바꾸려면 운영자 결정이 먼저다.

**수신 지연**: Go 행의 직렬화 키는 `time_utc, session_date, symbol, venue, session_segment, source,
open, high, low, close, volume, value` 뿐이다(`internal/marketminute/row.go`). 수신 시각을 JSONL 에
넣으면 Python 골든 바이트 계약이 바뀌므로, 이 PR 은 지연을 계산하지 않는다. 전환 기준 ④(최초 수신
p95)는 Go 쪽 수신 시각이 생기기 전까지 측정할 수 없다.

**실패 코드 매핑** (`FAILURE_CODE_MAP`, 테스트가 표를 고정): Go 가 내는 실패 코드를 hk 6299 진단 어휘
`fetch_error`·`no_trades_reported`·`cut_by_budget`·`not_selected` 에 맞춘다. 어휘에 없는 코드는 `None`
(`feed_gap`·`unsupported`)으로 두고 이유를 적는다. `rate_limited`·`circuit_open` 은 `fetch_error`로
접고 코드는 Go 실행 보고에 남긴다. Go 실행 보고(실패 클래스)는 JSONL 에 없으므로 이 CLI 는 표를 싣기만 한다.

| Go 코드 | hk 6299 어휘 | 비고 |
|---|---|---|
| `rate_limited` | `fetch_error` | 429·EGW00201 |
| `circuit_open` | `fetch_error` | 회로 열림 |
| `budget_cut` | `cut_by_budget` | 구독 슬롯·예산 부족 |
| `feed_gap` | 없음 | 갭 구간. 0행 사유가 아님 |
| `no_trades` | `no_trades_reported` | 정상 응답, 거래 없음 |
| `fetch_error` | `fetch_error` | 그 밖의 오류 |
| `unsupported` | 없음 | 소스가 세션·TR 을 제공하지 않음 |

**PASS 의 뜻은 좁다**: 이 CLI 의 PASS 는 "한쪽에 있고 다른 쪽에 통째로 없는 종목·(종목, venue, 거래일)
세션이 없다"는 뜻뿐이다. 설계 §6 전환 기준 ①–⑤ 중 어느 것도 증명하지 않는다. 기준 ①(OHLCV 100%)은
`listed` 의 `ohlcv_mismatch` 가, ②(결측 0)은 `listed` 의 `partial_gap_keys_*` 가, ④(최초 수신 p95)는
`latency` 가 `unavailable` 이라 측정되지 않는다. ③·⑤(429·회로, KIS 429)는 이 CLI 가 보지 않는다.
따라서 PASS 한 번으로 Python 수집을 멈추지 않는다. 기준은 여러 거래일 실행과 `listed` 값을 함께 본 뒤
운영자가 판단한다. 전환 표의 `ok`/`listed` 는 기준 충족 표시가 아니다.
