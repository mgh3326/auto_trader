# #4885 — Upbit 소액 실전 롱 전용 추세추종 실험: 설계와 사전등록

hk 4885가 요구한 설계 문서다. 상위 결정은 `hk:doc decision/2026-11-16/at-strategy-direction` §1이다.
이 문서의 모든 `path` 인용은 `origin/main` `d77ac9111` 기준이다. 이 문서는 설계 문서이며 코드·마이그레이션·정책 YAML·sleeve 행·스케줄러·env는 바뀌지 않는다.

표기: **[사실]** = 그 SHA의 코드·문서·hk doc에서 확인한 것, **[추정]** = 확인하지 않은 추론, **[의견]** = 이 문서의 권고. 확정되지 않은 값은 전부 §11의 번호 매긴 열린 결정(Q)이고, 본문에서는 "권고값"으로만 쓴다.

운영자 결정(원문은 hk doc):

- `decision/2026-11-16/at-strategy-direction` §1: Upbit 실전 소액 실험을 지금 연다(11-15 결정 Q6 "데모 판정 뒤 별도 설계"를 대체). 전략은 TREND-TSMOM 규칙의 롱 전용판, BTC·ETH·SOL·XRP, 신호가 숏/무신호면 현금. sleeve(ROB-1470)로 예산 1,950,000 KRW, 월 손실 한도 1,000,000 KRW. 주문은 건별 승인 카드(위임장 3795 없이). 자동 집행은 3795 이후 별도 승인. 설계 문서·사전등록이 먼저이고, 실주문 경로 변경·배포는 내용 기준 승인.
- 같은 결정의 "주지 않는 것": 실주문 경로 변경·운영 마이그레이션·스케줄러 등록·env 변경의 건별 승인, 자동승인 범위 확대.
- `review/2026-11-16/dots-upbit-live-conditions`(dots 조사, 공개 문서 읽기만, 2026-11-16 확인): 수수료·최소 주문·호가·요청 한도·identifier·입출금 조건과 "공개 API에 stop 주문이 없다"는 결론. 아래 §6·§5의 Upbit 사실은 전부 이 조사에서 왔고, 이 문서가 다시 검증한 것이 아니다.
- 데모판: `docs/design/3796-crypto-trend-demo.md`와 `docs/agent-contracts/broker-and-ledger-contracts.md`의 "TREND-TSMOM-v1" 절. 데모 manifest `research/experiments/manifests/rob-3796.json`(sealed, L=60, hard stop 10%, 봉인 구간 2026-11-15~2027-01-09).

## 1. 목적과 범위

### 1.1 목적

구현·운영 티켓이 다시 결정하지 않고 따라갈 수 있게 다음을 정한다.

1. 규칙: 신호, L, sleeve 안의 크기, 네 종목.
2. 신호가 기존 제안 경로의 카드가 되는 방식.
3. API stop이 없을 때 하방을 무엇이 지키고 무엇이 못 지키는지(§5).
4. 비용, 측정·판정 방법, 중단 조건.
5. 이 문서가 승인하지 않는 것의 목록(§9).
6. 사전등록 항목(manifest draft + registry 항목, k 불변).

### 1.2 범위 밖

`app/`·`scripts/` 코드, TREND 패키지 변경, sleeve 행 생성, 위임장(3795), Binance 데모 변경, env·정책·스케줄러 변경. 위임장 설계 문서(`docs/design/3795-mandate.md`, hk 4881)는 참조만 하고 고치지 않는다 — 이 실험은 위임장 없이 돈다.

### 1.3 지키는 하드룰

| 규칙 | 이 설계의 처리 |
|---|---|
| 하드룰 4 — 게이트 완화 금지 | 이 문서는 어떤 게이트도 열거나 상수를 바꾸지 않는다. 자동승인 없음, 손실매도 가드·loss_cut 경로·쿨다운은 있는 그대로 따른다(§5). |
| 하드룰 6 — 스케줄러 금지 | 신호 평가·손절 점검·카드 생성은 전부 운영자가 시작한 세션의 수동 작업이다. 스케줄 등록 없음. 이것이 §5의 "점검 주기" 한계의 원인이다. |
| 하드룰 9 — 검증 규율 | 이 PR은 문서·manifest·registry뿐이다. 검증은 registry lint다(§10). |
| 하드룰 19 — 기계식 손절(ROB-1460) | 스위치는 기본 off 그대로이고 이 실험은 거기에 기대지 않는다(§5.3). |
| 실험 레지스트리(ROB-1429) | 테스트 구간을 보기 전에 manifest를 등록한다. 이 PR은 draft이고 `k`는 0이다. 봉인 뒤 규칙 변경은 새 version이다. |

### 1.4 용어

- **결정 시각**: 업비트 일봉이 마감되는 09:00 KST(= 00:00 UTC). 신호는 이 시각에 마감된 일봉으로만 계산한다.
- **에피소드**: 한 종목의 진입 신호 → 체결 → 청산까지. ROB-1470 canary 통계의 표본 단위와 같다(`docs/runbooks/strategy-sleeves.md`).
- **leg**: 한 종목의 포지션 하나.

## 2. 현재 상태

### 2.1 재사용할 재료 [사실]

