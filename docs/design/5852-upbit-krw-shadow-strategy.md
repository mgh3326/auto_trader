# hk 5852 Upbit KRW 코인 전략 shadow 사전등록 설계

상태: 설계 초안. 이 문서와 `research/experiments/manifests/hk-5852-upbit-krw-shadow.json` 등록은 초안(draft, k=0)이다. 코드·마이그레이션·프롬프트·스케줄러·위임장 생성·실주문은 이 문서의 범위 밖이고, 구현은 §9의 티켓으로 나눈다.

근거 표기: 코드 근거는 `auto_trader@9677aab2d path:line`, fillwire는 `fillwire@a5a95c1 path:line`이다(§11에 한 곳에 모았다). hk 문서는 `hk:doc <key>`로 인용한다. 코드에서 확인한 것은 **[사실]**, 확인하지 못한 것은 **[모름]**, 추정은 **[추정]**, 이 문서의 권고는 **[의견]**으로 적는다. 확정되지 않은 값은 §10의 번호 매긴 열린 질문(Q)이고 본문에서는 "권고값"으로만 쓴다.

방향 근거: 운영자 질문(2026-11-21, Upbit KRW 전략을 어떻게 사고·팔고·거르고·비가격 데이터를 쓸지)과 "shadow 먼저, 사전등록 설계" 승인. 실행 경로 설계 `docs/design/5438-go-execution-path.md`, 같은 계좌의 실전 소액 설계 `docs/design/4885-upbit-trend-long-live.md`(이 문서와 별개 실험이며 건드리지 않는다), 증거 `docs/research/rob-1458-strategy-evidence-2026-11-02.md`.

## 0. 요약

- **무엇을 비교하나**: 같은 유니버스·같은 청산 규칙 아래 **진입 방식만 다른 두 계열** — (A) 추세 즉시 진입, (B) 같은 추세 안에서 눌림목 진입(물타기 없음) — 과, 각 계열의 **레짐 게이트 켬/끔**. 네 팔(arm).
- **무엇이 성과의 권위인가**: 게이트웨이 shadow 장부가 아니라 **녹화된 호가에 대한 재생 원장**이다(§7.2). 게이트웨이의 `shadowed`는 "지정가에 전량 즉시 체결" 가정이라 대기 주문·손절 시점을 표현하지 못한다(§1.2 F2). 게이트웨이는 **경로 증거**(위임장·상한·가드가 이 주문을 받아줬는가)로만 쓴다.
- **주문은 없다**: shadow 위임장만, `execution_mode='shadow'`만, live 스위치·live 위임장·live sleeve 없음(§7.5).
- **지금 막혀 있는 것(이 문서가 새로 확인)**: fillwire `execsim`은 KIS 전용이라 Upbit 체결 모의가 없다(F1). shadow 매도도 **실계좌** 평단으로 손실매도 가드를 평가한다(F3). 심볼당 열린 매도 intent는 하나라 동시 익절 사다리가 불가능하다(F4). 기록기는 20마켓까지다(F7). 비가격 입력 중 김치 프리미엄·funding 3회 이력은 저장되지 않는다(§6).
- **판정은 하나만**: 진입 창 60일 + 30세션 성숙, 최종 한 번(§8). 통과해도 live는 자동이 아니다.

## 1. 현재 상태

### 1.1 재사용할 재료 [사실]

| 재료 | 위치 | 사실 |
|---|---|---|
| Upbit 의도 v2 | `app/schemas/execution_intent.py:248-296`, 자식 `:298-308` | `account=upbit_live`, `execution_mode` shadow/live(기본값 없음·변경 불가), `limit_price_krw` 호가 단위 검증, `max_slippage_bps ≤ 500`(`:51`) |
| 위임장 상한(D9) | `app/services/execution_intent_gateway/mandates.py:58-62` | intent당 480,000 KRW, 동시 열린 매수 intent 4, 가격 폭 30bp, intent TTL 12시간, 위임장 수명 91일 |
| shadow forwarder | `app/services/execution_intent_gateway/upbit_shadow_forwarder.py:20-34` | 브로커 import 없음. `shadowed=True`만 돌려준다 |
| shadow 장부 | `app/services/execution_intent_gateway/upbit_store.py:351`, `docs/design/5438-go-execution-path.md:250` | `shadowed` submit = 그 `limit_price`에 저널 시각 **전량 체결**로 간주(§3.6). "경로 검증이지 성과 판정이 아니다" |
| 손실매도 가드 | `upbit_decisions.py:65,588-606` | 지정가 < **계정** 평단×1.01이면 `loss_sell_requires_card`, 평단 미증명이면 `avg_price_unproven`. shadow는 인계(handoff)만 안 연다 |
| 평단 읽기 | `upbit_account_reads.py:19-33` | `_get_holdings_for_order(symbol,"crypto")` — 실계좌 보유 |
| 쿨다운 읽기 | `upbit_account_reads.py:36-41`, `app/mcp_server/tooling/order_execution.py:138` | 실계정 Redis, 평단 −4.5% 이하 매도 뒤 8일 매수 거부 |
| 심볼당 매도 1개 | `upbit_decisions.py:357-369` | `(sleeve, 심볼)`당 열린 매도 intent 하나(S2). 매수는 `max_open_intents`(`:500`) |
| 호가 단위 | `app/core/upbit_tick.py:31-63`, fillwire `internal/exec/upbit/tick.go:47-79` | 2025-07-31 표. Go는 ≥100 KRW 정수 가격만(`minPriceKRW=100`), 최소 주문 5,000 KRW(`MinOrderKRW`) |
| Upbit 녹화기 | `fillwire internal/upbit/reader.go:81,192-198`, `README.md:643` | 공개 websocket `orderbook`(15단계)+`trade`, **≤20 KRW 마켓**, `<dir>/upbit/<UTC 날짜>/<마켓>.jsonl`, 단절은 `gap` 레코드 |
| Upbit 그림자 실행기 | `fillwire internal/exec/upbit/plan.go:95,147`, `docs/upbit-shadow.md:7-50` | 테이커 가격(매수=최우선 매도호가), 재호가 ≤`max_requotes`, 만료 취소, 오래된 호가(2초) 금지. **체결원이 없다**(`Plan.Fill`은 테스트용) |
| 체결 모의 | `fillwire cmd/execsim/main.go:91`, `internal/exec/sim/sim.go:25-85` | `through`(보수)/`touch`(낙관) 규칙. 입력이 `exec.ParseIntent`(KIS E1 의도)다 |
| 추세 정의 | `app/services/brokers/binance/trend/strategy.py:72,92`, `constants.py:22-37` | `sign(close_t/close_{t−L}−1)`, L=60·hard stop 10%·time exit 30일·σ\*=60%·EWMA 20일·수수료 0.05%(`SEAL_*`) |
| 일봉 | `app/models/crypto_candles.py:63,91` | `crypto_candles_1d`: `quote_volume`(NULL 허용), `is_closed` |
| 종목 목록·경고 | `app/services/upbit_symbol_universe_service.py:50,533-540`, `deploy/dagu/at-batch2/at-batch2-upbit-symbol-universe.yaml:6` | `market_warning`(NONE/CAUTION) 저장, 매일 06:15 KST 동기화(등록됨) |
| 제외 규칙 | `research/crypto_universe/universe.py:39,88,130` | 스테이블·래핑 중복·레버리지 토큰 목록 |
| 전략 증거 | `docs/research/rob-1458-strategy-evidence-2026-11-02.md:24,40,43,46,53-55,84-91` | 아래 §2 |

