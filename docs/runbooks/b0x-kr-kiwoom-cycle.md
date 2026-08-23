# B0-X KR — `kiwoom_mock` cycle (§39차 한시 대체 venue)

`scripts/run_b0x_kr_kiwoom_cycle.py` — 수동 kickoff 전용. **스케줄러 등록 없음**
(TaskIQ·Prefect·launchd 어디에도 없다). 사이클은 운영자가 실행해서 일어난다.

기본 지위는 **`OBSERVATION_DERIVATION_ONLY`**(preview)다. `--confirm` 단독은
**`ACCEPTANCE_ONLY`** — 기존의 1건 submit→cancel→reconcile 검증 레버다.
`--ordering --confirm` 을 함께 명시한 경우만 독립 모드인 **`ORDERING`** 으로
전환한다. 정책표가 결정적으로 파생한 주문만 §4 cap 안에서 DAY로 제출하고,
`ACCEPTANCE_ONLY` 의 즉시취소를 상속하지 않는다. 매도는 fresh broker fill로
증명된 자기 귀속 수량까지만 가능하다. 동일 cycle의 동일 symbol BUY 다단은
v1.8의 KR 전용 일괄 경계를 사용한다. 위험한 쪽은 기본값이 아니다.

---

## 1. 왜 kiwoom 인가 (§39차)

`kis_mock` 이 `40910000 모의투자 주문이 불가한 계좌입니다` 로 **계좌 단위 거절**
중이라, B0-X KR venue 를 한시 대체한다. kiwoom mock 의 기술 이점은 **미체결
조회와 취소가 실제로 지원된다**는 것이다 — kis_mock 의 구조적 약점이 없다.

`scripts/b0x/kr/mock.py`(kis) 는 **무접촉**이다. kis_mock 계좌 참가가 복구되면
그 레인은 이 PR 이전과 완전히 동일하게 동작해야 한다.

## 2. 계좌맵 (operator `a43e36e`, PR #39; merge 대기)

| 표면 | 키 | 값 |
| --- | --- | --- |
| `operator_contract.yaml` | `account_lanes.kiwoom_mock` | `KR-B1` |
| `operator_contract.yaml` | `b0x_adapter_orders_20260808.surfaces` | ∋ `kiwoom_mock` (§39차 한시, KRX RTH only) |
| `operator_contract.yaml` | `b0x_adapter_orders_20260808.writer` | `b0x_adapter_single` |
| `mock/CLAUDE.md` | 「계좌 전면 배타」 | MCP 뿐 아니라 **전 접속 경로** |

🔴 **B0-X 는 이 계좌의 단독 소유자가 아니라 공존 배정이다.**

- KR-B1 주문 발행과 **동시 사용 금지**
- KR-B1 재가동 시 재결정 · `kis_mock` 복구 시 복귀
- 배타성 확보 = **운영 조치**(KR-B1 비활성 확인). 코드가 제공하는 것은
  *탐지*뿐이다: 당일 자기 외 주문 흔적이 있으면 preflight 가 fail-closed
  (`CONTAMINATED_foreign_same_day_orders_kr_b1_active_suspect`).
- ORDERING 은 redacted account fingerprint를 키로 한 host-local `fcntl` writer
  lease를 추가로 잡는다. 이는 다른 host/외부 writer를 대신하지 않으므로, **매
  mutation 직전** kt00007의 pending과 당일 foreign trace를 같은 답에서 다시
  만들고, lease 상실·조회 실패·foreign 흔적이면 그 뒤 주문은 0이다.

## 3. 안전 경계