| 재료 | 위치 | 사실 |
|---|---|---|
| TREND 상수(데모) | `app/services/brokers/binance/trend/constants.py:21-37` | `SIGMA_STAR=0.60`, `EWMA_SPAN_DAYS=20`, `ANNUALISATION_DAYS=365`, `SEAL_LOOKBACK_DAYS=60`, `SEAL_HARD_STOP_FRACTION=0.10`, `TIME_EXIT_DAYS=30`, `TAKER_FEE_RATE=0.0005`. 이 문서는 값을 인용만 하고 바꾸지 않는다. |
| 데모 탐색 보고서 | `research/experiments/reports/rob-3796-exploration.md` | Binance USD-M 4h 기준 3년 탐색에서 L=60 선택. 후보 손익의 대부분은 롱 쪽(롱 422.5, 숏 +1.07 USD). in-sample이고 생존편향 가능성이 있다고 보고서가 명시한다. |
| sleeve | `app/models/strategy_sleeves.py`, `docs/runbooks/strategy-sleeves.md` | `("upbit","crypto")` 지원, 통화 KRW. 버전 append-only. live 전환에 `prereg_metric`·`prereg_min_effect_pct`·`prereg_horizon_sessions`·`prereg_max_duration_days` 넷 다 필요(CHECK `live_requires_prereg`), live였던 적이 있으면 사전등록 변경 불가. |
| 제안 경로 | `app/mcp_server/tooling/order_proposal_tools.py:728`, `app/services/order_proposals/service.py:358-366` | `order_proposal_create(..., sleeve_id=...)`. `("upbit","crypto")`는 place 제출 가능 조합. Upbit 승인 창은 24/7(`approval_window.py:1057`). |
| 손실매도 가드 | `app/mcp_server/tooling/order_validation.py:191-200` | 일반 매도는 `avg_buy_price × 1.01` 미만 가격을 거부한다. |
| loss_cut 경로 | `order_validation.py:500-620`, `docs/runbooks/order-proposals.md` | 제안 경로에서만, limit 매도만, 회고(retrospective)·`approval_issue_id`·허용 caller 필요, 2클릭(90초 nonce), 슬립 밴드. Upbit crypto 지원. |
| 손절 쿨다운 | `app/mcp_server/tooling/order_execution.py:137, 835-846, 2115-2121` | 크립토 매도가 `avg × (1−0.045)` 이하이면 8일 재진입 금지(Redis, 심볼 단위). 매수 시 `is_in_cooldown`이면 주문 거부. |
| 실험 레지스트리 | `research/experiments/README.md`, `scripts/ci/experiment_registry_lint.py` | draft manifest는 일부 섹션·값을 `{"pending": ...}`로 둘 수 있다. |

### 2.2 이 문서가 새로 발견한 제약 [사실]

1. **sleeve `rule_id`는 닫힌 목록이다.** `store.create_sleeve`는 `CANARY_RULES`에 없는 `rule_id`를 거부한다(`app/services/strategy_sleeves/store.py:304-306`). 현재 6개는 전부 ROB-1458 canary이고 추세추종 규칙이 없다. 어휘 파일이 "바뀐 metric·horizon은 새 규칙 id를 추가하라"고 적는다(`vocabulary.py:12-13`). 따라서 sleeve를 만들려면 규칙 id를 추가하는 작은 코드 PR이 먼저 필요하다(Q7). 이 PR의 범위 밖이다.
2. **sleeve 손실 한도는 누적이고, 월 단위가 아니다.** `SleeveBook.loss`는 sleeve가 귀속한 모든 체결의 실현+미실현 순손익(수수료 차감)에서 나온다(`book.py:114-118`). 월 경계 리셋이 없다(Q6).
3. **sleeve 게이트는 사람 승인을 막지 못한다.** 게이트는 판정이 `allowed`가 아니면 제안을 "사람 승인 카드로 강등"할 뿐 거부하지 않는다(`gate.py:1-8`). 손실 한도 도달 시 sleeve를 `stopped`로 옮기지만(`dispatch_gate.py:162-172`) 카드는 "평범한 승인 카드: 손으로 여전히 승인할 수 있다"(`strategy-sleeves.md`). 이 실험은 모든 주문이 이미 카드이므로 게이트는 사유 코드를 붙이고 상태를 바꿀 뿐 추가 차단을 하지 않는다.
4. **계정 단위 평단·쿨다운.** 손실매도 가드의 `avg_buy_price`와 8일 쿨다운은 Upbit 계정의 해당 코인 전체 기준이지 sleeve 기준이 아니다. 같은 계정에 이 네 코인의 기존 보유가 있으면 sleeve 밖 매수가 평단과 쿨다운을 같이 움직인다(§11 Q12).
5. **brief의 `account_mode` 표기.** 작업 지시는 `upbit_live`라 썼지만 코드의 값은 `upbit`다(`service.py:358-366`, `strategy_sleeves.py:SLEEVE_ACCOUNT_MARKETS`). 체결 원장 쪽 식별은 `("upbit","live")`다(`vocabulary.py` `LEDGER_IDENTITY`). 이 문서는 코드 값 `upbit`를 쓴다.

## 3. 규칙

