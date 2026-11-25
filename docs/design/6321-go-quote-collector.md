# 6321 — Go 시세 수집기(fillwire) 설계

- hk task: 6321 (grade S, T1). 문서만이며 코드·배포·env·스케줄러 변경은 없다.
- 운영자 결정: `hk:doc decision-request/2026-11-25/at-market-data-collection-direction` Q2=예(+Q1·Q3·Q4 예, 키움·NH 분산 조건부 승인).
- dots 입력: hk 6299 c32787(계층·예산·limiter·승격 시 웹소켓·백필 규칙), hk 6039 c32741(KR 후보 관측·fail closed), c32786(US 데이마켓 필드 분리).
- 근거 코드(읽기 전용): auto_trader `7462720fe`, fillwire `origin/main 8a9dea1`.
- 표기: 코드나 공식 문서로 확인하지 못한 주장은 **[unverified]** 로 표시한다.

## 0. 현재 상태 (사실)

- Python 세션 분봉(`app/services/research_candles/session_minutes_collector.py`, hk 6145/6299): 5분마다 KRX 150 + NXT 42 ≈ 192 REST 호출, 실행 1회 ≈ 54초, ≈ 3.5 호출/초. 상한을 정하는 것은 순차 실행 시간이지 브로커 한도가 아니다. 닫힌 봉만(`bar_start + 1분 + 10초 <= 요청 직전`, `CLOSE_GRACE`) 삽입 전용 `ON CONFLICT DO NOTHING` 으로 쓴다.
- Python 선물 분봉(`futures_minutes_collector.py`, hk 6148): 주간만, 야간은 `unsupported`.
- auto_trader KIS limiter 기본값: 전역 19/초(`kis_rate_limit_rate`, "20-1 안전 마진"), 분봉 TR `FHKST03010230` 20/초, 현재가 TR `FHKST01010100`·`HHDFS00000300` 은 1/0.2초(동시 버스트 실측 실패, ROB-753) — `app/core/config.py`. KIS 공식 REST 한도(실전 20건/초)는 **[unverified]**: 레포 주석만 근거이고 공식 문서(https://apiportal.koreainvestment.com/docs)에서 숫자를 확인하지 못했다.
- fillwire 현재: KIS 체결 웹소켓(`H0STCNI0` live / `H0STCNI9` mock, `fills:kis`), 같은 KIS 세션에 선택적 `H0STASP0` 호가 1종목(`[books]`, `docs/book-feed.md`), Toss 시세 리더(`trade:kr`·`orderbook:kr`·`trade:us`·`orderbook:us`, 최대 40종목, `quotes:toss`), Upbit 공개 웹소켓 리더(`internal/upbit`)와 shadow 실행기(hk 5439), `gatewayd`(토큰 발급 단일화), `cmd/market-tap`(빈 예약 이름). **분봉 수집기는 없다.**
- 수집되지 않는 것: US 데이마켓(KIS 주간거래, hk 6322), Upbit 전 KRW 1분봉(hk 6036).
- 키움 live 읽기 전용 클라이언트: `app/services/brokers/kiwoom/live_market_data.py`(차트 api-id `ka10080/81/82/83` 만, dispatch 시점 게이트, 계좌번호 없음). NH: 고정 모의 데이터 호스트 `moapi.nhplug.com:8443` 의 읽기만(broker-and-ledger 계약 Stage 1).

## 1. 데이터 형태

### 1.1 저장 표 — 기존 research 표를 그대로 쓴다

| 표 | PK | 비고 |
|---|---|---|
| `research.kr_candles_1m` | `(time_utc, symbol, venue)` | venue KRX/NTX, `session_segment`, `value` 는 증명될 때만(아니면 NULL) |
| `research.us_candles_1m` | `(time_utc, symbol, venue)` | `session_date`=ET 거래일, venue NASD/NYSE/AMEX, `US_DAY` 어휘 존재 |
| `research.kr_futures_candles_1m` | `(time_utc, symbol)` | hk 6148 |
| (신규, T2 티켓) `research.crypto_candles_1m` 또는 기존 crypto 분봉 표 | — | Upbit 전 KRW. 어느 표인지는 hk 6036 이 정한다 |

- Go 는 DB 에 직접 쓰지 않는다(fillwire 원칙: 체결도 auto_trader HTTP ingest 경유). 분봉은 새 **토큰 인증 research 분봉 ingest 엔드포인트**(T2)로 보내고, 그 엔드포인트가 기존 `SqlMinuteWriter` 와 같은 `ON CONFLICT DO NOTHING` 을 쓴다. 따라서 최초 수신 `retrieved_at` 은 먼저 도착한 쪽(Python 또는 Go)이 고정한다.
- 시세(체결·호가 틱)는 DB 에 넣지 않고 Redis Stream(`quotes:kis:<symbol>`, 기존 `book:kis:<symbol>`·`quotes:toss` 와 같은 모양, `MAXLEN ~`)과 선택적 JSONL 녹화에 둔다. 장기 저장은 별도 결정.

### 1.2 행 필드

| 필드 | 의미 | 규칙 |
|---|---|---|
| `event_at` | 경제 시각: 봉 시작(`time_utc`) 또는 틱의 거래소 시각 | 브로커가 날짜를 안 주면 KST 수신일로 추론하고 `event_date_basis=inferred` (book-feed 규칙과 같음) |
| `received_at` | 수집기가 응답을 받은 실제 시계 | 재시도·백필이 더 이른 시각을 주장할 수 없다. 표에는 `retrieved_at` 으로 저장 |
| `source` | `KIS`·`TOSS`·`KIWOOM`·`NH_MOCK`·`UPBIT`·`ALPACA_INDICATIVE` | 저장 CHECK 확장은 해당 소스 티켓에서. 동일성 미증명 소스는 KIS 로 라벨하지 않는다(§5) |
| `venue` | KRX/NTX, NASD/NYSE/AMEX, US 데이마켓 venue(§4.4), UPBIT | 다른 venue 봉을 합치지 않는다 |
| `session_segment` | `session_segment_of()` 와 같은 규칙 | Go 구현은 Python 표(`session_segment.py`)의 골든 픽스처로 일치 검증 |
| `batch_id` | `go-<collector>-<run>` | 백필은 `%backfill%` 를 넣어 진단의 최초 수신 계산에서 빠지게 한다 |
| `fetch_kind` | `poll`·`ws_agg`·`backfill` | 진단용(Redis/JSONL 메타, 표 열 추가는 T2에서 결정) |

틱(웹소켓) 필드: `symbol, venue, event_at, received_at, price, qty, side(가능 시), bid1..5/ask1..5(호가), feed(=tr id), indicative(bool), delayed(bool)`. dots c32786 대로 indicative/firm, delay, stale, 세션 라우팅 가능 여부를 별도 필드로 둔다. 가격·수량은 정확한 10진 문자열.

## 2. limiter·구독 예산·요청 합치기·백오프·회로 차단

새 패키지 `internal/ratebudget`(T1). 키는 `(provider, account_ref, endpoint)` — endpoint 는 `TR_ID|path` (auto_trader `kis_api_rate_limits` 와 같은 키 모양).

- **토큰 버킷 계층**: provider×account 전역 버킷(예: KIS 실전 19/초) 아래 endpoint 버킷. 한 요청은 두 버킷을 모두 통과해야 한다. **연구 몫**: Python 서빙 프로세스가 같은 키를 쓰므로 Go 는 전역 버킷의 고정 몫만 쓴다(data-plane `budget.py` 선례, 기본 KIS 25% 이하 — 수치는 T1 설정, 운영자 결정 전까지 보수적). 프로세스 사이 공유가 필요하면 Redis 슬라이딩 윈도(Toss limiter 선례)로 하고, Redis 장애 시 로컬로 강등하되 몫을 더 줄인다.
- **자격증명 교체 금지**: 같은 브로커의 여러 키를 돌려 한도를 피하지 않는다(운영자 결정). 버킷 키의 `account_ref` 는 설정에서 하나만 허용하고, 같은 provider 에 두 번째 키 설정은 기동 거부(exit 78).
- **구독 예산**: provider×session 별 웹소켓 등록 수 상한. 체결통보·기존 호가가 먼저 자리를 차지하고(예약), 남은 자리만 승격 종목에 준다. 넘으면 낮은 우선순위부터 해제(§3.3).
- **요청 합치기(singleflight)**: 같은 `(endpoint, 파라미터)` 진행 중 요청은 하나로 합치고 결과를 공유. 같은 분에 같은 심볼 두 번 폴링하지 않는다.
- **Retry-After/백오프**: HTTP 429·KIS 초과 msg_cd(auto_trader `do_not_retry_rate_limit_msg_codes` 목록 재사용)는 해당 endpoint 버킷을 `Retry-After`(없으면 지수 백오프 1s→30s, 지터 20%) 동안 닫는다. 5xx·타임아웃은 같은 백오프, 4xx(429 외)는 재시도 없음.
- **회로 차단**: endpoint 별 연속 실패 N(기본 5) → open(30초) → half-open 1건. provider 전역은 endpoint 회로 과반이 open 이면 open. 회로 open 중 건너뛴 심볼은 §7 의 `budget_cut` 과 구분된 `circuit_open` 으로 보고.

## 3. 수집 방식

### 3.1 REST 분봉 동시 폴링

- 5분 순차 대신 **매분** 동시 폴링: 대상 N 종목을 worker pool(크기 = 버킷 rate) 로 돌린다. 192 호출을 KIS 연구 몫 5/초로 보내면 ≈ 40초 — 매분 실행은 몫 ≥ 4/초가 필요하므로 시작은 5분 주기 유지 + 동시화로 실행 시간만 줄이고, 매분 전환은 패리티 뒤(§6).
- 닫힌 봉 규칙·첫 분 거래대금 규칙·NULL `value` 규칙은 Python 과 같다(골든 픽스처 공유). KIS 분봉 1회 ≈ 30봉이므로 새 종목은 `end_time` 커서 한 페이지 더(hk 6154 규칙).
- 대상 선정은 Go 가 하지 않는다: auto_trader 의 `build_targets`(hk 6299 계층: 고정 > 장중 승격 > 스크리너 > pick log) 결과를 읽기 전용 HTTP 로 받는다(T3). 계층 규칙의 정본은 하나.
- **백필(dots c32787)**: 30~60분 직전 분봉은 REST 여유에서만, `batch_id` 에 `backfill` 을 넣어 실시간 수신처럼 과거 시각으로 당기지 않는다. 재접속 복구가 우선.

### 3.2 웹소켓 체결·호가 (승격 종목)

- 연속 틱은 웹소켓, REST 는 재접속 복구용(dots).
- KIS: 체결가 `H0STCNT0`·호가 `H0STASP0`(KRX), NXT/통합 TR 은 [unverified]. fillwire 의 **기존 KIS 세션을 공유**한다(`[books]` 가 이미 같은 세션에 `H0STASP0` 을 추가하는 선례). 두 번째 세션을 열지 않는다(`OPSP8996` 세션 점유 → exit 42 계약 유지).
- KIS 세션당 실시간 등록 상한: **[unverified]** — 흔히 알려진 값은 "1세션 41건"이나 공식 문서(https://apiportal.koreainvestment.com/docs, 로그인 필요)에서 확인하지 못했다. 구현 티켓 T5 의 첫 Acceptance 가 이 숫자를 공식 문서 URL 로 확인하는 것이다. 예산: 상한 − 체결통보 1 − `[books]` 1 − 안전 여유 2 = 승격 종목 슬롯(체결+호가 2건/종목).
- Toss: 계정당 웹소켓 2연결, 3번째가 가장 오래된 것을 끊는다, fillwire 가 유일한 클라이언트(1연결) — 근거 Toss AsyncAPI 1.2.2 https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json (fillwire README). 종목 40 상한은 fillwire 설정 상한이며 Toss 공식 구독 상한은 [unverified].
- Upbit: 공개 웹소켓, 인증 없음(`internal/upbit`). 연결·메시지 rate 한도는 [unverified] (https://docs.upbit.com/kr/reference/rate-limits 확인 필요).

### 3.3 승격·강등

- 승격 입력: hk 6154 최초 포착 + hk 6299 장중 승격 계층. 슬롯이 차면 가장 낮은 순위부터 해제, 최소 유지 시간(기본 15분) 뒤에만 강등. 고정 계층은 강등하지 않는다.
- 해제·강등·예산 탈락은 이유와 함께 기록(`budget_cut`).

## 4. 브로커 역할

| 브로커 | 역할 | 한도 근거 |
|---|---|---|
| KIS 실전 | KR/US 분봉 REST 1순위, KR 체결·호가 웹소켓, US 데이마켓 분봉(§4.4) | REST 20/초 [unverified], 웹소켓 등록 상한 [unverified] |
| Toss | 캔들 fallback·2차 소스, KR/US 체결·호가 웹소켓(기존 리더) | 웹소켓 2연결(AsyncAPI 1.2.2 URL 위), ORDER 10TPS(https://developers.tossinvest.com/docs, broker-and-ledger 계약, 2026-11-10 확인); 시세 REST 한도 [unverified] |
| Upbit | crypto 전 KRW 1분봉(웹소켓 집계)·체결 | [unverified] |
| 키움 live 읽기 전용 | **동일성 증명된** KR 분봉의 부하 분산(Stage 2 수집만 승인). 차트 api-id 만, 계좌번호 없음 | 실전 TR별 5회/초·세션 1개(https://openapi.kiwoom.com/intro, 2026-11-10 확인, broker-and-ledger 계약) |
| NH(모의 데이터 호스트) | 동일성 증명된 데이터만 보조. `moapi.nhplug.com:8443` 고정 읽기 경로만, 주문 게이트 off 유지 | [unverified] |
| Alpaca | US overnight/BOATS **indicative 참고만**, 저장 시 `ALPACA_INDICATIVE` + `indicative=true`, 병합 금지 | [unverified] |

- 키움 Stage 1 가드(차트 api-id allowlist, dispatch 시점 게이트, 호스트·경로 전송 직전 재검증, `follow_redirects=False`, 계좌번호 부재)는 Go 포팅에서도 그대로 정적 테스트로 고정한다. Go 쪽 구현이 Python 클라이언트를 확장하는 것이 아니라 별도 읽기 전용 클라이언트여야 한다.
- 폴백 사용 시 행의 `source` 가 실제 소스다(Q4).

### 4.4 US 데이마켓(hk 6322)

- KIS 주간거래(겨울 KST 10:00~18:00, dots c32786) 분봉/시세 TR·거래소 코드는 **[unverified]** — auto_trader 코드에 데이마켓 거래소 코드가 없다. T6 의 첫 Acceptance 가 KIS 공식 샘플(https://github.com/koreainvestment/open-trading-api) 에서 TR·코드를 확인하는 것이고, 없으면 `unsupported` 로 드러낸다(선물 야간 선례).
- 정규장 closed ≠ 데이마켓 closed. `session_segment=US_DAY`, `session_date` 는 기존 20:00 ET 롤오버 규칙.

## 5. 소스 동일성 게이트 (hk 6323 연동)

- 섞기 전에 데이터 종류(분봉 OHLCV, 호가)별로 KIS 와 같은 `(symbol, venue, minute)` 을 비교한다. 형성 중 봉과 수정주가 이력은 다르다고 알려져 있다(키움↔KIS 2026-08-03 실측: 최신 봉 제외 시 100%, 068270 수정주가 반올림 차이).
- 판정 단위: `(source, data_type, market, venue)`. 비교는 닫힌 봉만, 최소 표본(예: 20종목 × 3세션) — 수치는 hk 6323 이 정한다.
- 상태: `proven_identical`(이 키로 KIS 대신 수집해 `source=<자기>` 로 저장하되 소비자가 KIS 와 같은 값으로 취급 가능) / `not_proven`(자기 라벨로만 저장, 병합 금지) / `diverged`(수집 중단, 알림).
- 🔴 proven 이어도 행의 `source` 를 `KIS` 로 바꾸지 않는다. 동일성은 판정 표(hk 6323)에 있고 행에는 없다.
- 게이트 판정 표를 Go 가 읽기 전용으로 받아, `not_proven` 소스로는 부하 분산 스케줄을 짜지 않는다.

## 6. Python 병행·패리티·전환

- 단계 0(shadow): Go 는 분봉을 ingest 하지 않고 JSONL/Redis 로만 쓴다. Python 이 그대로 권한자.
- 단계 1(병행 쓰기): Go 도 ingest. 표는 삽입 전용이라 먼저 온 쪽이 남는다. 진단(`diagnose_session_coverage.py`)의 최초 수신 p50/p95 로 비교.
- **side-by-side replay(T4)**: 같은 세션에 대해 Go JSONL 과 research 표(Python 행)를 `(time_utc, symbol, venue)` 로 조인해 OHLCV·volume·value·session_segment 일치율, Go 누락/Python 누락, 수신 지연 차이를 낸다. 읽기 전용.
- **전환 기준(모두 AND)**: ① 연속 5 거래일 닫힌 봉 OHLCV 일치 100%(형성 중 봉 제외) ② 고정 계층 결측 0 ③ Go 의 429·회로 open 이 Python 보다 많지 않음 ④ 최초 수신 p95 가 Python 이하 ⑤ 서빙 프로세스의 KIS 429 증가 없음. 충족 후 Python DAG 정지는 운영자 결정(스케줄러는 Dagu 사본, 하드룰 6).
- 롤백: Go 수집기 정지만으로 Python 으로 돌아간다(Python 코드는 전환 뒤에도 한동안 유지).

## 7. 배포(pi)와 실패 모드

- 배포: pi(shiftgate)에 fillwire 이미지로 새 컨테이너 1개(`quote-collector`), digest 고정(fillwire `docs/digest-pin-deploy.md`). 메모리 상한 256MiB 목표, Redis Stream `MAXLEN ~` 로 메모리 경계 — 실측 전 수치는 [unverified]. 토큰은 gatewayd fetch 모드(메모리 보관)만, 발급하지 않는다. 배포·env 는 별도 운영자 승인.
- 실패 모드는 서로 다른 닫힌 코드로 보고한다(dots: 각각 분리):

| 코드 | 조건 | 처리 |
|---|---|---|
| `rate_limited` | 429·KIS 초과 msg_cd | 버킷 닫힘(Retry-After), 재시도 |
| `circuit_open` | 연속 실패 | 그 endpoint 건너뜀, 다음 half-open |
| `feed_gap` | 웹소켓 끊김/무수신 > N초 또는 REST 응답에 기대 분 누락이 있으나 거래 있음 | REST 복구 폴링, 갭 구간 기록 |
| `no_trades` | 브로커가 정상 응답했으나 그 분 거래 없음 | 결손 아님, 행 없음 그대로 |
| `budget_cut` | 예산·구독 슬롯 부족으로 대상에서 빠짐 | 종목·계층과 함께 보고 |
| `fetch_error` | 그 밖의 오류 | 예외 클래스만 기록 |
| `unsupported` | 소스가 세션·TR 을 제공하지 않음 | 0행을 조용히 두지 않음 |

진단 사유 어휘는 hk 6299 의 `fetch_error`·`no_trades_reported`·`cut_by_budget`·`not_selected` 와 매핑표를 둔다(T4).

## 8. T3 확인 — 주문에 닿지 않는다

- 수집기 바이너리는 주문·정정·취소·원장 쓰기 경로를 링크하지 않는다: `bookrec` 의 `noorder_test.go` 선례대로 import 폐쇄 allowlist 테스트(go-kis 의 주문 패키지, `internal/exec*`, `internal/kislive`, `kismockedge` 금지).
- KIS 는 `/uapi/.../quotations/` 경로 GET 만, TR id 끝이 `R`·`FH*`/`HH*` 시세만 허용(kisread 선례). 키움은 차트 api-id 만, NH 는 고정 읽기 경로만. Toss 는 토큰을 발급·갱신하지 않는다(1토큰 규칙).
- auto_trader 측 새 엔드포인트는 research 분봉 표 INSERT 만, 주문·원장·승인 모듈 import 없음(정적 테스트). 따라서 이 설계의 모든 티켓은 T1/T2 이며 T3(주문 경로)이 아니다.

## 9. 티켓 분해 (PR 하나 = 티켓 하나)

hk project `auto_trader`, lane `auto_trader-design`. 각 티켓 본문(Current/Desired/Acceptance/Out of scope)은 hk task 의 첫 댓글에 있다.

| # | hk | 레포 | 제목 요지 | 선행 |
|---|---|---|---|---|
| T1 | 6342 | fillwire | `internal/ratebudget`: provider/account/endpoint limiter·구독 예산·singleflight·Retry-After·회로 차단 (grade A, T1) | — |
| T2 | 6343 | auto_trader | research 분봉 HTTP ingest(토큰, 삽입 전용, source CHECK 확장) + 대상 목록 읽기 엔드포인트 (grade A, T2) | — |
| T3 | 6344 | fillwire | KIS REST 분봉 동시 폴러 shadow(JSONL/Redis, ingest off) (grade A, T2) | T1 |
| T4 | 6345 | auto_trader | Go↔Python side-by-side 패리티 replay + 실패 사유 매핑 (grade B, T1) | T3 |
| T5 | 6346 | fillwire | KIS 웹소켓 승격 종목 체결·호가(공유 세션, 등록 상한 공식 확인) (grade A, T2) | T1 |
| T6 | 6347 | fillwire | US 데이마켓 분봉 소스(KIS 주간거래, 없으면 unsupported) — hk 6322 연동 (grade A, T2) | T3 |
| T7 | 6348 | fillwire | 키움 live 읽기 전용 분봉 소스(Stage 2 수집, 동일성 게이트 뒤) (grade A, T2) | T1, hk 6323 |
| T8 | 6350 | fillwire | NH 모의 데이터 호스트 분봉 읽기 소스(동일성 게이트 뒤) (grade B, T2) | T1, hk 6323 |
| T9 | 6349 | fillwire | pi 배포 사본(컨테이너·메모리 상한·digest 고정) + 전환 런북 (grade B, T2) | T3, T4 |

- Upbit 전 KRW 1분봉은 기존 hk 6036 에, 동일성 게이트는 기존 hk 6323 에 둔다(새 티켓 없음). hk 6321 이 이 티켓들의 부모다.

## 10. 범위 밖

코드, 배포, env 변경, 스케줄러 등록, 주문 경로, 과거 백필 일괄 실행.