| 경계 | 강제 지점 |
| --- | --- |
| Default-off | `B0X_KR_KIWOOM_ENABLED` 미설정 → `KiwoomLaneDisabled` |
| Mock 호스트 only | `KiwoomMockClient` 생성자 + `send` 직전 host 재검증 + 레인 자체 `assert_mock_host` (3층) |
| live 표면 금지 | AST 가드 — `api.kiwoom.com`/`LIVE_BASE_URL`/live 자격증명 이름을 **문자열로도** 금지 |
| KRX only | 주문 클라이언트가 `NXT`/`SOR` 를 네트워크 호출 **전에** 거부. 레인은 `exchange` 파라미터를 아예 노출하지 않는다 |
| 세션 | KRX 정규장만 (`is_krx_regular_session`, XKRX 캘린더) |
| envelope | §4 KR 열 **그대로 재사용** (`load_envelope("kr")`). 레인 전용 envelope 상수 없음 — 테스트가 강제 |
| Acceptance 1건 한정 | `ACCEPTANCE_SUBMISSION_LIMIT = 1`, CLI/env override 없음. `--confirm` 단독에만 적용 |
| ORDERING 별도 모드 | `--ordering --confirm` 에서만 policy-table 결정 파생 주문을 제출. 별도 cap/CLI/env override 없음 — §4 30만·동시 10·일신규 3이 파생 단계에서 계속 강제 |
| 같은-cycle BUY 묶음 | 동일 결정적 `cycle_id`·동일 symbol·BUY인 2개 이상 계획만 허용. 묶음 직전 공용 재제출 게이트 1회; 다른 cycle/symbol/SELL은 authorization 불가 |
| 묶음 부분 실패 | 이미 ack된 DAY 주문은 보상 취소하지 않고 보존, 다음 mutation 즉시 중단, `partial_failure` + exit 2 + accepted/remaining key 기록. 성공 상태로 표시 금지 |
| 매도 귀속 | 매도 직전 fresh broker fill × own-order journal로 자기 수량을 다시 계산해 `assert_sell_is_own`으로 제한. 귀속 불명/부족은 sell 0 |
| Acceptance 왕복 강제 | `--no-cancel` 같은 플래그가 **존재하지 않는다**. `ACCEPTANCE_ONLY` 취소 미확인 = `RoundTripIncomplete` + exit 2 |
| DAY 주문 잔존 | `ORDERING` 은 즉시 취소하지 않는다. `broker_ack`은 접수/주문번호 증거일 뿐 filled가 아니며 후속 kt00007 readback이 partial/fill/remaining/VWAP/slippage를 보존 |
| Mutation boundary | 매 submit/cancel 직전 lease + kt00007 one-read에서 pending과 same-day foreign trace를 동시 재조회. 읽기 실패·foreign·lease 상실은 다음 mutation 0 |
| 일손실 입력 | dedicated realized P&L 증거가 없으면 current-day own activity 없는 bootstrap만 0을 증명할 수 있다. 그 밖의 unreadable은 kill 0으로 치환하지 않고 주문 0 |
| Kill | kill 발화 시 신규 주문 0, 자기 resting 전부 kt10003 cancel 시도, 각각과 최종 상태를 kt00007 재조회로 확인. cancel 응답만으로 성공 기록 금지 |
| halted | 표가 이미 제외(ROB-1236). 레인이 `universe.halted_suspect` 를 두 번째 선으로 재검사 |

## 4. 🔴 자기 미체결 = 브로커 직접 조회, 원장 예외 **미사용**

계약 v1.6 ① 의 `review.kis_mock_order_ledger` 예외는 **브로커 표면 부재** 한정
(`TTTC8036R` 이 `is_mock=True` 에서 raise). kiwoom 은 답하므로 이 레인은 그
예외를 쓰지 않으며, 쓰면 계약 위반이다. AST 가드가 격추한다.

### 4.1 게이트는 kt00009 가 아니라 **kt00007** 이다 (2026-08-12 실측)

명목상 미체결 표면은 `kt00009`(계좌별주문체결현황요청)지만, **실측 결과 미체결이
살아 있는데도 `return_code=0` + 빈 배열**을 반환했다:

```
12:11–12:20 KST, mockapi.kiwoom.com
  B0-X 주문 0107387 / 0108695 / 0109507 이 계좌에 살아 있는 동안
  kt00009 → return_code=0, rows=0        ← 빈 답
  kt00007 → 당일 6행 전부 반환(매수 3 + 취소 3)
```