### 3.1 한 줄 요약

네 종목의 일봉 TSMOM 부호가 양이면 롱, 아니면 현금. 한 종목 한 포지션, 추가 매수 없음, 현물 롱 전용이라 숏 없음.

### 3.2 유니버스

`KRW-BTC`, `KRW-ETH`, `KRW-SOL`, `KRW-XRP`. DOGE 제외(데모와 같음). 심볼 변환은 `app/core/symbol.py`·기존 Upbit 심볼 해석만 쓴다.

### 3.3 신호

- 입력: Upbit KRW 일봉 종가(`get_ohlcv` 일봉, DB의 `daily_candles`). 거래 대상 자체의 종가를 쓴다 — 데모의 Binance USDT 종가가 아니다(권고 Q15).
- 결정 시각 `t` = 09:00 KST 일봉 마감. `s_t = sign(close_t / close_(t−L일) − 1)`, `L`은 권고 60일(Q1). 두 종가 중 하나라도 없으면 **NO_SIGNAL**(현금 유지), 앞 값으로 채우지 않는다.
- 데모는 4h 봉 마감마다 평가하지만 이 실험은 **일 1회**다. 건별 사람 승인이 4h 주기를 따라갈 수 없기 때문이다(Q9). 데모의 일봉 등가 종가는 00:00 UTC 4h 종가이고 업비트 일봉 경계도 00:00 UTC라 신호 시각은 맞는다.

### 3.4 진입·유지·청산

| 상태 | 신호 | 동작 |
|---|---|---|
| 현금 | `s_t > 0` | 매수 제안 카드 1건 |
| 현금 | `s_t ≤ 0` 또는 NO_SIGNAL | 아무것도 하지 않음 |
| 롱 | `s_t > 0` | 유지(추가 매수 없음) |
| 롱 | `s_t ≤ 0` | 전량 매도 제안 카드(신호 반전 청산) |
| 롱 | 진입가 대비 −10% 도달(§5의 점검 시점에 관측) | 전량 매도 제안 카드(hard stop) |
| 롱 | 보유 30일 | 전량 매도 제안 카드(time exit). 다음 결정 시각에 신호가 여전히 양이면 새 에피소드로 재진입 |

청산 우선순위는 hard stop → 신호 반전 → time exit(데모와 같음). hard stop 비율 10%와 time exit 30일은 데모의 상수 값을 권고값으로 가져온 것이며 이 실험의 값은 Q2·Q3에서 운영자가 확정한다.

### 3.5 크기(sleeve 안)

- 예산 `budget_cap` 1,950,000 KRW(운영자 결정). 한 leg의 상한 `LEG_CAP` = **480,000 KRW** 권고 — 4 × 480,000 = 1,920,000이고 나머지 30,000은 수수료·호가 단위 반올림 여유다. 예산을 4등분한 487,500을 쓰면 반올림·수수료로 예산 게이트(`used + order > budget_cap`)에 걸릴 수 있다(Q8).
- 변동성 스케일: `notional = LEG_CAP × min(1, σ* / σ̂)`, `σ*=60%`, `σ̂` = 일수익률 20일 EWMA의 연율화(√365). 상한을 넘는 확대 없음(데모 상수와 같은 값, Q8).
- 바닥: 계산된 notional이 **50,000 KRW 미만**이면 진입하지 않고 `skipped_below_floor`로 기록한다. 업비트 최소 주문은 5,000 KRW지만 수수료·호가 비율을 의미 있게 유지하려는 값이다(Q8, [의견]).
- 수량: `floor(notional / limit_price)`를 소수 8자리까지. 입력 허용 자릿수와 초과분 처리는 dots가 확정하지 못했다 — 시작 전 dry-run 미리보기로 운영자가 확인한다(§10, Q12).

## 4. 신호가 제안 카드가 되는 경로

> 개정(hk 5438, `decision/2026-11-19/at-go-executor`): 정기 진입·청산은 `docs/design/5438-go-execution-path.md`의 의도 경로(전략 잡 → intent → Go 실행기 → 게이트웨이)와 최소 위임장으로 대체된다. 아래 카드 경로는 의도 경로가 live로 열리기 전까지의 대체 경로이며, 평단×1.01 미만 청산(5438 §2.1 A)에서는 계속 쓰는 loss_cut 절차다. 이 실험은 5438 경로가 결정되고 shadow 종료 기준(5438 §9.1)을 통과할 때까지 seal하지 않는다.

기존 경로만 쓴다. 새 코드·새 도구·새 게이트가 없다.

