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

- 출력 JSON: `inserted`, `already_present`, `skipped_forming`, `skipped_invalid`, `null_value`, `errors`, `error_details`(종목·venue·예외 클래스만), `targeted_symbols`(이번 실행의 대상 전부), `targets_tiers`(단계별 `budget`·`allowance`·`candidates`·`selected`·`cut`), `targets_cuts`(잘린 종목마다 `symbol`·`tier`·`reason`, reason 은 `budget_cut` 하나), `targets_by_origin`, `targets_truncated`. 종료 코드 0 = 정상, 1 = 오류 있음.
- 단계 순서(hk 6299): ① **고정** = 보유(KIS·수동) + 고정 목록, 절대 자르지 않는다(예산 50 을 넘어도 전부 받고 아래 단계가 줄어든다). ② **KR 장중 승격** = 오늘의 6154 최초 포착, 최초 포착 시각 → 순위 순, 예산 40. 오늘 분봉이 이미 있는 종목은 예산에서 보호되어 같은 날 내려가지 않는다. ③ **장전 스크리너** = 프리셋별 상위 N 의 합집합, 예산 60. ④ **pick log** = 나머지. 쓰지 않은 예산은 다음 단계로 넘어간다(예: 고정이 2개만 쓰면 48 이 승격으로 간다). US 는 ② 가 없다.
- `targets_truncated` 가 0 이 아니면 상한 또는 단계 예산에 걸린 것이다. 잘린 목록은 `targets_cuts` 에 있다.
- 닫힌 봉만 쓴다(`시작 + 1분 + 10초 <= 요청 직전`). 같은 봉을 다시 받으면 `already_present` 이고 `retrieved_at` 은 최초 값 그대로다.
- KIS 분봉 한 번 호출은 최근 구간만 돌려준다(KR 최대 30행, US 최대 120행). 5분 간격 DAG 에서는 겹치지만, 실행이 한동안 멈추면 그 구간은 비며 소급 채움은 없다(진단의 `missing_minutes` 로 보인다).

- KR `value` 는 증명될 때만 채운다(직전 행 차분 또는 venue 첫 분). 그 밖의 분은 봉은 저장하되 `value` 가 NULL 이고 `null_value` 로 센다. 🔴 KR 연속 집계(5m/15m/30m/1h)의 `value` 는 1분봉 `value` 가 NULL 인 분이 있으면 **부분합**이다(`SUM` 이 NULL 을 무시). 집계 값을 거래대금으로 쓰기 전에 진단의 `value_null` 을 확인한다.

## 3. 커버리지 확인

```bash
uv run python -m scripts.diagnose_session_coverage --market kr|us --date 2026-11-24 \
    [--symbols 005930 000660] [--collect-report RUN.json [RUN2.json ...]]
```

`--collect-report` 는 수집기가 출력한 JSON(한 줄에 한 실행, 같은 시장)이다. 선택이다. 없으면 `requested_zero_row` 키가 나오지 않고 나머지 출력은 같다(`futures-minutes.md` 의 `--symbols` 명령은 그대로 동작한다). `dry_run` 보고는 근거에서 빠진다. 실행별 `runs`(단계 수·잘린 목록)가 나오고, 행이 0개인 요청 종목마다 닫힌 사유 하나가 `requested_zero_row` 에 붙는다.

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

- 종료 코드: `0` PASS, `3` BLOCK, `1` Python 읽기 실패, `2` 사용법·Go 파일 오류.
  출력 JSON 은 `verdict`, `blocking`, `listed`, `cutover`(전환 표), `failure_code_map`.
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

**전환 표 (스모크 수준)**: PASS 면 전환 기준 ①–⑤ 중 Go 쪽이 막히는 항목이 없다는 뜻이다. 전체 기준
(연속 5 거래일 OHLCV 100%, 결측 0, 429·회로 비교, 최초 수신 p95, KIS 429 증가 없음)은 이 CLI 가 아니라
여러 거래일 실행과 설계 §6 의 측정이 판단한다.