미체결이 있는데도 빌 수 있는 답은 **미체결 부재를 증명하지 못한다** — 계약
v1.5 ① 이 실격시키는 성질이고, ROB-341 이 KIS 일별체결조회를 비-게이팅 진단으로
강등시킨 것과 같은 결함 클래스다. kt00009 위에 재제출 게이트를 세웠다면 게이트가
**공허(vacuous)** 해졌을 것이다: 모든 심볼이 비어 보이고, 모든 「취소 확인」이
자동으로 참이 된다(애초에 목록에 없었으므로).

→ 게이트 = `kt00007` 의 브로커 자체 `ord_remnq > 0`.
→ `kt00009` 는 `order_status_diagnostic` 으로 **기록만** 한다(모의서버가 고쳐지면
   이 카운트가 맞기 시작하는 것으로 보인다). ROB-1088 이 미검증으로 남긴 5필드
   body 의 첫 실호출 검증이기도 하다.

### 4.2 own_pending 은 계좌 전체 상위집합이다

`kt10000` 의 body 는 5필드뿐이라 **클라이언트 correlation 을 받지 않는다**.
「내 것」은 로컬 저널로만 성립하므로, 재제출 게이트를 로컬 상태에 의존시키지
않으려고 **계좌 전체 미체결**을 게이트 입력으로 쓴다 — 계약이 요구하는 자기
미체결의 상위집합이라 항상 더 많이 막고 덜 막지 않는다. 자기 분은
`own_symbols` 로 따로 기록한다.

### 4.3 v1.8 — 같은 cycle BUY 사다리의 게이트 1회 + 묶음 제출

**cycle 경계는 호출자가 붙이는 임의 문자열이 아니다.** `derive_orders`가
`policy_table_hash + 제출 전 account_state_hash + locked envelope_hash + lane`으로
계산한 결정적 `cycle_id`를 `plan_orders`가 각 `PlannedOrder`에 직접 스탬프한다.
`authorize_same_cycle_buy_batch`는 2개 이상 주문의 `cycle_id`·symbol·side·order key
순서가 정확히 같을 때만 authorization을 만들 수 있다. SELL과 단건 주문은 기존
`submit_day_order`를 계속 사용하며 매 호출마다 공용 게이트를 탄다.

ORDERING은 동일 symbol의 연속 BUY 다단을 하나로 묶고, 묶음 **직전**의 fresh
`MutationBoundary`에서 만든 broker truth로 `assert_resubmit_allowed`를 정확히 1회
검사한다. 첫 다리 뒤 생긴 자기 미체결은 같은 authorization의 후속 다리를 막지
않는다. 대신 후속 다리마다 lease + kt00007 foreign trace를 다시 읽는다. 그 사이
외부 주문이 끼면 다음 다리 전에 차단된다. 각 broker 답변과 실제 전송 사이의
원자적 잠금은 venue가 제공하지 않으므로 기존과 같은 짧은 TOCTOU는 남으며,
artifact의 mutation boundary 시각으로 드러낸다.

**사이클 간 방어는 불변이다.** 다음 broker snapshot이 이전 cycle의 자기 미체결을
보면 새 `account_state_hash`와 새 `cycle_id`가 만들어지고, 파생 단계의 v1.5 ①
게이트가 해당 symbol 전체를 `own_pending_order_exists`로 제거한다. 이전 cycle의
authorization은 cycle/order-key exact + 일회성이므로 재사용할 수 없다.

**부분 실패 방침은 잔존이다.** 다리 N 전송이나 readback이 실패하면 앞서 ack된
DAY 주문에 보상 cancel을 새로 보내지 않는다. cancel 자체가 추가 broker mutation이고
원실패 뒤 증거가 불완전한 상태에서 위험을 키울 수 있기 때문이다. 추가 제출을
즉시 중단하고 exit 2, `PARTIAL_BATCH_FAILURE`, accepted/remaining order key,
`compensating_cancel_attempted=false`를 cycle JSONL과 Markdown 표 양쪽에 남긴다.

