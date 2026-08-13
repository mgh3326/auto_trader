# B0-X KR — `kiwoom_mock` cycle (§39차 한시 대체 venue)

`scripts/run_b0x_kr_kiwoom_cycle.py` — 수동 kickoff 전용. **스케줄러 등록 없음**
(TaskIQ·Prefect·launchd 어디에도 없다). 사이클은 운영자가 실행해서 일어난다.

기본 지위는 **`OBSERVATION_DERIVATION_ONLY`**(preview)다. `--confirm` 단독은
**`ACCEPTANCE_ONLY`** — 기존의 1건 submit→cancel→reconcile 검증 레버다.
`--interim-ordering --confirm` 을 함께 명시한 경우만 **`INTERIM_ORDERING`** 으로
전환해 envelope 파생 **매수** 전건을 DAY 주문으로 남긴다. 매도는 기본 ON인
매수 전용 게이트가 제출 경계에서 차단한다. 위험한 쪽은 기본값이 아니다.

---

## 1. 왜 kiwoom 인가 (§39차)

`kis_mock` 이 `40910000 모의투자 주문이 불가한 계좌입니다` 로 **계좌 단위 거절**
중이라, B0-X KR venue 를 한시 대체한다. kiwoom mock 의 기술 이점은 **미체결
조회와 취소가 실제로 지원된다**는 것이다 — kis_mock 의 구조적 약점이 없다.

`scripts/b0x/kr/mock.py`(kis) 는 **무접촉**이다. kis_mock 계좌 참가가 복구되면
그 레인은 이 PR 이전과 완전히 동일하게 동작해야 한다.

## 2. 계좌맵 (operator `09c3fc1`, PR #37)

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
- 이 계좌에는 `kis_mock` 의 `KISMockWriterLease` 같은 durable lease 가 **없다**.
  아티팩트의 `writer_lease.acquired` 는 `false` 로 기록되며, 이 레인은 lease 를
  가졌다고 주장하지 않는다.

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
| Interim 매수 전건 제출 | `--interim-ordering --confirm` 에서만 envelope 파생 매수 leg 전건 제출. 별도 제출 상한/CLI/env override 없음 — §4 30만·동시 10·일신규 3이 파생 단계에서 계속 강제 |
| Interim 매수 전용 | 매도 leg는 파생되더라도 제출 경계의 `interim_buy_only_sell_gate`가 차단하고 artifact에 사유를 남긴다. 기본 ON이며 CLI/env 해제 경로 없음; 명시 운영자 승인 또는 B-track merge 전까지 유지 |
| Acceptance 왕복 강제 | `--no-cancel` 같은 플래그가 **존재하지 않는다**. `ACCEPTANCE_ONLY` 취소 미확인 = `RoundTripIncomplete` + exit 2 |
| DAY 주문 잔존 | `INTERIM_ORDERING` 은 즉시 취소하지 않는다. `submitted` 는 접수/주문번호 증거만 뜻하고 filled를 뜻하지 않는다 |
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
🔴 제출 직후의 자기 주문번호는 즉시 저널한다. `INTERIM_ORDERING` 의 DAY 주문도
다음 사이클의 당일 외부흔적 게이트가 자기 주문으로 식별할 수 있어야 한다.
`ACCEPTANCE_ONLY` 에서는 **취소도** 자기 주문번호를 갖는다(실측: 매수 `0107387` →
취소 `0107388`). 취소 ord_no 를 기록하지 않으면 다음 사이클의 당일 외부흔적 게이트가
자기 취소를 제2 writer 로 오인해 정지한다 — 2026-08-12 12:13 KST 에 실제로 발생했다.
그래서 취소도 `side="cancel"` 로 저널한다(귀속 수량에는 영향 없음).

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
# 자격증명은 배포 env 파일에서. REDIS_URL 은 OAuth 토큰 캐시용으로 프로세스
# 환경에 필요하다(pydantic settings 가 아니라 os.environ 에서 읽는다).
export ENV_FILE=/path/to/.env.prod.native
export REDIS_URL="$(grep -m1 '^REDIS_URL=' "$ENV_FILE" | cut -d= -f2-)"

# ① 읽기 전용 준비상태 프로브 (주문 없음)
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle --readiness

# ② 프리뷰 — 파생 + 계획, 디스패치 0
uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables

# ③ acceptance 왕복 1건 (KRX 정규장 안에서만, 기본 안전 mutation 모드)
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables --confirm

# ④ INTERIM_ORDERING — 이중 명시가 있어야만 DAY 주문을 남긴다.
#    §4 envelope 파생 매수 leg 전건만 제출하며, 자동 취소하지 않는다.
#    매도 leg는 기본 ON 매수 전용 게이트가 명시 사유와 함께 차단한다.
B0X_KR_KIWOOM_ENABLED=true uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
    --table-dir ~/services/auto_trader-operator/policy-tables \
    --interim-ordering --confirm
```

exit code: `0` 정상 · `2` writer lock 경합, acceptance 왕복 미완(취소 미확인),
또는 interim 제출 증거 미확인. 🔴 `2` 를 보면 계좌에 미회수 주문이 남았을 수 있다 —
`--readiness` 로 즉시 `pending` 을 확인하고, 남아 있으면 운영자가 직접 취소한다.

## 8. 아티팩트

`<out-dir>/kiwoom_mock/` — `cycles.jsonl`(append-only) ·
`<ts>-cycle.md` · `own-orders.jsonl` · `operator-notices.jsonl`.

모든 아티팩트에 `COEXISTING_ACCOUNT_LANE` 라벨이 붙는다: 이 계좌의 보유·체결
이력 전부를 B0-X 산출로 읽으면 안 된다.

## 9. 알려진 경계 (조용히 넘어가지 않는 것들)

1. **계좌 배타성은 코드가 보장하지 않는다.** flock writer lock 은 B0-X 프로세스
   간 경합만 막는다. 계좌 단위 배타성은 운영 조치이고, 코드는 당일 외부 주문
   흔적 탐지(fail-closed)만 제공한다. TOCTOU 는 완전히 제거되지 않았다 —
   preflight 이후 제출 직전까지의 창이 남는다.
2. **legacy 귀속 게이트는 2026-08-12 실환경에서 *증명되지 않았다*.** 그날
   kiwoom_mock 계좌의 보유가 **0종목**이라 legacy 분기에 도달할 입력이 없었다.
   단위 테스트로만 증명된 상태다(보유가 생기는 첫 사이클이 첫 실환경 검증 기회).
3. **dedup(동일 심볼 재제출 차단)도 실환경 미증명.** `ACCEPTANCE_ONLY` 는 같은
   사이클 안에서 취소하므로 재제출 시점에 자기 미체결이 남아 있지 않다.
   `INTERIM_ORDERING` 은 DAY 주문을 남기므로 kt00007 재조회가 같은 심볼 재제출을
   막아야 한다. 단위 테스트 + preflight 의 `account_has_resting_orders` 사유가 코드
   경로를 덮는다.
4. **kill switch 는 발화 입력이 없다.** `realized_pnl_today` 소스가 이 레인에도
   없으므로 −2.5% NAV kill 은 발화할 수 없다. 구조적 부재이지 측정된 0 이 아니다.
5. **저널 유실 방향은 안전하다** (과소 귀속 → 매도 못 함). 다만 저널이 사라지면
   자기 보유가 legacy 로 재분류되어 영영 팔 수 없게 된다 — 백업 대상이다.