### 1.2 이 문서가 새로 확인한 제약 [사실]

- **F1. `execsim`은 Upbit를 모른다.** 입력이 KIS용 `exec.ParseIntent`이고 가격은 KRX 정수 주식 단위다(`cmd/execsim/main.go:91`). Upbit 쪽은 `execupbit`가 체결원 없는 계획기일 뿐이다. 그래서 "녹화 호가로 체결을 모의"하는 Upbit 경로가 **없고**, 만들어야 한다(T5).
- **F2. `shadowed` ≠ 체결 모사.** 같은 저널 행이 지정가에 즉시 전량 체결로 읽힌다(`docs/design/5438-go-execution-path.md:250`). 대기하는 익절 주문도 즉시 체결로 보이고, 그 결과 shadow 보유 수량이 줄어 이후 손절 매도가 `inventory_constraint_exceeded`가 될 수 있다.
- **F3. shadow 매도도 실계좌 평단으로 가드된다.** `_loss_sell_decision`은 `reads.avg_buy_price`(실계좌)를 쓴다. 실계좌가 그 코인을 안 들고 있으면 `avg_price_unproven`, 들고 있어도 shadow 비용과 무관한 평단×1.01로 거부된다(`upbit_decisions.py:588-606`).
- **F4. 동시 사다리 불가.** `(sleeve, 심볼)`당 열린 매도 intent가 하나다. 익절 사다리는 **순차**여야 하고, 손절 매도는 열려 있는 익절 intent를 먼저 취소해야 한다.
- **F5. 쿨다운도 실계정 값이다.** shadow 매수가 실계정의 8일 쿨다운에 막힐 수 있다(`upbit_decisions.py:672`).
- **F6. 위임장 수명이 91일**(`mandates.py:62`)이라 진입 60일 + 청산 30일이 정확히 한 위임장에 들어간다(§8).
- **F7. 녹화기는 20마켓**(`reader.go:81`)이라 유니버스를 20으로 자르고 녹화 목록과 맞춰야 한다.
- **F8. 손절은 연속 감시가 아니다.** 5438 §6.1은 손절 점검을 6시간 슬롯(20:20·02:20·08:20)으로 둔다(`docs/design/5438-go-execution-path.md` 전략 잡 절). 실행 가능한 손절의 시간 해상도는 슬롯이다.
- **F9. sleeve 규칙 id는 닫힌 목록**이다(`app/services/strategy_sleeves/store.py:87`). 4885도 같은 이유로 코드 PR을 선행으로 둔다(`docs/design/4885-upbit-trend-long-live.md` §2.2-1).
- **F10. 스테이블 제외 목록에 `USDT`가 없다**(`research/crypto_universe/universe.py:39-76`). 그 목록은 USDT 호가 시장용이라 USDT가 기준 통화다. Upbit에는 `KRW-USDT`가 있으므로 이 전략은 목록에 `USDT`를 더해 써야 한다(T1).

### 1.3 증거와 한계

ROB-1458은 같은 계좌 이력의 **표본 안(in-sample) 한 창**(암호화폐 지수 −9.9%, 2026-05-01~10-31, `rob-1458-strategy-evidence-2026-11-02.md:24`)이다.

- 반복 소액 매수(물타기) 802건 D+20 중앙값 −2.1%·Q1 −8.8% → **FAIL**, 평단 −8% 이하 추가매수(기타) 8건 중앙값 +3.5% → 표본 작음(`:84-91`).
- 지정가 깊이: 깊은 지정가 체결률 25%(n=4), 시장 근처 66.7%(n=9)(`:43`).
- 익절: +1~8% 지정가 n=54 체결 70.4%·중앙 1일, +8% 이상 n=9 체결 44.4%·중앙 18일(`:46`).
- 기계식 절단: −8% 첫 도달 n=12에서 절단이 실제 보유보다 나았던 비율 66.7%(D+20 83.3%)(`:40`).
- 처음부터 짠 규칙 73신호(9종목): A2(근처 진입·−10% 절단·+30% 익절·60세션) 평균 +1.5%·중앙 −10.3%·승률 35.6%, B3(근처 진입·−10/−20 물타기·평단 +10% 청산·−35% 손절) 평균 +11.7%·승률 94.5%·**최악 −47.2%**(`:53-55`).

한계: 한 체제·한 창·종목 선택이 이 창의 것·시가 체결(슬리피지 없음). 이 증거는 "물타기는 이 창에서 위험 대비 설명이 안 되고, 시장 근처 진입이 더 잘 체결되고, +8% 아래 익절이 더 빨리 체결된다"까지만 지지한다. **어느 규칙이 앞으로 이긴다는 증거가 아니다.** 그래서 이 설계는 규칙을 고르지 않고 두 계열과 게이트를 앞으로의 데이터로 비교한다.

## 2. 설계 원칙

1. **규칙 권위 = 재생 원장.** 성과·체결·손익은 (의도 파일, 녹화 호가·체결, 일봉)에서 결정론으로 재생해 얻는다. 게이트웨이 행은 증거로만 쓴다(§7.2).
2. **자유 변수 없음.** 모든 상수는 이 문서에서 고정하고 이유를 적는다. 튜닝 예산 0(manifest `tuning_budget.max_configurations=0`). 봉인 뒤 바꾸면 새 에포크다.
3. **모르는 값은 0이 아니다.** 결측·오래됨·판독 불가는 닫힌 사유와 함께 `unknown`이고, 게이트 입력이 모르면 **진입을 막고** 그 일수를 센다.
4. **물타기는 점수가 아니라 실행 불가다.** 이미 보유한 마켓에 매수 의도를 만들지 않는다(§5.4). 5750 §3.2와 같은 구조다(`docs/design/5750-kr-daily-plan-v0.md` §3.2).
5. **비가격 데이터는 레짐 게이트다.** 종목을 고르는 데 쓰지 않는다. 입력이 레짐 한 줄(BTC·시장 전체)이고 종목별 값이 아니다(§6).

## 3. 유니버스와 스크리닝

### 3.1 단계 (순서 고정, 매일 09:10 KST 발행 슬롯 이전 입력으로 계산)