1. **결정 시각 이후**, 운영자가 시작한 세션이 네 종목의 일봉을 읽어 §3의 상태·신호를 계산한다. sleeve 현황(`scripts/strategy_sleeves.py report`)과 보유 읽기로 현재 상태(현금/롱, 진입가, 보유 일수)를 확인한다.
2. 동작이 있는 종목마다 `order_proposal_create(symbol="KRW-<COIN>", market="crypto", account_mode="upbit", side=…, order_type="limit", proposer=…, rungs=[…], sleeve_id=<id>, strategy="upbit-trend-long-v1", thesis=…, rationale=…, valid_until=…)`를 호출한다. rung은 1개, `quantity`와 `limit_price`를 채운다.
3. **진입**: 일반 매수 제안. `limit_price`는 호출 시점 최우선 매도호가를 호가 단위로 맞춘 값(체결 가능한 지정가, taker)이다. 체결이 안 될 수 있는 호가 아래 대기 주문은 쓰지 않는다 — "신호 대비 체결률"이 측정 대상이기 때문이다(Q10).
4. **청산**: `side="sell"`, 최우선 매수호가 단위로 맞춘 지정가. 가격이 `avg_buy_price × 1.01` 이상이면 일반 매도 제안이다. 그 미만이면 일반 매도는 손실매도 가드에 거부되므로 **`exit_intent="loss_cut"`** 제안이어야 한다: `exit_reason`은 hard stop이면 `stop_loss`, 신호 반전·time exit이면 `thesis_change`, 제안 시점 72시간 이내의 `retrospective_id`(해당 심볼, trigger_type 일치), `approval_issue_id`. 호출 caller가 `LOSS_CUT_ALLOWED_AGENT_IDS`에 있어야 하고 `ORDER_PROPOSALS_SUBMIT_AGENT_ID`가 설정되어 있어야 한다. 이 값들의 설정은 env 변경이라 이 문서가 승인하지 않는다(Q13).
5. **카드**: Telegram 승인 카드. 일반 제안은 1클릭, `loss_cut`은 첫 클릭이 아무것도 제출하지 않고 두 번째 "손절 확인" 클릭(90초 단일 사용 nonce)이 제출한다.
6. **유효기간**: `valid_until`은 결정 시각 + 12시간 권고(기본값은 다음 00:00 KST). 기간 안에 승인되지 않은 카드는 만료되며 `expired_unapproved`로 기록한다(Q10).
7. **자동승인이 개입하지 못하게**: 마스터 게이트 `ORDER_PROPOSALS_AUTO_APPROVE`는 기본 false이고 이 문서는 바꾸지 않는다. 운영 값이 켜져 있어도 기존 자동승인은 호가에서 `min_distance` 이상 떨어진 대기 주문만 대상으로 하므로 3의 호가(체결 가능 지정가)는 대상이 아니다 [추정 — 운영 env 값은 읽지 않았다]. 시작 전에 운영자가 값을 확인한다(Q12).
8. **sleeve 게이트**: dispatch 직전에 `evaluate_sleeve_for_dispatch`가 sleeve 상태·손실·예산을 판정하고 결과를 카드에 사유 코드(`sleeve_*`)로 싣는다. 판정이 `allowed`가 아니어도 카드는 카드로 남는다(§2.2-3).

결정 로그: 제안 세션은 결정 시각마다 종목별 한 줄(신호값, σ̂, 크기, 동작 또는 no-op 사유)을 **레포 밖** 운영자 파일에 쓴다. no-op도 기록해야 미체결·미승인 신호의 분모가 생긴다. 이 문서는 형식을 코드로 정하지 않는다.

## 5. 하방은 무엇이 지키는가 — API stop이 없을 때

**사실부터.** 업비트 공개 주문 생성 API에는 stop/예약 주문 파라미터가 없다(limit/price/market/best만, dots §3). 업비트 서비스의 "예약 주문"은 감시가 도달 시 지정가를 내는 별도 기능이고 API로 만들 수 없으며, 체결도 손실 상한도 보장하지 않는다. 이 레포의 어떤 코드도 거래소 측 손절을 만들지 못한다.

### 5.1 지키는 것

| 장치 | 무엇을 지키는가 | 한계 |
|---|---|---|
| leg 상한 480,000 KRW와 변동성 스케일 | 한 종목의 최대 노출. 10% hard stop이 그대로 실행되면 leg당 약 48,000 KRW | 스케일은 σ̂가 클 때 노출을 줄이지만 갭을 막지 못한다 |
| sleeve 예산 1,950,000 KRW | 총 노출의 상한(현물이라 레버리지 없음). 매수는 `used + order > budget_cap`이면 `sleeve_budget_exceeded`로 카드에 사유가 붙는다 | 사유 표시일 뿐 사람이 승인하면 나간다(§2.2-3) |
| sleeve 손실 한도 1,000,000 KRW(누적) | 도달하면 sleeve가 `stopped`가 되고 이후 태그된 제안은 사유 `sleeve_not_live`/`sleeve_loss_limit_reached`를 단다 | **연속 감시가 아니다.** dispatch 시점에만 계산한다. 마크는 마지막 마감 일봉 종가라 장중 하락을 못 본다. 사람이 승인하면 막히지 않는다 |
| hard stop 10% 규칙 | 진입가 대비 −10%에서 청산 카드를 내게 한다 | **관측 시점에만 작동**(§5.2). 거래소에 걸려 있지 않다 |
| 사람 승인 | 모든 주문 직전의 판단 | 지연이 곧 노출 시간이다(§5.2) |
| loss_cut 슬립 밴드 | 손절 매도 지정가를 현재가 대비 정책 값(`sell.loss_cut_max_slip`, 기본 0.02) 이내로 제한 — 터무니없이 낮은 가격 주문을 막는다 | 밴드 안에서도 체결 가격은 불리할 수 있고, 급락 중 지정가는 체결되지 않을 수 있다 |