**cap은 묶음 밖에서 이미 전량 소비된다.** derivation은 기존대로 다리마다 주문당
30만과 종목 총 150만을 소비하고, L1/L2는 일일 신규 symbol 하나로 센다. 일일 신규
3종목이면 최대 `3 symbols × 2 BUY legs × 300,000 = 1,800,000 KRW/cycle`이며,
묶음 authorization은 주문을 추가하거나 크기를 다시 계산하지 않는다.

## 5. 🔴 legacy 불가침 귀속 게이트 (§39차 ③, #1835 패턴)

- 매수(+) = **브로커가 확인한 체결 수량**만 가산 (접수/미체결은 우리 수량이 아님)
- 매도(−) = 저널의 요청 수량 **상태 무관 전량** 차감
- 최종 상한 = **브로커 보유 수량** (저널이 뭐라 하든 계좌에 없는 주식은 우리 것이 아니다)
- 귀속 불가 = 「자기 것 없음」이 아니라 「알 수 없음」 → 자기 포지션 0 **그리고**
  §4 상한 입력 = 계좌 전체 → 양방향 차단
- NAV = `cash + 자기 귀속 평가금액`. legacy 평가금액 제외 — §4 kill 이
  `pct_of_nav` 라 legacy 를 넣으면 임계가 **넓어진다**. 넓어지지 않는 방향만 고른다.
- 매도는 제출 경계에서 `assert_sell_is_own` 이 한 번 더 본다(중복이 의도).

### 5.1 저널 = 「이 ord_no 는 우리 것이다」

`<out-dir>/kiwoom_mock/own-orders.jsonl`, append-only.
🔴 제출 직후의 자기 주문번호는 즉시 저널한다. `ORDERING` 의 DAY 주문도
다음 사이클의 당일 외부흔적 게이트가 자기 주문으로 식별할 수 있어야 한다.
`ACCEPTANCE_ONLY` 에서는 **취소도** 자기 주문번호를 갖는다(실측: 매수 `0107387` →
취소 `0107388`). 취소 ord_no 를 기록하지 않으면 다음 사이클의 당일 외부흔적 게이트가
자기 취소를 제2 writer 로 오인해 정지한다 — 2026-08-12 12:13 KST 에 실제로 발생했다.
그래서 취소도 `side="cancel"` 로 저널한다(귀속 수량에는 영향 없음).

### 5.2 ORDERING fidelity journal

`<out-dir>/kiwoom_mock/ordering-events.jsonl`도 append-only다. 한 주문의 evidence
path는 `table_price → intended_limit → broker_ack → broker_readback
(partial/fill VWAP/remaining) → reconcile`로 보존된다. kill에서는 같은 저널에
`cancel_ack → cancel_reconcile → final_reconcile`가 추가된다. cycle artifact는 이
저널의 요약일 뿐, ack/fill/cancel의 유일한 증거가 아니다.

## 6. Rate limit — 페이싱은 안전 속성이다

`MIN_CALL_INTERVAL_SECONDS = 1.5` (모듈 상수, CLI/env override 없음).

2026-08-03 mock 프로브: 2.0s/1.0s/0.5s OK · 0.2s/0.05s `HTTPStatusError`.
그런데 2026-08-12 12:11 KST 첫 시도가 **9콜 / 약 5초**(≈0.55s 간격, 그 "OK" 대역
안쪽)로 나갔는데도 **9번째 = 취소 후 reconcile 조회**가 `HTTPStatusError` 로
실패했다. 즉 per-call 간격만의 문제가 아니라 그 길이의 버스트가 창을 넘긴다.

reconcile 조회를 잃는다는 것은 **제출한 주문의 취소를 증명할 수 없는 상태**라서,
페이싱은 예의가 아니라 안전 속성이다. (그때도 fail-closed 는 정상 작동했다:
exit 2 + `cancel_unconfirmed`, 그리고 실제로 취소 자체는 성공해 계좌는 flat 이었다.)

## 7. 실행