| 단계 | 규칙 | 근거 |
|---|---|---|
| U1 후보 | `upbit_symbol_universe`의 KRW 마켓, `is_active` | `upbit_symbol_universe_service.py:533-540` |
| U2 제외(정적) | `crypto_exclusion_reason` 중 **레버리지 토큰·스테이블 목록·래핑 중복**만. 30일 peg 규칙은 USD 종가용(0.98~1.02)이라 KRW 종가에 적용하지 않는다. 목록에 `USDT` 추가(F10) | `universe.py:39-76,88,130-170` |
| U3 경고·상장 나이 | `market_warning == "CAUTION"`이면 제외. 완결 일봉 ≥ 90개(`is_closed`)가 없으면 제외(상장일 필드가 없어 일봉 개수로 나이를 대체 — **[추정]**: 일봉이 상장일부터 쌓였다고 가정하지 않으므로 이는 "신뢰할 수 있는 이력 길이"이지 상장 나이가 아니다) | `:50,540`, `crypto_candles.py:91` |
| U4 가격·결측 | 마지막 완결 종가 ≥ 100 KRW(호가 단위 제약: Go 모델이 100 KRW 미만을 거부), 최근 61개 완결 종가 중 빈 날이 있으면 그 날은 `NO_SIGNAL`(앞 값 채우기 금지) | `tick.go:47` |
| U5 유동성 | 최근 30개 완결 일봉 `quote_volume`의 **중앙값** ≥ 5,000,000,000 KRW. `quote_volume`이 NULL이면 `liquidity_unknown`으로 제외 | `crypto_candles.py:63`. 5,000,000,000은 **[추정]**: 한 intent 상한 480,000이 그 0.01%라 시장충격이 아니라 "녹화 호가 모의가 호가 공백으로 무너지는 시장을 거른다"는 목적이다. T2가 녹화 호가 두께로 검증하고 봉인 전에만 바꾼다(Q4) |

### 3.2 에포크 유니버스를 얼려 둔다

- **U0** = 봉인일 S에 U1~U5를 통과한 마켓을 30일 거래대금(합) 내림차순으로 **상위 20개**(동률은 마켓 코드 오름차순). 20은 녹화기 상한(F7)이다.
- 에포크 동안 U0 구성원은 바뀌지 않는다. **일일 적격** = U0 ∩ U2~U5 재판정. 제외된 마켓도 녹화는 계속한다. 새로 뜨는 마켓은 이 에포크에 들어오지 않는다(수용한 한계: 91일 동안 유니버스 이동을 못 따라간다. 반대로 사후 선택 편향이 없다).
- **해시**: 일일 `candidate_set_sha256` = sha256(정규 JSON{`asof`, `rules_version`, `u0_sha256`, 마켓별 `{eligible, reason_code, turnover30_median_krw(decimal 문자열), last_close_krw}`}). `u0_sha256`은 봉인 때 manifest에 박는다. 파일은 한 번 쓰고 바꾸지 않는다(§7.3).
- 녹화기의 `markets_file`은 U0와 정확히 같아야 하고 시작 전 점검(T6)이 대조한다.

## 4. 두 신호 계열

공통 정의: `C_k` = 오늘 09:00 KST에 마감된 일봉부터 k번째 전 완결 일봉의 종가(`C_0` = 가장 최근 완결). 일봉 경계는 00:00 UTC = 09:00 KST다. 필요한 `C_0..C_60` 중 하나라도 없으면 그 마켓·그 날은 `NO_SIGNAL`. 수식은 Decimal로 한다.

- `R60 = C_0 / C_60 − 1`
- `RSI14` = `_calculate_rsi_series`(Wilder, `alpha=1/14`, `min_periods=14`, `adjust=False`)의 `C_0` 값(`app/mcp_server/tooling/market_data_indicators.py:459`, 단일 산식 ROB-1382)

### 4.1 (A) 추세 — `trend`

- **신호**: `R60 > 0`(엄격히 양). 롱 전용이라 `≤ 0`은 현금.
- **왜**: TREND-TSMOM-v1이 쓰는 정의 그대로의 롱 쪽이다(`strategy.py:72`, `constants.py:30` `SEAL_LOOKBACK_DAYS=60`). 새 탐색을 돌리지 않으므로 L은 고른 값이 아니라 **인용**이다. 같은 규칙이 Binance 선물 데모에서 봉인돼 돌고 있다(`research/experiments/registry.json` ROB-3796). 다른 점: 일봉·Upbit KRW 종가·롱 전용·유니버스 20·익절 사다리 포함(§5) — **데모의 복제가 아니다**. 데모 결과와 직접 비교하지 않는다.

### 4.2 (B) 눌림목 — `pullback`

- **신호**: `R60 > 0` **그리고** `RSI14 ≤ 40` **그리고** `C_0 > C_1`.
- **왜**: (A)와 같은 레짐 필터(`R60 > 0`) 안에서 **진입 시점만** 다르게 한다. 세 조건이 각각 하는 일: `R60>0`=상승 추세 안에서만, `RSI14≤40`=단기 눌림(상승 추세의 RSI 하단 통상값 — **[의견]**, 튜닝하지 않는다), `C_0>C_1`=떨어지는 중에 사지 않고 일봉 반등 확인 뒤 산다. 증거 연결: 시장 근처 진입이 더 잘 체결되고(§1.3), 같은 창에서 평단 아래로 키우는 규칙은 설명이 안 됐다 — 그래서 "더 싸게 사되 크기를 키우지 않는다". 규칙 상수 셋(60·40·1일)은 인용(60), 통념(40), 최소 확인(1일)이며 데이터로 맞추지 않았다.
- **비교의 의미**: (B) ⊂ (A)의 신호 집합이다. (A)가 가능한 날 (B)는 더 드물게 진입한다. 그래서 비교 지표는 일수·진입 횟수가 아니라 **진입당 순수익률과 예산 대비 손익**이다(§8).

### 4.3 공통 규칙

- 한 팔에서 한 마켓 한 포지션. 이미 보유 중이면 신호가 와도 `already_held`로 기록하고 의도를 만들지 않는다(물타기·증액 없음, §5.4).
- 팔당 동시 포지션 최대 4개(위임장 `max_open_intents` 4와 같은 값). 후보가 빈 자리보다 많으면 **유동성 순위**(U0 30일 거래대금 큰 순)로 채운다 — 알파 선택이 아닌 결정론적 동률 규칙이다.
- 청산 뒤 재진입: 신호가 다시 참이면 새 에피소드. 손절 또는 평단 −4.5% 이하 청산 뒤에는 그 마켓을 8일간 재진입 금지로 **재생이 스스로 모사**한다(실계정 쿨다운의 규칙을 따르되 값은 팔의 모의 이력에서 계산, F5).

## 5. 진입·청산·크기

### 5.1 시각 (09:00 KST 일봉 마감 기준, 모든 시각은 사전 고정)