### 5.2 못 지키는 것

- **관측 사이의 손실.** hard stop은 누군가 가격을 보고 카드를 내야 작동한다. 스케줄러가 없으므로(하드룰 6) 점검 주기는 운영자가 시작한 세션의 빈도다. 권고 최소 주기는 결정 시각(09:00 KST) 직후 1회 + 저녁 1회(예: 21:00 KST)이고 하루 두 번이다. 그 사이 최대 약 12시간 동안 구멍이 있고, 운영자가 자는 동안은 점검 자체가 없다. 크립토는 24/7이라 이 시간에도 가격은 움직인다.
- **갭.** −10%에서 점검했는데 이미 −18%일 수 있다. 손절 카드를 승인해도 지정가 매도는 급락 중 체결되지 않을 수 있다.
- **승인 지연.** 카드 생성 → 운영자 확인 → (loss_cut이면) 두 번째 클릭 → 신선한 dry-run·슬립 밴드 재검증 → 전송. 운영자가 부재이면 카드는 `valid_until`까지 방치되고 만료된다 — 그 동안 노출은 그대로다.
- **손실 한도의 구속력.** 한도 1,000,000 KRW는 leg 4개가 풀 사이즈(1,920,000 KRW)일 때 네 종목이 **동시에 약 52% 하락**해야 닿는다. 현실의 하방을 실제로 제한하는 것은 한도가 아니라 leg 상한과 hard stop(그리고 그 점검 주기)이다. 한도는 "한도에 닿았으니 운영자가 멈춘다"는 운영 규칙의 신호일 뿐이다(§8).
- **거래소 장애·점검.** 업비트 점검·API 한도(주문 12회/초)·429/418 차단은 청산을 막을 수 있다(dots §4).

### 5.3 하방 보강이 될 수 있지만 여기서 쓰지 않는 것

- **업비트 앱 예약 매도**(운영자가 앱에서 직접 건다): 이 시스템이 보지도 귀속하지도 못하는 주문이다. 체결되면 sleeve 장부에는 보유가 남아 P&L이 어긋나고, 자산을 잠가 이 시스템의 매도 제안이 잔고 부족으로 실패한다. 이 실험에서는 쓰지 않는다(권고 Q11).
- **ROB-1460 기계식 손절**: 가격이 손절가 아래로 내려가면 제안을 만드는 새 부류이지만 스위치(`LOT_EXIT_PLAN_WRITE_ENABLED`·`MECHANICAL_LOSS_CUT_*`)와 시장·계정 allowlist가 기본 off이고 `thresholds_confirmed`가 필요하다. 켜는 것은 게이트 변경이라 이 문서가 승인하지 않는다. 설령 켜도 만들어지는 것은 "제안 + 사람 카드"라 §5.2의 승인 지연은 남는다.
- **buy 제안의 `exit_plan`**: 손절가를 기록해 두는 선택 필드이며(`order_proposal_tools.py:676-700`) 쓰기도 `LOT_EXIT_PLAN_WRITE_ENABLED` 뒤다. 보호 장치가 아니라 기록이다.

### 5.4 쿨다운과 손실매도 가드가 실험에 주는 영향

- **8일 쿨다운**: 어떤 크립토 매도든 체결 기준가가 `avg×(1−0.045)` 이하이면 그 심볼의 매수가 8일 거부된다. 10% hard stop 청산은 거의 확실히 걸린다. 그동안 신호가 양이어도 진입하지 못한다 — 이는 이 시스템 계정 단위 동작이며 데모에는 없다. 측정에서는 `blocked_by_cooldown`으로 따로 세고, 해당 신호는 미진입 에피소드(결과 0)로 코호트에 남긴다. 데모와 직접 비교할 때 이 차이를 명시한다.
- **손실 청산 = loss_cut**: 신호 반전·time exit 청산도 가격이 평단 ×1.01 미만이면 loss_cut 경로를 타야 한다. 회고 행(72시간 이내)을 먼저 저장해야 하므로 청산 카드 하나당 운영자 작업이 늘어난다. 이 마찰은 의도적 설계 위반이 아니라 기존 가드의 결과다.

## 6. 비용

dots 조사 값(2026-11-16 확인, 실행 시점 보증 아님):

| 항목 | 값 | 비고 |
|---|---|---|
| 일반 KRW 주문 수수료 | 0.05% / 편도 | 이벤트·등급으로 달라질 수 있고 해당 계정의 실제 적용률은 미확인. 실측은 체결 원장의 수수료가 우선 |
| 왕복 | 0.10% | 480,000 KRW leg 기준 약 480 KRW |
| 예약 주문 | 0.139%(고객센터 예시) | 이 실험은 쓰지 않는다 |
| 최소 주문 | 5,000 KRW | |
| 호가 단위 | 가격 구간표(dots §2) | 현재 BTC·ETH·SOL·XRP가 어느 구간인지는 확인하지 않았다. 실행 시 호가를 읽어 맞춘다 |
| 요청 한도 | 주문 12/초, 조회 30/초 | 일 1회 수동 평가라 문제되지 않는다 |

