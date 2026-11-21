# 5939 — 선물 T2: 파생 상품·계좌·원장 모델 설계

hk 5939 설계 문서다. **문서만 바뀐다** — 실행 코드·마이그레이션·스케줄러·브로커 호출·env 게이트는 이 PR에 없다. 근거는 `docs/research/5892-futures-options-survey.md` §6·§8.2 T2 와 운영자 결정 `decision-request/2026-11-21/at-futures-expansion`(Q1–Q12, "권고대로 진행"). 기준 SHA는 브리프가 고정한 `113ecf4ec`다.

- **범위**: KIS 국내 선물 **모의(주간)** 한 베뉴의 상품·계좌·원장 모델 정의. 옵션(Q8), 해외선물(Q6·Q9·Q10), 야간 세션(Q7)은 범위 밖이다.
- **후속 티켓과의 관계**: T1은 읽기 어댑터(주문 없음), T3이 이 문서의 모델로 모의 주문 경로를 만든다. T3 구현 전에 이 문서의 `운영자 확정 필요` 값이 채워져야 한다.
- **불변 전제**: 알 수 없는 값은 0이나 생략이 아니라 `unknown`이다. 모호한 체결·송신은 재전송하지 않는다(기존 `unverified` 규율). 귀속 없는 주문은 나가지 않는다(하드룰 7의 선물판).

## 1. 상품 모델 (contract identity)

선물 계약의 정체성은 `(상품 코드, 결제월)`이다. KOSPI200 계열 지수선물만 1단계 대상이며 상품 목록은 닫힌 집합으로 시작한다.

| 필드 | 의미 | 규칙 |
|---|---|---|
| `product_code` | KRX 상품 코드(예: KOSPI200 선물, 미니 KOSPI200 선물) | 닫힌 열거. 목록 밖 상품은 INSERT·주문 거부 |
| `contract_month` | 결제월 `YYYY-MM` | 3·6·9·12월 주기, 최종거래일 = 결제월 둘째 목요일(§5 조사 근거) |
| `contract_symbol` | DB 정본 심볼 | §7 심볼 규약의 `<product>.<YYMM>` 형식 |
| `broker_symbol` | KIS 단축종목코드(`SHTN_PDNO`) | 주식 `PDNO`와 필드가 다르다 — 주식 주문 코드 재사용 금지 |
| `multiplier` | 승수(KRW/pt) | 상품별 상수 테이블. KOSPI200=250,000, 미니=50,000 |
| `tick_size` / `tick_value` | 호가 단위·틱 가치 | 상품별 상수. 가격은 정확한 `Decimal`, float 금지 |
| `last_trade_date` | 최종거래일 | 결제월 달력에서 계산 + KRX 공지 오버라이드(§8) |
| `final_session_close` | 최종거래일 종료 시각(15:20) | 일반일 15:45와 다르다 |

가격·수량·금액은 전부 `Decimal`이다. 명목가 = `price × multiplier × qty`이고 틱 반올림은 상품의 `tick_size`로만 검증한다.

## 2. 계좌·지문 모델

- `account_mode = 'kis_futures_mock'` (신규 닫힌 어휘 값). 기존 `kis_mock`(주식)와 분리된 별도 영역이다 — 같은 KIS 모의 호스트·토큰 계열을 쓰더라도 계좌 상품코드가 다르다.
- **계좌 지문**: KIS 국내선물옵션 계좌는 상품코드 `"03"`이다(조사 §2.1). 송신 경로는 요청 계좌의 CANO·ACNT_PRDT_CD가 설정된 선물옵션 계좌(`"03"`)와 일치할 때만 진행한다. 주식 계좌(`"01"`)로의 전송·주식 계좌 지문과의 혼합은 insert·송신 전에 거부한다.
- 지문 불일치·계좌 미설정·조회 실패는 **사람 카드/거부**이지 기본값 대체가 아니다. 기존 계좌 확인 계약(예상 계좌 검증)을 그대로 적용한다.

## 3. 증거금·평가·정산 모델

선물은 일일정산(marked-to-market) 상품이다. 원장에 필요한 필드와 의미는 아래와 같다.

| 개념 | 필드 | 규칙 |
|---|---|---|
| 위탁증거금 | `initial_margin` | 브로커 조회(`VTTO5105R` 계열) 또는 상품 상수. 값이 없으면 `unknown`, 0 금지 |
| 유지증거금 | `maintenance_margin` | 동일 규칙 |
| 예수금·주문가능 | `deposit`, `orderable` | 조회 실패 시 거래 불가(fail-closed) |
| 평가손익(미실현) | `unrealized_pnl` | `position_qty × (mark − entry) × multiplier`. **실현 필드에 쓰지 않는다** |
| 일일정산 손익 | `settlement_pnl` | 정산가 기준으로 **대사(reconcile)가 계산·기록**한다 — 클라이언트 측 추정으로 쓰지 않는다. 정산가를 얻지 못한 날은 `settlement_unverified` |
| 수수료·세금 | `fee`, `tax` | 브로커 보고값만. 미보고 시 `not_provided`(0 아님) |