| 슬롯 | KST | 일 |
|---|---|---|
| 결정·진입 발행 | **09:10** | 신호 계산 → 진입 의도(§5.2). 발행이 마감 후 60분을 넘기면 그 날은 `late_issue`로 진입 없음 |
| 손절·청산 점검 | 09:10 · 15:10 · 21:10 · 03:10 | 6시간 간격(F8). 슬롯의 최우선 매수호가를 본다 |
| 익절 의도 갱신 | 09:10 · 21:10 | 위임장 TTL 12시간에 맞춘 두 주기 |

발행 시각을 5438 §6.1의 14:20 실행 슬롯으로 하지 않는 이유: 신호 정보가 09:00에 확정되므로 5시간 20분의 가격 이동을 규칙의 성과에 섞지 않으려는 것이다. 실제 배포 슬롯은 5438 D7이 정한다 — Q2에 따로 묻는다.

### 5.2 진입

- 의도: `side=buy`, `limit_price_krw` = 발행 시점 최우선 매도호가(호가 단위 유지), `max_slippage_bps=30`(위임장 상한), `expires_at` = 발행 + 10분, `max_requotes=2`, 오래된 호가 한도 2초 — 모두 `execupbit`의 기본 동작(`docs/upbit-shadow.md:50-`)이다.
- **왜 시장 근처 지정가(테이커 가격)인가**: 증거에서 시장 근처 체결 66.7% vs 깊은 지정가 25%(§1.3)이고, 깊은 지정가의 미체결은 "신호는 맞았는데 못 산" 표본 손실을 만든다. 이 실험의 질문은 진입 *방식*이 아니라 *시점*이라 진입 방식은 하나로 고정한다. 깊은 지정가 비교는 별도 실험 대상이다(H-entry-price 계열).
- 체결이 안 되면(`unfilled`) 에피소드는 **결과 0으로 코호트에 남는다**(체결률은 따로 보고). 재시도 없음 — 다음 신호는 다음 날 09:10이다.
- 최소 주문 5,000 KRW 미만 자식은 만들지 않는다(`tick.go:50`).

### 5.3 크기

`N = 480,000 KRW × min(1, 0.60 / σ̂)`, σ̂ = 최근 20일 일수익률의 EWMA(영평균 2차 모멘트) 연율화(√365) — `realized_vol_annual`(`strategy.py:92`)과 같은 정의를 완결 일봉 종가에 적용한다(`sizing.py:32`와 같은 형태). `N < 50,000 KRW`이면 진입하지 않고 `skipped_below_floor`. 수량 = `floor8(N / limit_price)`. 480,000·0.60·50,000은 4885가 고른 값이고 이 문서는 바꾸지 않는다(위임장 상한이 480,000을 지배한다, `mandates.py:58`). 팔 예산은 4 × 480,000 = 1,920,000 KRW(운영 값은 Q5).

### 5.4 청산 — 두 계열이 같다

| 우선순위 | 조건 | 동작 |
|---|---|---|
| 1 | **하드 손절**: 평균 진입 체결가 대비 **−10%** | 점검 슬롯의 최우선 매수호가가 손절가 이하이면 남은 전량을 테이커 매도 |
| 2 | **추세 이탈**: 결정 슬롯에서 `R60 ≤ 0` | 남은 전량 테이커 매도 |
| 3 | **시간 손절**: 진입 체결 후 30 결정 슬롯 | 남은 전량 테이커 매도 |
| 병행 | **익절 사다리**(순차, F4) | 아래 |

- **손절 −10%인 이유**: ① 봉인된 TREND 데모(`SEAL_HARD_STOP_FRACTION=0.10`)·4885와 같은 값이라 새 자유 변수가 아니다. ② ROB-1458의 −8% 근거는 n=12·한 체제라 "절단이 유용했다"까지만 지지하고 8이라는 값은 못 고른다(§1.3). ③ 일봉 신호·6시간 슬롯에서 −8%는 일상 변동에 잘 닿고, 10% 절단 규칙 A2의 승률이 35.6%였다(`:53`) — 절단이 잦은 규칙이다. ④ 손절·−4.5% 이하 청산은 어느 값이든 8일 쿨다운을 건다(`order_execution.py:138`). 반대 의견(−8%)은 Q3.
- **익절 사다리**: 진입 완료 후 평균 진입가 `c`에 대해 세 단(`r = +2%, +4%, +6%`), 각 수량 1/3(8자리 내림, 마지막 단이 나머지). 가격 = `ceil_tick(c × (1 + r))`. 이유: 모두 증거의 "+8% 아래" 대역(체결률 70.4%·중앙 1일, §1.3)이고, 왕복 수수료 0.10%를 감안해 +1%가 아닌 +2%에서 시작하며, +8% 이상(체결률 44.4%·18일)은 피한다. **순차**: 동시에 열린 매도 intent가 하나뿐이라(F4) 가장 낮은 미체결 단 하나만 연다. 한 단은 TTL 12시간(09:10·21:10 주기)이고 같은 가격으로 최대 10주기(5일) 다시 연다. 단이 체결되면 15초 뒤(5438 R12 `PollEvery`) 다음 단을 연다. 5일이 지나면 사다리는 닫히고 남은 수량은 손절·추세 이탈·시간 손절에 맡긴다. 점검 슬롯에서 손절·추세 이탈이 걸리면 열린 익절 intent를 **먼저 취소**하고 청산 intent를 연다.
- **물타기 금지 = 실행 불가**: 매수 의도 생성기는 보유 마켓에 대해 의도를 만들 수 없다. 그 날의 신호는 `not_executable_reason=already_held`로 기록한다(점수 감점이 아니다). 평단 아래 추가매수를 켜는 설정·플래그는 만들지 않는다.
- **수수료·호가**: 수수료 0.05%/편도를 체결 금액에 적용(4885 §6 dots 조사 값, 계정 실제 적용률은 **[모름]**). 가격은 `upbit_krw_tick`(`upbit_tick.py:52`)과 Go `TickAt`(`tick.go:54`)이 공유 픽스처로 일치함이 이미 검사된다 — 이 문서가 호가표를 다시 정의하지 않는다. 100 KRW 미만 호가 구간은 U4에서 거른다.
- **라이브 가드와의 관계**: 손절·추세 이탈·시간 손절이 평단 아래이면 live에서는 `loss_sell_requires_card`(평단×1.01 미만)로 사람 카드(S5)가 된다(`docs/design/5438-go-execution-path.md` §2.1). 이 실험은 그 가드를 바꾸지 않는다. shadow에서는 그 거부를 **실행 가능성 증거**로 센다(§7.4).

## 6. 비가격 데이터: 레짐 게이트

게이트는 **새 진입만** 막는다(이미 연 포지션의 청산은 그대로). 게이트 팔의 규칙: 아래 G1~G4 중 **하나라도 막으면, 또는 하나라도 `unknown`/오래되면(>36시간)** 그 날 신규 진입 없음. 각 게이트의 개별 막음 여부는 매일 기록해 사후 서술만 한다(게이트별 팔을 따로 두지 않는다 — 검정력과 튜닝 예산 때문). 통제군 = 같은 계열의 게이트 끔 팔.