슬리피지는 모형화하지 않고 **측정**한다: 결정 시각의 기준 호가(최우선 매도/매수호가)와 실제 체결가의 차이를 에피소드마다 기록한다. 승인 지연의 효과가 여기에 들어가는 것이 이 실험의 핵심 측정이다.

## 7. 측정과 판정

### 7.1 무엇을 재는가

- **주 지표(primary)**: sleeve의 에피소드 결과 — 에피소드의 모든 체결을 첫 신호 후 `H`번째 마감 종가로 평가(마크)한 비용 차감 순손익을, 그 에피소드 시작 시점 예산 대비 %로 환산한 값. 기준선은 **현금(0)**이다. 즉 D = 에피소드 결과. ROB-1468 canary 통계의 정의 그대로이며 `scripts/strategy_sleeves.py report`가 계산한다(미체결 에피소드는 결과 0으로 코호트에 남고, H번째 종가가 없는 만기 에피소드는 `unmeasured`).
- `H` = 권고 30 세션(= time exit 30일, Q3). 크립토 세션은 일(UTC 일봉)이다.
- **보조 지표**: 신호 대비 체결률, 신호→승인→체결 지연, 기준 호가 대비 슬리피지, `expired_unapproved`·`declined` 카드 수, `blocked_by_cooldown` 수, loss_cut 경로 사용 수, 실현 수수료 대비 0.05% 모형, 누적 순손익과 sleeve 장부의 최대낙폭, 종목별 분해.
- **대조(연구에서 계산, 레포 밖)**: `control-cash`(주 기준선), `control-btc-hold`(같은 기간 BTC 단순 보유를 같은 예산 비중으로). auto_trader는 기준선을 기록하지 않는다(`baseline=not_recorded`). 참고로 데모 같은 신호의 Binance 데모 롱 레그와의 방향 일치도를 본다.

### 7.2 판정 규칙(사전 고정)

- 최대 기간(`max_duration_days`, 권고 84일, Q5)이 끝났을 때 만기된 에피소드가 **15개 미만**이면 결과는 `INSUFFICIENT_SAMPLE`이다 — 통과도 실패도 아니고 같은 manifest를 연장하지 않는다. 15는 [의견]이며 Q5와 함께 확정한다.
- 15개 이상이면: 에피소드 결과의 평균(예산 대비 %)이 `prereg_min_effect_pct`(권고 0.5%, Q4) **이상**이고 비용 차감 후 누적 순손익이 현금(0)보다 크면 "효과 있음(채점 대상)", 아니면 종료. 어느 쪽도 **통계적 유의성을 주장하지 않는다** — 종목 4개는 같은 시장의 강하게 상관된 표본이고 구간이 하나다. sleeve 보고서의 verdict 상태 `final_date_reached`는 통과가 아니라 "사전등록 대비 채점 대상"이다.
- 이 실험이 통과해도 **승격은 자동이 아니다.** 자동 집행·한도 확대·위임장 편입은 3795 이후 별도 승인이다.
- 탐색(backtest)은 새로 돌리지 않는다. L은 데모에서 한 번 고른 값을 쓰고 이 가설의 `tuning_budget`은 0이다(Q1). 이 PR은 테스트 구간을 읽지 않고 `k`를 올리지 않는다.

## 8. 중단 조건

아래 중 하나라도 해당하면 신규 진입을 멈추고(`sleeve set --state paused` 또는 `stopped`) 열린 포지션은 카드로 청산한다. 청산 여부의 판단은 운영자다.

1. **손실 한도**: sleeve 누적 순손실 ≥ 1,000,000 KRW. sleeve가 `stopped`가 된다. 재개는 새 sleeve(새 epoch)다.
2. **leg 초과 손실**: 한 leg의 청산 손실이 진입가 대비 −15%를 넘는다(hard stop −10% 규칙이 갭·승인 지연으로 실패했다는 증거). 신규 진입 보류 후 점검 주기·크기 재검토.
3. **실행 불가**: 4주 구간에서 진입·청산 카드의 30% 초과가 만료·미승인이다(사람 승인 지연이 규칙을 실행 불가로 만든다는 증거). 결과를 `INSUFFICIENT_SAMPLE`이 아니라 `not_executable_by_card`로 보고하고 종료한다.
4. **측정 불가**: `sleeve_pnl_unmeasured`(마크 없음)·`sleeve_used_unmeasured`·귀속 불가 체결이 이틀 이상 지속되면 보류(미측정은 0이 아니다).
5. **계정 오염**: 시작 전 점검(§10)에서 이 네 코인의 sleeve 밖 매수·보유가 확인되고 처리되지 않으면 시작하지 않는다. 진행 중 sleeve 밖 거래가 같은 코인에 생기면 보류.
6. **최대 기간 도달**: `final_date_reached` — 채점하고 열린 포지션은 카드로 청산한다(권고: 7일 안).
7. **규칙·유니버스·상한·비용표 변경**: 새 manifest version과 새 sleeve epoch. 이 실험 안에서 바꾸지 않는다.
8. **데모와의 연계 점검(자동 중단 아님)**: 데모 봉인 구간 종료일(2027-01-09)에 데모 결과와 이 실험의 중간 결과를 운영자가 함께 본다.