**정산 경계**: 실현손익 행은 브로커 체결·정산 증거(REST `VTTO5201R` 계열)가 있는 것만 reconcile writer가 쓴다. 평가손익은 읽기·게이트 계산용이며 영구 ledger에 실현으로 기록하지 않는다.

## 4. 원장 분리안 (Q5)

`execution_ledger`는 선물을 담지 못한다(`broker`·`currency`·`instrument_type` CHECK에 해당 값이 없음, `app/models/execution_ledger.py:58-66`, `app/models/trading.py:19-24`). 운영자 결정대로 **베뉴별 분리 원장**으로 시작하고 `execution_ledger` 확장은 모의 증명 뒤 별도 결정이다.

신규 테이블(전부 `review` 스키마, 서비스 레이어 경유 쓰기만):

| 테이블 | 용도 | 유일 키 |
|---|---|---|
| `kis_futures_mock_signal_ledger` | 송신 전 귀속 신호(`kis_mock_signal_ledger` 선물판). `strategy`·`correlation_id`·`signal_source` 비어 있으면 INSERT 거부 | `correlation_id` |
| `kis_futures_mock_order_ledger` | 주문 생애 원장(전송·ACK·거절·unverified) | `(account_scope, idempotency_key)`, `(account_scope, order_no)` |
| `kis_futures_mock_position_ledger` | 계약별 수량·진입가·증거금 스냅샷(append-only 이벤트) | `(account_scope, contract_symbol, seq)` |
| `kis_futures_mock_settlement_ledger` | 일일정산 결과(reconcile writer만) | `(account_scope, contract_symbol, trading_day)` |

- `order_send_intents`의 `(account_scope, idempotency_key)` 예약 규칙을 그대로 쓰되 `account_scope`에 `kis_futures_mock` 값을 추가한다 — **동일 키 재전송은 UNIQUE 위반으로 fail-closed**.
- uncertain(`acceptance_uncertain`·불량 ACK·원장 기록 실패)은 절대 종결하지 않고 재전송하지 않는다.
- 체결 원장은 별도 ingest 경로(T4)가 `kis_futures_mock_*` 계열에 쓴다 — 기존 `/trading/api/execution-ledger/fills/ingest`는 `execution_ledger` 전용이라 그대로 쓰지 않는다. 새 경로는 같은 토큰 인증·멱등키·항목별 savepoint·`source` 강제 규칙을 복사한다.

## 5. 거래일·세션 경계 (Q7·Q12)

- **1단계 거래일 = KRX 파생 주간 세션 날짜(KST 달력일)**: 08:45–15:45, 최종거래일은 08:45–15:20.
- **야간 세션 제외**(Q7): 18:00–익일 06:00 세션은 날짜를 넘기므로 1단계 모델에 두지 않는다. 야간 시각의 체결·정산은 1단계에서 `night_unsupported`로 분류하며 주간 원장에 합산하지 않는다.
- **파생 달력**(Q12): XKRX 주식 달력을 재사용하지 않는다. KRX 공지 기반 파생 달력(휴장·시간조정 포함)을 별도 테이블/데이터로 두고, **알 수 없는 날은 거래 불가로 fail-closed**. 달력 행이 없는 날짜는 세션 판정을 시도하지 않는다.
- `trading_day` 키는 주간 세션의 KST 날짜다 — 브로커 응답 날짜가 없거나 모호하면 체결을 붙이지 않고 `unverified`로 둔다(기존 귀속 규율과 동일).
- **대사 경계**: reconcile은 `trading_day` 단위로 체결·정산을 묶는다 — `VTTO5201R` 계열 응답의 날짜와 원장 `trading_day`가 다르면 붙이지 않고 대사 불일치로 남긴다. ingest(실시간 통보)는 provisional이고 REST 대사가 권한자다(조사 §6.4).

## 6. 방향 인식 손실 가드 (Q4)

기존 손실매도 가드(`매도 지정가 < 평단 × 1.01`)는 "매도=보유 청산" 전제라 선물에 그대로 쓸 수 없다. **선물 전용 가드를 신설하고 기존 가드는 바꾸지 않는다.**

| 다리 | 판정 |
|---|---|
| 신규 진입(롱 매수·숏 매도) | 가드 대상 아님 — 상한(envelope)만 적용 |
| 청산 매도(롱 포지션 청산) | `limit < entry_price × (1 − g)` 이면 사람 카드 |
| 청산 매수(숏 포지션 청산) | `limit > entry_price × (1 + g)` 이면 사람 카드 |
| 진입가 미상·포지션 귀속 모호 | `unverified` → 사람 카드(면제가 아니라 강등) |

`g`(손실 허용폭)는 운영자 확정 값이다(§9). 방향·기준가가 확인되지 않는 주문은 "청산 편의"로 통과시키지 않는다.

## 7. 심볼·만기·롤오버 규약