| # | 입력 | 레포 안 출처 | 관측 시각 | 막는 규칙 | 지금 상태 |
|---|---|---|---|---|---|
| G1 | 공포탐욕지수 | `get_fear_greed_index_impl`(`app/mcp_server/tooling/analysis_tool_handlers.py:1732`, alternative.me), 스냅샷 `crypto_insight_snapshots`(`app/services/crypto_insight_snapshots/builder.py:27`) | alternative.me 일 1회(UTC 00:00 전후 [모름]), `snapshot_at` | 값이 [25, 75] 밖(극단 공포·탐욕) | 수집 경로 있음. 일 갱신 스케줄이 **등록돼 있는지 [모름]**(플로는 import만, `app/flows/invest_crypto_insight_snapshots_flow.py:1-20`) |
| G2 | 스테이블코인 공급 7일 변화 | defillama provider(`:33`), 레짐 뷰(`app/mcp_server/tooling/fundamentals/_crypto_regime.py`) | `snapshot_at` | 7일 전 대비 `< 0` | 플로 기본 provider에만 포함. 이력은 수집이 돈 만큼만. 8일 예열 필요 |
| G3 | BTC 김치 프리미엄 | `_fetch_kimchi_premium`(`app/mcp_server/tooling/fundamentals_sources_naver.py:226`) — Upbit KRW·Binance USDT·환율 | 호출 시각(저장 안 함) | `≥ +3.0%` | **수집 안 함.** 호출 시점 계산뿐이고 이력 저장이 없다 → T3 |
| G4 | BTC 선물 funding | `_fetch_funding_rate_batch`(`app/mcp_server/tooling/fundamentals_sources_binance.py:44`), `builder.py:65`(BTCUSDT·ETHUSDT) | 마지막 3회 정산(24시간) | 최근 3회 평균 `≥ +0.03%`(8시간당) | 최신 값만 저장, 3회 이력은 저장 안 함 → T3 |
| — | Upbit 경고(`market_warning`) | `upbit_symbol_universe_service.py:50,540` | 일 동기화 06:15 KST | **게이트가 아니라 모든 팔의 하드 필터**(§3 U3) | `market_event` 상세(투자유의 세부)는 저장하지 않는다 **[모름 — API가 주는지 T1에서 확인]** |
| — | Upbit 공지(거래지원 종료 예고 등) | 없음 | — | — | **수집 안 함.** 별도 수집 티켓 T10. 그 전에는 위험으로만 기록 |
| — | 뉴스 | `get_news` crypto(Finnhub 일반, `docs/agent-contracts/mcp-and-analysis-contracts.md` ROB-510) | — | 사건 제외로만 | **봉인 팔에 넣지 않는다.** 코드가 기사를 자동 제외하지 않는다는 계약(ROB-491)이 있고 결정론적 출처가 없다. 세션이 에피소드에 `event_flag` 주석을 다는 것은 허용(비교에 안 씀) |

- **임계값은 증거가 없다**: 레포에는 G1~G4 임계의 근거 데이터가 없다. [25,75]는 지수 자체의 "극단" 구간, `<0`은 방향만, +3.0%·+0.03%는 **[의견]**(국내 과열·레버리지 과밀의 통념 구간)이다. 그래서 게이트는 "효과가 있다"가 아니라 "이 사전등록 규칙이 게이트 끔 팔보다 나은가"만 묻는다(§8 C2). 값을 사후에 바꾸지 않는다(Q6).
- **예열**: G2는 8일치 일 스냅샷이 필요하다. G3·G4가 수집되기 시작한 날부터 8일 뒤에 시작한다(T3 선행).
- **관측 시각**: 게이트 입력은 `observed_at ≤ 발행 시각(09:10)`인 값만 쓴다. 입력 묶음은 매일 한 번 파일로 얼려 해시한다(§7.3).

## 7. Shadow 기계

### 7.1 흐름

```
09:00 KST 일봉 마감
09:10  [T4 일일 잡]  입력 동결 → 4팔 신호·크기 → 의도 4팔분
        ├─ 게이트웨이: shadow 위임장·shadow sleeve(팔마다 1개)로 의도 저장 + 자식 submit
        │     → shadowed / refused(사유 코드)            ← 경로 증거 (DB)
        ├─ 결정 로그: decision_evidence(종목×팔마다 한 행, no-op 포함)
        └─ 의도 파일 내보내기(JSONL, 불변 일 파일)       ← 재생 입력
슬롯마다 같은 잡이 손절·추세 이탈·익절 단 갱신 의도 발행
녹화기(pi): 마켓 20개 orderbook(15단계)+trade → <dir>/upbit/<UTC 날짜>/<마켓>.jsonl
[연구 호스트] T5 재생기 + T7 하네스: (의도 파일, 녹화, 일봉) → 에피소드 원장 → 팔 비교
```

### 7.2 재생 원장이 권위인 이유와 체결 규칙 (규칙을 먼저 쓴다)

재생 원장은 모든 자식(진입·재호가·익절 단·청산)을 **녹화된 호가에 대해** 아래 규칙으로 체결한다. 이 규칙은 봉인 때 해시로 고정하고 이후 바꾸지 않는다.

1. **지연**: 자식의 가격을 정한 호가는 발행 시각 `τ`의 동기화된 호가(나이 ≤ 2초). 체결은 **`τ + 1.0초` 이후 첫 호가 프레임**에 대해서만 일어난다(주문 왕복 지연을 1초로 둔 **[추정]** — 실측 없음, 봉인 전에 바꿀 수 있는 값이 아니라 고정값으로 둔다).
2. **테이커 부분**: 제출 시점 호가에서 지정가 이내로 도달 가능한 반대편 단계의 **표시 수량만큼** 각 단계 가격으로 체결(가중평균). 15단계 깊이를 넘는 부분은 지정가 가격에 대기하는 잔량이 된다.
3. **대기 잔량**(익절 단·테이커 잔량): 보수 규칙 `through` = 주문 가격을 **엄격히 통과한** 체결(`trade` 프레임)만이, 그 체결 수량만큼 채운다(`internal/exec/sim/sim.go:25`). 낙관 규칙 `touch` = 주문 가격의 체결 또는 반대편 호가가 가격에 닿으면 남은 수량 전부(`sim.go:28`). 체결가는 항상 주문 자신의 지정가다. 큐 순서는 모사하지 않는다.
4. **만료**: `expires_at`에 도달하면 남은 수량을 취소한다. 만료 시각 이후의 프레임은 채우지 않는다.
5. **녹화 단절**: `gap` 레코드와 겹치는 구간에서는 새 자식도 대기 체결도 인정하지 않는다. 포지션이 있는 동안 연속 단절 > 60초이면 그 에피소드는 `gap_exposed`로 표시한다.
6. **보고**: 모든 결과는 `through`(**기본·판정에 쓰는 값**)와 `touch`(상한)를 함께 낸다. 판정이 두 규칙 사이에서 뒤집히면 `BRACKET_SENSITIVE`이고 `SUPPORTED`가 될 수 없다.
7. **손절의 시간 해상도(F8)**: 기본은 **슬롯 기반** — 점검 슬롯의 최우선 매수호가가 손절가 이하일 때만 발동하고 매도는 그 슬롯 시각 + 1초 이후 프레임에서 체결. 보조로 **연속 감시** 값을 함께 낸다(녹화된 어느 프레임이든 매수호가 ≤ 손절가이면 발동). 두 값의 차이가 "감시자가 있다면"의 가치이고 Go 실행기 손절 감시 티켓의 입력이다. 판정은 슬롯 기반 값만 쓴다.