## 9. 이 문서가 승인하지 않는 것

- 자동 집행·위임장·계획 결속 자동승인, `ORDER_PROPOSALS_AUTO_APPROVE*` 변경, 자동승인 범위 확대. (개정 hk 5438: 자동 집행은 5438 의도 경로 + 최소 위임장으로 설계한다. 그 경로의 위임장 행 승인과 shadow→live 전환은 별도 운영자 행위다.)
- 스케줄러·cron·Prefect·Dagu 등록(신호 평가·손절 점검·카드 생성 포함). 5438의 결정·점검 Dagu 잡 등록도 별도 승인이다.
- env·게이트·allowlist 변경 — `LOSS_CUT_ALLOWED_AGENT_IDS`·`ORDER_PROPOSALS_SUBMIT_AGENT_ID` 설정, ROB-1460 스위치, Upbit 관련 어떤 스위치도.
- 코드 변경 — sleeve 규칙 id 추가(Q7), TREND 패키지, 손실매도 가드·쿨다운·loss_cut 슬립 값, 정책 YAML.
- 실주문, sleeve 행 생성·상태 변경, 마이그레이션, 배포. 실주문 경로 변경·배포는 기존대로 내용 기준 승인이다.
- 건별 주문 승인 — 카드는 운영자가 건마다 승인한다. (개정 hk 5438: 의도 경로가 live가 되면 위임장 범위 안의 주문은 카드 없이 집행되고, 평단×1.01 미만 청산과 계정 평단을 증명할 수 없는 청산만 5438 S5 청산 인계를 거쳐 카드로 남는다. 정지된 sleeve의 청산은 카드가 아니라 5438 S6 운영자 청산이다.)
- 위임장 문서(`docs/design/3795-mandate.md`) 수정, Binance 데모(ROB-3796) 규칙·상수 변경.
- manifest seal(이 PR은 draft). seal은 값 확정 뒤의 별도 PR이다.

## 10. 시작 순서와 선행 조건

1. 이 PR 병합(설계 + manifest draft, `k=0`).
2. 운영자가 §11의 열린 결정에 답한다.
3. **코드 PR**(별도): `CANARY_RULES`에 추세추종 규칙 id 추가 + `docs/runbooks/strategy-sleeves.md` 규칙 표 갱신. 이것이 sleeve 생성의 선행 조건이다(§2.2-1).
4. 시작 전 운영자 확인(코드 변경 아님): (a) Upbit 계정의 BTC·ETH·SOL·XRP 기존 보유와 평단, (b) `ORDER_PROPOSALS_AUTO_APPROVE`의 운영 값, (c) `LOSS_CUT_ALLOWED_AGENT_IDS`·`ORDER_PROPOSALS_SUBMIT_AGENT_ID` 설정 여부, (d) 수량 입력 자릿수·초과분 처리(dry-run 미리보기), (e) 실제 적용 수수료율(공개 수수료표·이벤트).
5. sleeve를 `shadow`로 생성(사전등록 4개 필드 포함 — 운영 명령은 `docs/runbooks/strategy-sleeves.md`). shadow 동안 태그된 주문은 전부 카드다.
6. **manifest seal PR**(별도): 값 확정, 코드 SHA·비용표 해시 기록, `status=sealed`, version+1, registry 상태 전이. 봉인 구간의 시작은 그 PR 병합 시점이다. registry 상태가 `running`이 되면 `heartbeat.json`에 이 가설의 epoch가 필요하다(린트 규칙) — 무엇이 advance를 찍는지(예: sleeve 보고서 스냅샷)는 seal PR에서 정한다.
7. 운영자가 sleeve를 `live`로 전환한다(사전등록 동결). 이 전환은 이 문서의 승인 범위가 아니며 별도 운영자 행위다.
8. 이후 일 1회 결정 시각 + 저녁 점검을 수동으로 수행한다.

로컬 검증 명령: `python3 scripts/ci/experiment_registry_lint.py`(CI의 `lint` 잡이 같은 스크립트를 `--base-registry` 인자와 함께 실행한다).

## 11. 열린 결정

각 항목은 코드와 기존 결정만으로 정할 수 없다. 답이 없으면 권고안이 아니라 **미결**이다. manifest의 해당 값은 `pending`이고 사유에 권고값을 적어 두었다.