```bash
# 자격증명과 Redis 설정은 배포 env 파일에서 Settings가 읽는다. 별도 process
# REDIS_URL export는 필요하지 않으며, 값은 출력하지 않는다.
export ENV_FILE=/path/to/.env.prod.native

# ① 읽기 전용 준비상태 프로브 (주문 없음)
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle --readiness

# ② 프리뷰 — 파생 + 계획, 디스패치 0
uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables

# ③ acceptance 왕복 1건 (KRX 정규장 안에서만, 기본 안전 mutation 모드)
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables --confirm

# ④ ORDERING — 이중 명시가 있어야만 DAY 주문을 남긴다.
#    독립검증 PASS 및 배포 실증 전에는 이 명령을 실행하지 않는다.
#    정책표 파생만, own-only sell, mutation-boundary 재조회, 자동취소 없음.
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables \
    --ordering --confirm
```

exit code: `0` 정상 · `2` writer lock 경합, acceptance 왕복 미완(취소 미확인),
또는 ORDERING ack/readback/cancel 재확인 미완. 🔴 `2` 를 보면 계좌에 미회수 주문이 남았을 수 있다 —
`--readiness` 로 즉시 `pending` 을 확인하고, 남아 있으면 운영자가 직접 취소한다.

## 8. 아티팩트

`<out-dir>/kiwoom_mock/` — `cycles.jsonl`(append-only) ·
`<ts>-cycle.md` · `own-orders.jsonl` · `ordering-events.jsonl` ·
`operator-notices.jsonl`.

모든 아티팩트에 `COEXISTING_ACCOUNT_LANE` 라벨이 붙는다: 이 계좌의 보유·체결
이력 전부를 B0-X 산출로 읽으면 안 된다.

BUY 묶음이 있으면 Markdown에 `Same-cycle BUY batches` 표가 추가된다. `cycle_id`,
symbol, status, planned 수, accepted/remaining order key, 보상취소 여부와 실패 사유를
표시한다. `partial_failure`는 헤더에도 `PARTIAL_BATCH_FAILURE`로 중복 표시된다.

## 9. 알려진 경계 (조용히 넘어가지 않는 것들)

1. **계좌 배타성은 여전히 운영 조치다.** account-keyed local lease는 같은 host의
   B0-X writer만 배타화한다. 다른 host/KR-B1 writer는 broker foreign trace가
   방어한다. ORDERING 은 preflight 1회에 의존하지 않고 매 mutation boundary에서
   동일 kt00007 답으로 재검사하지만, broker 자체가 답한 뒤 발생한 외부 변화까지
   원자적으로 잠글 수는 없다.
2. **legacy 귀속 게이트는 2026-08-12 실환경에서 *증명되지 않았다*.** 그날
   kiwoom_mock 계좌의 보유가 **0종목**이라 legacy 분기에 도달할 입력이 없었다.
   단위 테스트로만 증명된 상태다(보유가 생기는 첫 사이클이 첫 실환경 검증 기회).
3. **같은-cycle BUY 묶음과 cycle 간 dedup은 실환경 미증명.** 단위 테스트는 한
   cycle의 L1/L2가 게이트 1회 뒤 모두 제출되고, 다음 cycle은 이전 DAY 미체결 때문에
   `own_pending_order_exists`로 0건임을 함께 증명한다. venue는 묶음 원자 API를
   제공하지 않으므로 다리별 ack/readback과 부분 실패 표면은 계속 필요하다.
4. **dedicated realized P&L source는 아직 없다.** own-order journal이 current-day
   activity 없음까지 증명하는 bootstrap에서만 0을 쓴다. activity·journal 오류·시간
   정보 오류가 있으면 −2.5% NAV kill을 미발화로 꾸미지 않고 ORDERING 자체가 0이 된다.
5. **저널 유실 방향은 안전하다** (과소 귀속 → 매도 못 함). 다만 저널이 사라지면
   자기 보유가 legacy 로 재분류되어 영영 팔 수 없게 된다 — 백업 대상이다.