### 7.3 저장

| 무엇 | 어디 | 형태 |
|---|---|---|
| 의도·자식·거부 사유 | 게이트웨이 DB(`review.execution_intents`, `execution_child_requests`) | 기존 테이블. 이 문서가 새로 쓰는 것 없음 |
| 결정 로그(신호값·σ̂·크기·동작/no-op 사유·의도 id) | `review.decision_evidence`(5438 §6.4) | 팔×마켓마다 한 행 |
| 일 입력(U0 해시·일일 적격·게이트 입력·신호·후보 해시) | 레포 밖 아티팩트 디렉터리 `upbit-shadow/<epoch>/inputs/date=<UTC 날짜>.json` | 한 번 쓰고 불변, sha256 manifest 한 줄 추가 |
| 의도 파일 | `.../intents/date=<UTC 날짜>.jsonl` | 불변 |
| 녹화 | pi의 fillwire 기록 디렉터리 | 일 단위 gzip 마감 |
| 재생 결과 | `.../replay/date=...jsonl`(자식마다 `through`·`touch`) | 재실행 가능(입력·코드 sha가 같으면 바이트 동일) |
| 에피소드 원장·비교 | `.../ledger/` | 일 단위 불변 파일 + 해시 연쇄(ROB-1473 `research/paper_forward/store.py` 방식) |

데이터는 비공개이므로 레포 밖이다(ROB-1439와 같은 이유). 일 파일이 쓰이지 못한 날은 `unverified`이고 0으로 채우지 않는다.

### 7.4 게이트웨이 경로 증거 (판정에 쓰지 않는다)

매일 `shadowed`/`refused` 건수와 거부 사유를 팔별로 센다. 재생 원장이 만든 자식 수와 게이트웨이 자식 수를 대조한다. **알려진 어긋남**(F2·F3·F4·F5)은 사유를 붙여 `path_divergence`로 센다: `loss_sell_requires_card`·`avg_price_unproven`(실계좌 평단), `cooldown_active`(실계정 쿨다운), `inventory_constraint_exceeded`(shadow 장부의 즉시 체결 가정), `open_sell_intent_exists`. 이것은 **live에서 이 규칙이 실행 가능한지**의 증거다 — 특히 손절·추세 이탈·시간 손절의 거의 전부가 live에서는 사람 카드가 된다는 것을 숫자로 보인다. 설명되지 않은 어긋남은 중단 조건 S5다.

### 7.5 주문은 없다

- 위임장은 **shadow 모드 4개**만(운영자 CLI `scripts/execution_mandates.py`, preview 기본). live 위임장·live sleeve 전환·`EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`는 건드리지 않는다. shadow forwarder는 브로커·주문 코드를 import하지 않고(`upbit_shadow_forwarder.py:1-12`) `execution_mode`는 바뀌지 않는다(`execution_intent.py:248-256`).
- 위임장 한도는 D9 상수 그대로이고(`mandates.py:58-62`) 넓히지 않는다. 의도 TTL 12시간 상한이 익절 단 주기(12시간)를 정한다.
- 이 문서는 위임장·sleeve·DAG를 만들지 않고 스케줄을 등록하지 않는다(하드룰 4·6). 일일 잡의 Dagu 사본은 T4가 만들되 등록은 운영자 승인이다.

## 8. 판정·중단

### 8.1 팔과 비교 (모두 사전 고정, 비교는 서술 통계이며 유의성 검정이 아니다)

| 팔 | 계열 | 게이트 |
|---|---|---|
| A0 | 추세 | 끔 |
| A1 | 추세 | 켬 |
| B0 | 눌림목 | 끔 |
| B1 | 눌림목 | 켬 |

통제군(연구에서 계산, 레포 안 기록 없음): `cash`(0), `btc-hold`(같은 예산을 첫 슬롯에 BTC 매수·창 끝까지 보유, 편도 수수료 1회).

- **지표**: 에피소드 하나 = 한 팔·한 마켓의 한 진입 신호 → 청산(또는 미체결 0). `D_i` = 비용 차감 순손익 ÷ **의도한 진입 금액**(미체결은 0, 코호트에 남음). 예산 대비 합계 손익은 보조로 같이 낸다.
- **비교**: C1 계열 `B0 − A0`; C2 게이트 `A1 − A0`·`B1 − B0`; C3 절대 각 팔 vs 현금. 각 값 = `D_i` 평균의 차(%p).
- **효과 문턱 `delta_min` = +1.0%p**(왕복 비용 0.10%의 10배, 손절 −10%의 1/10 — **[의견]**, Q7). C3는 팔의 `D_i` 평균 ≥ +1.0%이고 예산 대비 누적 순손익 > 0이다.
- **표본 문턱**: 비교에 드는 두 팔 모두 **완결 에피소드 ≥ 15**. 미달이면 그 비교는 `INSUFFICIENT_SAMPLE`(통과도 실패도 아님, 연장 없음, 새 에포크).
- **판정 코드**(비교마다): `SUPPORTED`(표본 충족·`delta_min` 이상·`through`와 `touch` 둘 다 같은 부호) · `NOT_SUPPORTED`(표본 충족·`delta_min` 미만) · `BRACKET_SENSITIVE` · `INSUFFICIENT_SAMPLE` · `UNVERIFIED`(진입 창 일수 또는 에피소드 일수 중 `unverified`가 10% 초과: 녹화 공백, 일봉 결측, 게이트 입력 결손 등).
- **보조**: 체결률·진입 지연·슬리피지(발행 호가 대비 체결가)·손절 슬롯 vs 연속 감시 차이·게이트별 막음 일수(서술만)·팔별 최악 에피소드·`already_held` 수·`path_divergence` 수.
- **체제 주의**: 한 창·한 체제다. 결과는 그 체제 안의 서술이고 일반화하지 않는다. `btc-hold` 대비도 함께 읽는다.

### 8.2 기간