1. **L (룩백 일수).** 권고: **60**. 데모 봉인이 같은 값을 쓰고, 이 실험은 새 탐색을 돌려 같은 3년 구간에서 두 번째 선택을 하지 않는다(`tuning_budget=0`). 다른 값(20·120)을 원하면 Upbit KRW 일봉으로 별도 탐색을 먼저 돌려야 하고 그 설정 수가 `tuning_budget`을 쓴다.
2. **hard stop 비율.** 권고: **진입가 대비 10%**(데모 `SEAL_HARD_STOP_FRACTION`과 같음). 4h 사이 극값 감시는 이 실험에서 불가능(스케줄러 없음)하므로 §5.2의 점검 주기가 감시 해상도다.
3. **평가 구간 H와 time exit.** 권고: **30 세션 / 30일 time exit**(데모 `TIME_EXIT_DAYS`와 같음). H가 길수록 만기 에피소드 수가 줄어든다.
4. **최소 효과 `prereg_min_effect_pct`.** 권고: **0.5**(에피소드 평균 결과가 예산 대비 +0.5% 이상 = 예산 1,950,000 KRW 기준 약 9,750 KRW). 왕복 비용 0.10%가 leg 대비라 예산 대비로는 약 0.025%라 비용 대비 충분한 여유를 둔 값이다. 값은 [의견]이며 운영자가 정한다.
5. **최대 기간 `prereg_max_duration_days`와 최소 표본.** 권고: **84일(12주) / 만기 에피소드 15개**. 데모 탐색(L=60)의 롱 진입 약 9건/월(335건/36개월)에서 일 1회 평가로 더 줄어드는 것을 감안해도 12주면 15개 안팎을 기대할 수 있다 [추정 — 다른 시장·평가 주기의 탐색에서 환산]. 8주는 표본이 너무 적어 `INSUFFICIENT_SAMPLE`일 가능성이 높다.
6. **"월 손실 한도 1,000,000"의 해석.** 권고: sleeve `loss_limit` = **1,000,000 KRW 누적**(코드가 지원하는 유일한 형태, 실험 전 기간에 적용 — 월 리셋보다 엄격하다) + 운영자가 월 단위로 별도 점검. 월 리셋이 필요하면 새 sleeve epoch로만 가능하다.
7. **sleeve 규칙 id.** 권고: 별도 코드 PR로 새 규칙 id(예: `trend.upbit_long_tsmom_v1`, markets `{crypto}`)를 추가한다. 기존 `rob1458.crypto_b3`(다른 규칙·다른 지표)를 재사용하면 라벨이 거짓이 된다. 이 코드 PR 전에는 sleeve를 만들 수 없다.
8. **크기 상수.** 권고: `LEG_CAP`=**480,000 KRW**, `σ*`=60%, EWMA 20일, 바닥 50,000 KRW(미만이면 미진입).
9. **평가·점검 주기.** 권고: 신호는 **일 1회**(09:00 KST 일봉 마감 직후). 손절 점검은 **하루 두 번 이상**(결정 시각 직후 + 저녁). 4h 평가는 쓰지 않는다. 운영자가 약속할 수 있는 실제 빈도를 정해야 하고, 못 지킬 주기는 쓰지 않는다.
10. **주문 방식.** 권고: 진입·청산 모두 **호가에 맞춘 체결 가능 지정가**(taker), `valid_until`=결정 시각 + 12시간. 호가 아래 대기 주문은 쓰지 않는다. 시장가·최유리 IOC는 제안 경로가 지원하는지 이 문서가 확인하지 않았다.
11. **업비트 앱 예약 매도 백스톱.** 권고: **쓰지 않는다**(§5.3). 이 시스템이 귀속하지 못하고 매도 제안을 막는다.
12. **시작 전 확인**(§10-4의 a~e). 권고: 전부 확인하기 전에는 sleeve를 `live`로 전환하지 않는다. 특히 (a)에서 기존 보유가 있으면 평단 가드·쿨다운이 sleeve 밖 매수와 섞인다 — 기존 보유가 있는 코인은 유니버스에서 빼거나 별도 결정한다.
13. **loss_cut caller·제출 identity env.** 권고: 시작 전에 설정 여부만 확인한다(없으면 설정은 env 변경 승인이 별도로 필요). 없으면 손실 청산이 거부되므로 실험을 시작하지 않는다.
14. **다른 크립토 매수 흐름과의 겹침.** 권고: 같은 네 코인에 대한 다른 Upbit 매수 흐름(기존 held-majors 지지선 티어 등)은 이 실험 기간 동안 쓰지 않는다. 계정 단위 평단과 쿨다운을 공유해 측정을 오염시킨다.
15. **신호 데이터 출처.** 권고: **Upbit KRW 일봉 종가**(거래 대상 자체). Binance USDT 종가를 쓰면 환율·김치 프리미엄이 신호와 체결 사이에 끼어든다. 데모와 같은 신호를 비교하려면 별도로 계산한다.

## 12. 알았지만 건드리지 않은 것

- 어휘·sleeve 코드의 규칙 id 추가(§2.2-1, Q7): 코드 변경이라 범위 밖. 선행 조건으로만 기록했다.
- 손절 쿨다운의 8일·4.5%, 손실매도 가드의 1.01, loss_cut 슬립 기본 0.02: 하드 인바리언트라 바꾸지 않았다. 영향만 §5.4에 적었다.
- 위임장(3795) 이후의 자동 집행: 이 실험이 사람 카드로 얻는 측정(승인 지연, 체결률)이 그 설계의 입력이 될 수 있다 — 연결은 별도 설계다.
- Upbit 수량 입력 자릿수·초과분 처리, 수수료 이벤트 전체: dots가 확정하지 못했고 운영자 확인 사항으로 남겼다(§10-4).
- 운영 env의 실제 값(`ORDER_PROPOSALS_AUTO_APPROVE` 등)은 읽지 않았다.
- Binance 데모(ROB-3796)와 같은 가설 id를 쓰지 않았다. 이 가설은 `ROB-4885`로 따로 등록했고 `k`는 각자 센다.