- **DB 정본**: `<product>.<YYMM>` (예: `F200.2412` 형태로 상품 짧은 코드 + 결제월). `.` 구분 규칙은 유지하되 파생 세그먼트는 주식의 거래소 접미와 의미가 다르다.
- **변환 경계**: `app/core/symbol.py`에 파생 전용 헬퍼를 추가한다(직접 문자열 치환 금지, 하드룰 8). `broker_symbol`(KIS `SHTN_PDNO`) ↔ `contract_symbol` 변환은 그 헬퍼를 통해서만 한다.
- **만기**: `last_trade_date` 당일에는 신규 진입을 거부하고 청산만 허용한다. 최종거래일 종료(15:20) 이후 주문은 `session_closed`.
- **롤오버(Q11)**: 자동 롤오버 없다. 운영 규칙은 "최종거래일 전에 청산, 다음 결제월은 새 의도(intent)로 연다"이며 만기 임박 통지는 사람 카드·수동 CLI다. 동일 상품의 차월물 보유는 허용하나 `max_open_positions = 1`(§8) 안에서만 가능하다 — 즉 1단계에서는 롤오버 중 동시 보유가 상한에 걸려 자동으로 막힌다. 이것은 의도된 fail-closed다.

## 8. 잠금 상한·인바리언트 (Q3)

기존 상한(`kis_mock` 단건 5,000,000원 등)은 **건드리지 않는다**. 선물 전용 locked 상수를 새로 정의한다 — envelope 패턴(`app/services/kis_mock_runner/envelope.py`)과 같이 env·CLI로 바꿀 수 없는 불변값이다.

| 상수 | 값 | 상태 |
|---|---|---|
| `max_order_qty` | **1 계약** | 운영자 확정(Q3) |
| `max_open_positions` | **1 포지션** | 운영자 확정(Q3) |
| `max_contract_notional_krw` | **50,000,000 KRW** (`price × 50,000 × qty`) | 운영자 확정(`decision-request/2026-11-21/at-futures-t3-values`) |
| `daily_loss_limit_krw` | **1,000,000 KRW** | 운영자 확정(같은 결정) |
| `guard_fraction` (§6의 `g`) | **0.02** | 운영자 확정(같은 결정) |
| 상품 | **미니 KOSPI200 선물만** (승수 50,000, 호가 0.02pt, 틱 가치 1,000원) | 운영자 확정(Q1, 닫힌 집합). 사양 근거: [KRX 미니 KOSPI200 선물 상품명세](https://open.krx.co.kr/contents/OPN/01/01040204/OPN01040204.jsp) — 2026-11-21 직접 확인(결제월 매월 6개, 최종거래일 = 결제월 둘째 목요일, 공휴일이면 앞당김) |
| `limit_only` | `True` | 시장가 금지 |
| `night_session` | `False` | Q7 |
| `options` | `False` | Q8 |
| `auto_rollover` | `False` | Q11 |

일일 손실 한도 초과 시 신규 진입을 멈춘다(`daily_loss_entry_halt` 동등 의미). 손실 한도 판정 기준은 당일 확정 체결·정산 합계이고 미정산·미확정은 `unverified`로 보수 해석한다.

## 9. 열린 항목 · 명시 금지

**열린 항목**(이 문서가 정하지 않거나 운영자 확정이 필요한 것):
1. ~~`max_contract_notional_krw`, `daily_loss_limit_krw`, 손실 가드 `g`~~ — 확정됨(§8, hk 5946). 모의 단계 값이며 실계좌는 새 결정이 필요하다.
2. KIS 모의 주간 주문 TR(`VTTO1101U`/`VTTO1103U`)의 응답 형식·필드 — T1/T3이 샘플 코드 원문으로 재확인(조사 §10).
3. ~~파생 달력의 저장 형식~~ — T3 은 코드 모듈(`app/services/kis_futures_mock/session_calendar.py`)에 범위 + 개장일 표로 두었다(범위 밖 = `unknown` 거부, 범위 안 비개장일 = `closed`). 공지 갱신은 KRX 공지를 근거로 한 PR 이다(런북). 시드 범위는 KRX 공지로 확인되지 않았다.
4. ~~마이그레이션·writer 서비스명~~ — T3 이 정했다: `20261121_hk5946_futures_mock`(주문·귀속 신호 원장 2개), `FuturesMockOrderLedgerService`. 포지션·정산 원장은 T4/T5 다.
5. 해외선물·옵션·야간 세션 모델 — 후속 조사 후 별도 결정.

**명시 금지**(이 PR과 후속 티켓이 하면 안 되는 것):
- `execution_ledger` CHECK 확장, 기존 상한 변경, 손실매도 가드 완화.
- 야간 세션을 주간 거래일에 합산하거나 unknown을 0으로 기록.
- uncertain 상태의 재전송·종결, 귀속 없는 주문, placeholder strategy.
- 스케줄러 등록, 자동 롤오버, 시장가 주문.
- 주식 `PDNO` 경로로 선물 주문 전송.

## 10. 승인 요약 (T3 전 운영자 확인)

- [x] `max_contract_notional_krw` = 50,000,000 KRW (hk 5946)
- [x] `daily_loss_limit_krw` = 1,000,000 KRW (hk 5946)
- [x] 손실 가드 `g` = 0.02 (hk 5946)
- [x] 닫힌 상품 집합 = 미니 KOSPI200 선물만 (일반 KOSPI200 선물은 이 단계에서 제외)