- **진입 창 60일**(60번의 09:10 발행). 이후 마지막 진입의 시간 손절(30 슬롯)까지 청산·사다리를 계속 관리해 **총 91일**에 끝낸다 — 위임장 수명 91일(F6)에 정확히 맞다. 91일에 열린 포지션이 남아 있으면 그 에피소드는 `unmeasured`(마크하지 않고 0으로 채우지 않는다).
- 최종 열람은 **한 번**(ROB-1429 `test_looks`, `research/experiments/README.md`). 중간에는 안전·완결성(S1~S6)만 보고 비교 지표를 읽거나 규칙을 바꾸지 않는다. 표본 문턱은 검정력이 아니다.
- 기대 표본 **[추정]**: 팔당 동시 4포지션·평균 보유 10일이면 60일에 약 24 에피소드. 진입이 더 드문 (B)는 15에 못 닿을 수 있다 — 그 경우가 `INSUFFICIENT_SAMPLE`이고 그것 자체가 (B)의 정보다.

### 8.3 중단 (하나라도 해당하면 멈추고 원인 보고)

- **S1** 실주문·live 위임장·live sleeve·제안·감시 등록이 이 경로에서 생김(0건 허용). 즉시 중단.
- **S2** ROB-501 위반(`app/**`에 in-process LLM provider). 정적 가드 `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`.
- **S3** 팔 누적 순손실 ≥ 1,000,000 KRW(4885 sleeve 손실 한도 모사, 값은 Q5) → 그 팔의 신규 진입 중단, 열린 포지션은 규칙대로 청산. 재개는 새 에포크.
- **S4** 녹화 커버리지: 결정일 중 필요한 마켓의 동기화 호가가 없는 일수 > 10% → `UNVERIFIED`로 종결(규칙 변경 금지, 녹화 복구 뒤 새 에포크).
- **S5** 게이트웨이 어긋남 중 설명 못 하는 것이 5%를 넘음(§7.4) → 결과를 내기 전에 원인 수리.
- **S6** 규칙·유니버스·상수·체결 규칙·비용표·코드 sha 변경 → 새 manifest 버전·새 에포크. 이 실험 안에서 바꾸지 않는다.
- **S7** 시작 전 점검(T6) 실패: 녹화 마켓 ≠ U0, 게이트 입력 예열 미달, 결정 시각 일봉이 반복적으로 늦음 → 시작하지 않는다.

통과해도 live 승격은 자동이 아니다. 별도 설계(실계좌 위임장·sleeve live·사람 카드 경로 설계)와 별도 운영자 승인이 필요하다.

## 9. 구현 티켓 (각각 PR 하나, blocking 간선)

| # | 티켓 | 내용 | 선행 |
|---|---|---|---|
| T1 | 순수 규칙 라이브러리 | `app/services/upbit_krw_strategy/`: U1~U5·`crypto_exclusion_reason` 래핑(+`USDT`)·`R60`·`RSI14`·두 신호·크기·청산 규칙·익절 사다리 가격·일일 적격·`candidate_set_sha256`. 순수 함수, DB·브로커·네트워크 import 없음(정적 테스트), 골든 픽스처. `market_event` 상세 제공 여부 확인 | 없음 |
| T2 | 유니버스 검증 | 최근 `crypto_candles_1d`·녹화 호가로 U5 하한과 `quote_volume` NULL 비율을 측정해 하한을 확정, U0 후보 목록 산출 스크립트(읽기 전용). 값 변경은 봉인 전에만 | T1 |
| T3 | 게이트 입력 수집 | `crypto_insight_snapshots`에 김치 프리미엄(BTC)·funding 최근 3회 provider 추가(기존 서비스만 쓰기), G1·G2 수집 경로 점검, Dagu 사본(미등록) | 없음 |
| T4 | 일일 잡 | 입력 동결 → 4팔 신호 → shadow 의도(게이트웨이)·결정 로그·의도 파일 내보내기. CLI, 스케줄 없음. 슬롯 잡(손절·익절 단 갱신)도 같은 CLI | T1, T3(게이트 팔), T9a |
| T5 | fillwire Upbit 체결 재생 | `execsim`에 Upbit 모드: `internal/exec/upbit` 계획기 + 녹화 `orderbook`/`trade` 재생, §7.2 규칙 1~7, `through`/`touch`, 골든 | 없음(fillwire 저장소) |
| T6 | 시작 전 점검 | 녹화 마켓 = U0 대조, 커버리지 리포트(마켓·일·`gap`), 게이트 예열 확인, 일봉 지연 점검. 읽기 전용 | T2, T3, T5 |
| T7 | 연구 하네스 | `research/upbit_shadow/`: 에피소드 원장·4팔·통제군·비교·판정 코드·`unverified` 규칙. 연구 호스트에서 실행, pi 아님. DB·브로커 없음 | T4, T5 |
| T8 | 경로 증거 보고 | 게이트웨이 거부 사유·재생 자식 수 대조(`path_divergence` 분류) 읽기 전용 스크립트 | T4 |
| T9a | sleeve 규칙 id + 운영 절차 | `CANARY_RULES`에 두 규칙 id 추가(4885 Q7과 같은 PR이면 한 번만), 4개 shadow sleeve·shadow 위임장 운영자 절차 런북. 위임장·sleeve 행 생성은 운영자 | 없음 |
| T9 | 봉인 | manifest 봉인(해시: 규칙·U0·체결 규칙·비용·코드 sha), registry 전이, heartbeat epoch | T1~T8 |
| T10 | (선택) Upbit 공지 수집 | 거래지원 종료 예고 등 공지의 공개 수집·저장. 별도 운영자 결정 후 | Q9 |

의존 간선: T1→{T2, T4}; T3→{T4, T6}; T5→{T6, T7}; T2→T6; {T4}→{T7, T8}; T9a→T4; {T1..T8}→T9. T10은 독립이다.

## 10. 열린 질문 (권고 답)

1. **팔 수**: 4팔(계열×게이트) vs 2팔(게이트 팔 제외). 권고: 4팔. 게이트 입력이 아직 없는 팔은 T3 완료 전까지 시작하지 않으므로 4팔 모두 준비된 뒤 시작.
2. **발행 슬롯**: 09:10 KST(권고) vs 5438의 14:20 실행 슬롯. 권고: 09:10 — 신호의 정보 시각과 발행을 붙여 둔다. 배포 슬롯은 5438 D7과 별개 결정.
3. **하드 손절**: −10%(권고) vs −8%. 권고: −10%(§5.4 이유). −8%를 원하면 새 manifest 버전.
4. **U5 유동성 하한**: 중앙 30일 거래대금 5,000,000,000 KRW(권고, T2가 검증). 대안: 상위 20 안에서는 하한 없음.
5. **shadow sleeve 예산·손실 한도**: 권고: 4885와 같은 값(팔당 예산 1,950,000 KRW, 손실 한도 1,000,000 KRW 누적). 운영자 값이다. sleeve 사전등록 4필드(`prereg_metric`·`prereg_min_effect_pct`·`prereg_horizon_sessions`·`prereg_max_duration_days`)는 이 문서의 8.1·8.2 값을 쓴다.
6. **게이트 임계(G1~G4)**: 권고: §6 값 그대로, 증거가 없음을 manifest 질문에 명시하고 사후에 바꾸지 않는다. 대안: 김치·funding 게이트를 이력이 쌓일 때까지 제외하고 G1·G2만으로 시작.
7. **`delta_min` +1.0%p와 표본 문턱 15**: 권고: 그대로. 4885의 15·5750의 0.25%p와 같은 기준으로 읽기 어려운 점은 감수(수익률 정의가 다르다).
8. **shadow 손실매도 가드·쿨다운의 실계좌 의존(F3·F5)**: 권고: **가드를 바꾸지 않고** 어긋남을 증거로 센다(§7.4). shadow 전용으로 shadow 평단을 읽게 하는 변경은 손실매도 가드 코드를 건드리는 일이라 하드룰 4에 따라 운영자 결정이 필요하다 — 이 문서는 제안하지 않는다.
9. **Upbit 공지·`market_event` 수집**(T10): 권고: 이 실험 시작 전에는 하지 않고 위험으로 기록한다. 거래지원 종료 예고가 급락 원인이므로 live 설계 전에는 필요하다.
10. **체결 지연 1.0초와 시계**: 권고: 고정. 실측(실제 주문 왕복)은 live 설계의 입력이 아니고 이 shadow에서는 얻을 수 없다.
11. **일봉 지연**: 09:10에 `crypto_candles_1d`가 마감 일봉을 갖고 있는가 **[모름]**(쓰는 잡이 이 문서에서 확인되지 않았다). 권고: T6가 측정하고, 없으면 `NO_SIGNAL`이 아니라 `unverified`로 센다.
12. **ROB-1473(암호화폐 B3 일 shadow)와의 관계**: B3는 물타기를 포함하고 일봉 시가 체결이다. 이 실험이 B3를 대체하지 않고 B3 결과와도 비교하지 않는다. 권고: 두 실험을 병행.

## 11. 근거 핀

### `auto_trader@9677aab2d`

| 항목 | 위치 |
|---|---|
| Upbit 의도 v2·자식 | `app/schemas/execution_intent.py:51,248-308` |
| 위임장 D9 상수 | `app/services/execution_intent_gateway/mandates.py:58-62` |
| shadow forwarder | `app/services/execution_intent_gateway/upbit_shadow_forwarder.py:1-34` |
| shadow 장부 | `app/services/execution_intent_gateway/upbit_store.py:351-370` |
| 가드·S2·쿨다운·최대 열린 intent | `app/services/execution_intent_gateway/upbit_decisions.py:65,357-369,500,588-606,608-686` |
| 평단·쿨다운 읽기 | `app/services/execution_intent_gateway/upbit_account_reads.py:19-41` |
| 8일 쿨다운·−4.5% | `app/mcp_server/tooling/order_execution.py:138` |
| 호가 단위 | `app/core/upbit_tick.py:31-63` |
| 추세 정의·크기 | `app/services/brokers/binance/trend/strategy.py:72,92`, `sizing.py:32`, `constants.py:22-37` |
| 일봉 | `app/models/crypto_candles.py:63,91` |
| 종목·경고 | `app/services/upbit_symbol_universe_service.py:50,533-540`, `deploy/dagu/at-batch2/at-batch2-upbit-symbol-universe.yaml:6` |
| 제외 규칙 | `research/crypto_universe/universe.py:39-76,88,90-92,130-170` |
| RSI 단일 산식 | `app/mcp_server/tooling/market_data_indicators.py:459` |
| 게이트 입력 | `app/mcp_server/tooling/analysis_tool_handlers.py:1732`, `app/mcp_server/tooling/fundamentals_sources_naver.py:226`, `app/mcp_server/tooling/fundamentals_sources_binance.py:19,44`, `app/services/crypto_insight_snapshots/builder.py:27,65`, `app/flows/invest_crypto_insight_snapshots_flow.py:33`, `app/mcp_server/tooling/fundamentals/_crypto_regime.py:27-37`, `app/services/retro_v1/data_gaps.py:69-93` |
| sleeve 규칙 id | `app/services/strategy_sleeves/store.py:87` |
| 같은 구조의 물타기 금지 | `docs/design/5750-kr-daily-plan-v0.md` §3.2 |
| 경로 설계 | `docs/design/5438-go-execution-path.md` §2.1(`:110`)·§3.6(`:250`)·§6.1·§9.1(`:426`) |
| 실전 소액 설계 | `docs/design/4885-upbit-trend-long-live.md` §3.3(`:81`)·§3.5(`:100`)·§7(`:173`) |
| 증거 | `docs/research/rob-1458-strategy-evidence-2026-11-02.md:24,37,40,43,46,53-55,84-91` |
| 실험 레지스트리 규칙 | `research/experiments/README.md`, `scripts/ci/experiment_registry_lint.py` |

### `fillwire@a5a95c1`

| 항목 | 위치 |
|---|---|
| Upbit 녹화기 | `internal/upbit/reader.go:81,192-198`, `README.md:643` |
| 그림자 실행기·계획기 | `internal/exec/upbit/plan.go:14,95,147`, `internal/exec/upbit/intent.go:78,132`, `docs/upbit-shadow.md:7,50` |
| 호가 단위·최소 주문 | `internal/exec/upbit/tick.go:47-79` |
| 체결 모의(KIS 전용) | `cmd/execsim/main.go:64,91`, `internal/exec/sim/sim.go:25-85` |

## 12. 알았지만 건드리지 않은 것

- 5438 §3.6의 "shadowed = 즉시 전량 체결" 가정과 shadow 매도가 실계좌 평단을 읽는 동작(F2·F3): 코드·계약 변경이라 이 문서의 범위 밖. 영향만 기록했다.
- `docs/agent-contracts/market-data-and-screener-contracts.md`의 "`research.crypto_universe.exclusions` 단일 정의" 서술: 레포에는 그 이름의 모듈이 없고 판정은 `research/crypto_universe/universe.py:130`의 `crypto_exclusion_reason`이다. 문서 정정은 이 PR의 범위 밖.
- 4885(실전 소액·카드 경로)와 이 실험의 관계: 서로 읽지 않는다. 4885의 manifest·설계는 바꾸지 않았다. 5438 §6.1의 결정 슬롯(14:20)과 이 문서의 09:10이 다르다(Q2).
- `crypto_candles_1d`를 매일 채우는 잡과 그 지연 시각, Upbit `market_event` 상세 필드의 제공 여부, 계정 실제 수수료율: 확인하지 못했다.
- fillwire `execupbit`의 intent 전송 배선(`docs/upbit-shadow.md:3-5`)과 게이트웨이 폴링(5438 R12): 이 실험은 재생 입력을 의도 파일로 받으므로 거기에 의존하지 않는다.
