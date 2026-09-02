# Kiwoom B0-X bounded-send

ROB-1319가 연 KR Kiwoom bounded-send owner 경로의 봉인, 만료, one-shot 상태를
운영하는 런북이다. ROB-1334는 2026-08-31 KRX 정규장 G3 1회용 봉인 하나를
`scripts/b0x/kr/kiwoom_bounded_send_seals.toml`에 등록한다. 이 런북의 캘린더
워밍업과 봉인 사전검증은 주문·브로커 호출·레저 쓰기를 만들지 않으며, 이 경로에는
스케줄러가 없다. 봉인 등록 PR이 머지되는 순간부터 G3 종결까지 serving 배포는
동결한다.

## 안전 경계

봉인은 caller가 만든 digest만으로 권한이 되지 않는다. registry의 정확한 4필드 행과
일치해야 하며, registry 전체는 알 수 없는 키, 중복 digest, 잘못된 schema/형식 또는
유효창 위반이 하나라도 있으면 `bounded_send_registry_unavailable`로 fail-closed한다.

유효창은 registry를 로드하는 시점의 KST 날짜와 같은 날이어야 하고, 레포의 XKRX
캘린더가 반환한 그날 정규장 마감 이하여야 한다. 판정은
`app/services/market_events/session_calendar.py::regular_session_bounds`를 재사용하므로
주말, 휴일, 캘린더 범위 밖 또는 캘린더 오류에는 봉인이 성립하지 않는다. 마감 정각은
registry 상한으로 허용하지만 런타임 freshness는 `now < expires_at`만 허용하므로
`expires_at` 정각부터는 닫힌다.

owner 구성 전 소비 마커는 `O_EXCL`, mode `0600`, 파일·디렉터리 `fsync`, exact
readback을 모두 통과해야 한다. 기록 실패나 불확실성은 인가가 아니라 거부다.

### 캘린더 가용성 위험 (NICE-4)

**캘린더 실패는 봉인 불성립(거부)이다.** fail-closed이므로 안전 방향은 맞지만,
라이브러리 오류, 지원 범위 밖 날짜 또는 콜드 동시 초기화 실패로 정당한 G3 봉인이
거부될 수 있다.

bounded-send owner 경로에서 캘린더 최초 접촉은 봉인 검증 시점이다.
`scripts/b0x/kr/kiwoom_coordination.py:425-439`의 factory가
`scripts/b0x/kr/kiwoom_bounded_send.py:255`의 `regular_session_bounds`에 도달하며,
이는 `scripts/b0x/kr/kiwoom_cycle.py:2361`의 `is_krx_regular_session`보다 앞선다.
ROB-1325 동적 귀속 검증에서는 해당 봉인 검증이 캘린더의 최초 접촉이고
`kiwoom_cycle.py:2361`은 5번째 접촉임을 확인했다. 현행 기본 grant-only 경로도
confirm 시 `kiwoom_cycle.py:2330-2335`에서 조기 반환하므로 2361행에 도달하지 않는다.

### 게이트 ⑦-0 — 집행 진입점 실행가능성 실측

G3 발주와 운영 봉인 등록 전에, 계좌맵 게이트와 같은 지위로 **실제 serving SHA에서
집행할 정확한 진입점이 bounded-send owner factory와 검토된 durable ports factory를
함께 선택할 수 있음**을 먼저 실측한다. 소스에 factory가 존재하거나 CLI `--help`에
플래그가 보이는 것만으로는 PASS가 아니다.

필수 증거는 다음을 모두 포함한다.

1. exact serving SHA와 최종 실행 명령을 고정한다. 명령은
   `uv run python -m scripts.run_b0x_kr_kiwoom_cycle --bounded-send --seal <JSON_PATH>
   --durable-ports-factory <MODULE:CALLABLE> --ordering --confirm` 형태여야 한다.
2. 같은 SHA의 격리 테스트에서 `--bounded-send` 지정 시
   `build_bounded_send_kiwoom_coordination_factory`가 실제 cycle의
   `coordination_factory`로 전달되고, 플래그 미지정 시 기존
   `production_kiwoom_coordination_factory()` grant-only 경로가 그대로 선택됨을 함께
   증명한다. 정본 회귀 테스트는
   `test_cli_bounded_send_reaches_registered_seal_factory`와
   `test_cli_default_still_injects_production_grant_only_factory`다.
3. 운영 명령에 지정할 `<MODULE:CALLABLE>`이 serving 환경에서 import 가능하고, 검토된
   실제 durable `KiwoomCoordinationPorts`를 반환한다는 별도 배포 증거를 남긴다.
   unit-test sentinel이나 `make_grant_only_kiwoom_coordination_adapter(...).ports`는 이
   증거가 아니며, 후자는 명시적으로 G3 포트가 아니다.
4. 이 사전 실측은 브로커 호출 0, 주문 0, 운영 봉인 소비 0이어야 한다. 실제 bounded
   factory 내부 callable 호출은 봉인을 소비하므로, 운영 봉인으로 사전검증한다는 이유로
   호출하지 않는다. one-shot 소비는 계속 bounded factory 내부에서만 일어난다.

위 네 항목 중 하나라도 없으면 `GATE_7_0=FAIL`로 기록하고 ⑦, 운영자 승인, 봉인 등록,
집행으로 진행하지 않는다. 특히 이 레포가 CLI 배선 테스트를 통과했다는 사실만으로 운영
durable ports의 존재나 배포 결속을 추론하지 않는다. `<MODULE:CALLABLE>`은 동일 Python
프로세스에서 실행되는 신뢰 코드이므로 exact module/symbol과 배포 artifact hash를 증거에
고정한다.

### 운영 durable ports factory (ROB-1338)

검토 대상 callable은 다음 exact 문자열이다.

```text
scripts.b0x.kr.kiwoom_durable_ports:build_ports
```

`build_ports(entry)`는 exact `KiwoomCoordinationPorts`를 반환하며, 호출 자체는 DB 연결,
브로커 소켓, 주문 또는 봉인 소비를 만들지 않는다. 실제 I/O는 bounded factory가 별도로
봉인을 소비해 owner를 구성한 뒤 coordination operation 안에서만 시작된다. 기본
grant-only factory는 이 모듈을 선택하지 않으며 기존 in-memory canary 그대로다.
이 factory는 signed lane registry의 policy/cap/owner/canary/activation 값을 만들거나
완화하지 않는다. 따라서 해당 row가 `NOT_READY`/missing binding이면 generic coordination은
여전히 broker callback 전에 fail-closed한다. factory 존재 증거를 activation 증거로
대체하지 않는다.

저장면은 두 부분이다. 기존 `review.order_send_intents`가 broker I/O 전에 binary claim을
별도 트랜잭션으로 커밋하고, `review.kiwoom_coordination_lifecycle`가 immutable pre-send
lineage와 post-dispatch evidence를 보존한다. dispatch writer는 ACK가 붙은 envelope와 typed
dispatch evidence를 한 PostgreSQL 트랜잭션으로 쓴다. 프로세스 재시작 뒤 새 ports의
`claims.rediscover_unreleased_claims()`가 살아 있는 claim을 lifecycle row와 left join하고,
모든 실제 coordination 진입도 uncertainty gate에서 같은 재발견을 수행한다. evidence가
없거나 uncertain이면 account mutation을 fail-closed한다.

🔴 **알려진 원자성 한계:** Kiwoom HTTP ACK와 PostgreSQL commit을 하나의 분산
트랜잭션으로 만들 수는 없다. ACK 뒤 모든 evidence write가 실패하는 창은 존재한다. 이때
ACK를 기록했다고 가장하지 않으며, 먼저 커밋된 claim이 남아 재전송을 막고 restart
rediscovery가 `evidence_missing`으로 복구한다. 해소는 `kt00007` authoritative readback과
terminal evidence에 따른 exact claim release가 필요하다. claim 부재를 주문 부재로
해석하거나 자동 재주문하지 않는다.

### G3 필수 순서 — ① 같은 프로세스 1회 워밍업 + ③ 실패 폴백

G3 집행 프로세스는 **봉인 검증 또는 bounded-send owner factory 진입 직전에**, 같은
Python 프로세스에서 아래 블록을 정확히 1회 실행한다. 별도 shell/프로세스에서 실행한
결과는 G3 프로세스의 캘린더 캐시를 워밍하지 않으므로 이 절차를 충족하지 않는다.

```python
import datetime as dt

from app.services.market_events.session_calendar import regular_session_bounds

bounds = regular_session_bounds("kr", dt.date(2026, 8, 31))
if bounds is None:
    raise SystemExit("registry_unavailable")
session_open, session_close = bounds
print(
    "calendar_warmup=READY "
    f"session_open={session_open.astimezone(dt.UTC).isoformat().replace('+00:00', 'Z')} "
    f"session_close={session_close.astimezone(dt.UTC).isoformat().replace('+00:00', 'Z')}"
)
```

이 블록은 로컬 캘린더를 읽고 메모이즈된 calendar 객체를 준비할 뿐이다. 봉인을
로드·검증·소비하지 않고, 파일/DB/레저 mutation이나 계좌·브로커·주문 호출도 만들지
않는다. 성공 출력 직후 같은 프로세스에서 봉인 검증과 단 한 번의 bounded-send owner
구성으로 진행한다.

워밍업이 `registry_unavailable`로 끝나거나 이어지는 봉인 검증이
`bounded_send_registry_unavailable`로 닫히면 **재시도하지 않는다.** 2026-08-31 G3
집행 실패로 보고하고, 해당 봉인으로 더 진행하지 않은 채 새 운영자 승인과 새 봉인
등록 절차로 돌아간다.

2026-08-30에 실행 순서를 다시 확인한 명령과 원문은 다음과 같다.

```text
$ sed -n '2318,2325p;2359,2362p' /Users/mgh3326/work/auto_trader.rob1325-seal-precond/scripts/b0x/kr/kiwoom_cycle.py
        )

        coordination, coordination_record = _resolve_coordination_owner(
            coordination_factory=coordination_factory,
            expected_entry=coordination_entry,
        )
        record["coordination"] = coordination_record


        # --- RTH gate: cheapest check, before any table/account I/O ---
        in_session = is_krx_regular_session(now)
        record["krx_regular_session"] = in_session
```

## 내구 one-shot 상태 — 정리 금지

소비 마커의 현재 위치는 다음과 같다.

```text
~/.local/state/auto-trader/b0x-kr-kiwoom-bounded-send/<seal_digest>.json
```

과거 위치였던
`~/work/herdr-artifacts/b0x/kr.kiwoom.mock/bounded-send-consumed/`는 b0x 관측
산출물 트리이므로 더 이상 사용하지 않는다. 새 state 디렉터리를 처음 사용할 때
`README.md`도 생성된다.

이 디렉터리는 로그, 캐시, b0x 산출물 또는 정리 대상이 아니다. **마커 파일을 지우면
프로세스 재시작 뒤 해당 봉인의 one-shot 인가가 해제된다.** 파일 하나를 삭제하는 것만으로
`expires_at` 창 안에서 봉인이 재사용될 수 있으므로 마커를 삭제·이동·편집하거나 디렉터리를
청소하지 않는다. 마커가 손상되었거나 읽을 수 없으면 복구 목적으로 고치거나 지우지 말고,
기존 봉인이 만료되게 둔 뒤 별도 운영자 승인을 받은 새 봉인을 사용한다.

## G3 종결 verdict와 배포 동결

### `AUTHORITY_CESSATION` / `RELEASE_VERIFIED`

`AUTHORITY_CESSATION` 증거로 인정하는 것은 **advisory unlock 성공** 또는 **backend
termination receipt**뿐이다. 단순히 lock holder가 보이지 않는다는 관측으로
`RELEASE_VERIFIED`를 선언하지 않는다. 둘 중 어느 증거도 확보하지 못하면
`G3_OVERALL = INCOMPLETE_EVIDENCE`다.

ROB-1340부터 정본 열거원은 첫 `pg_try_advisory_lock` 전에 commit+readback한
`review.kiwoom_authority_attempts`의 **현재 cycle** 행이다. `E`는 그 start 중 key를
하나라도 획득했거나 응답이 in-flight unknown인 attempt 전체다. 다음을 모두 만족할 때만
`RELEASE_VERIFIED`다.

1. `E`가 비어 있지 않다.
2. 모든 current-cycle start가 정확히 한 terminal을 가지며 enumeration이 완전하다.
3. `E`와 committed qualifying receipt의 `authority_attempt_id` 집합이 정확히 같다.
4. unlock receipt는 모든 acquired key의 exact-true와 같은 backend의 사후 matching row
   0을, termination receipt는 exact owner binding·`pg_terminate_backend=true`·독립
   observer의 PID 부재를 모두 증명한다.
5. `unreleased_authority_holds_for_cycle(cycle_id)`가 비어 있다.

빈 `pg_locks`, pool close, 프로세스 종료, claim 0, 마지막 성공 receipt 또는 cycle JSON은
양성 증거가 아니다. release 자체가 증명된 뒤 receipt commit만 실패하면 주문 결과를
소급 실패/재시도하지 않고 `G3_AUTHORITY_RELEASE=INCOMPLETE_EVIDENCE`로 남긴다.

### current-cycle 범위와 과거 crash orphan 복구 (S9)

`enumeration_complete`의 범위는 명시적으로 **현재 cycle_id 하나**다. 과거 cycle의 crash
orphan은 전역 운영자 hold 목록에는 계속 보이지만, 새 cycle을 영구 INCOMPLETE로 만들지는
않는다. 그렇다고 과거 cycle을 승격하거나 orphan을 삭제하는 것도 아니다.

복구 절차는 해당 과거 cycle을 그대로 고정하고 surviving durable claim과
`authority_attempt_id`를 조회한 뒤 `kt00007`로 exact broker order를 재확인한다. terminal
broker evidence와 account/position reconciliation이 완전할 때만 기존
`release_with_terminal_evidence`를 사용한다. authority receipt가 끝내 없으면 그 **과거
cycle은 INCOMPLETE_EVIDENCE로 유지**하고, 운영자 승인 후 별도 새 cycle을 시작한다. DB
행이나 process hold를 수정·삭제해서 새 cycle에 합치지 않는다.

### `MANDATORY_CANCEL_BLOCKED_BY_AUTHORITY`

ACCEPTANCE 매수와 취소는 하나의 coordinator scope다. cancel 직전
`scope.assert_owned()`가 실패하면 계약 불변식 때문에 취소를 보내지 않는다. 대신 그
시점에 `cycles.jsonl`에 `live_order_risk`(order id, symbol, remaining quantity, side,
timestamp)를 먼저 append하고, 그 write 뒤 기존 `get_trade_notifier().notify_agent_message`
경로로 Telegram 운영 알림을 시도한다. 알림 실패는 이미 기록된 flag를 되돌리지 않는다.
설정은 기존 `TELEGRAM_TOKEN`과 `TELEGRAM_CHAT_IDS_STR`(단일 대상 fallback은
`TELEGRAM_CHAT_ID`) 키를 사용하며 값은 로그나 산출물에 남기지 않는다.

남은 주문은 새 retry/send 표면이 아니라 기존 recovery contract가 흡수한다. composite
dispatch는 BUY order id와 `UNCERTAIN` claim을 durable하게 남긴다. recovery owner가 그
claim을 재발견하고 `kt00007` exact readback을 수행하며, terminal evidence가 완전할 때만
`release_with_terminal_evidence`로 claim을 해제한다. open/unknown이면 계속 account를
막고 자동 재주문하지 않는다. DAY 마감 소멸은 하한 안전망일 뿐 claim 해제 증거가 아니다.

### BUY ACK→cancel 종료 예외의 침묵 금지

유효한 BUY `ord_no`를 추출한 순간부터 사후 `kt00007`로 cancel 종료를 판정할 때까지의
예외는 `POST_ACK_CANCEL_WINDOW_EXCEPTION` live-order-risk로 같은 `cycles.jsonl` 경로에
먼저 append하고, 그 뒤 위와 같은 기존 Telegram notifier를 호출한다. 저장하는 예외 정보는
닫힌 `exception_stage`와 예외 **type**뿐이며 메시지·URL·vendor payload는 저장하거나
알리지 않는다. 단계 어휘는 다음 11개다.

1. `buy_ack_journal`
2. `pre_cancel_resting_read`
3. `pre_cancel_resting_parse`
4. `cancel_authority_check`
5. `cancel_transport_guard`
6. `cancel_request`
7. `cancel_ack_parse`
8. `cancel_ack_journal`
9. `post_cancel_reconcile_read`
10. `post_cancel_reconcile_parse`
11. `post_cancel_terminal_classification`

이 관측은 기존 주문 상태 판정을 바꾸지 않는다. 기존에 계속하던 journal/read/cancel API
예외는 계속하고, 기존에 실패/반환하던 경로는 그대로 실패/반환한다. 알림의 일반 예외도
이미 append된 flag를 되돌리거나 취소 시도 여부를 바꾸지 않는다. `CancelledError`,
`KeyboardInterrupt`, `SystemExit`를 포함한 `BaseException`은 잠깐 관측 경계에서 잡되,
flag·알림 시도 뒤 **원본을 그대로 재발생**시킨다. 즉 이 경계는 프로세스 제어 예외를
삼키는 recovery 경계가 아니다.

### migration·권한 preflight와 신뢰 한계 (S11)

상류 운영자는 배포 전에 additive migration을 적용한다. worker/검증 세션은 운영 DB에서
`alembic upgrade head`를 실행하지 않는다. confirmed ACCEPTANCE는 broker send 전에 fresh
read-only catalog projection으로 두 table, 필수 column, named CHECK/UNIQUE,
`rob1340.v1`, effective `SELECT`/`INSERT` 허용과 `UPDATE`/`DELETE`/`TRUNCATE` 부재를
검증한다. probe INSERT는 receipt를 위조하므로 금지한다.
이 privilege projection을 만족하는 운영 role 실사가 끝나기 전에는 confirmed
ACCEPTANCE를 활성화하지 않는다.

DB trigger와 REVOKE는 update/delete/truncate를 막지만, **같은 application role을 획득한
임의 코드의 INSERT 위조까지 막지는 못한다.** producer와 reader의 운영 role 분리는 이번
범위 밖이며 상류 후속 결정 사항이다. 따라서 이 receipt는 동일 role 자체가 침해되지
않았다는 운영 신뢰 경계를 전제로 한다.

### 게이트 ⑬ — 추가 주문 0 + 동일 봉인 재사용 불가

게이트 ⑬은 두 조건을 모두 요구한다. G3의 허용된 1회 이후 broker evidence에서 추가
주문이 0건이어야 하고, 동일 `seal_digest`가 두 번째 owner를 인가하지 못한다는 증거가
있어야 한다. 후자는 canonical state root의 exact 소비 마커와 동일 봉인 재사용 거부
결과를 함께 보존한다. 추가 주문 0건만으로 one-shot 재사용 불가를 대신 증명할 수 없다.

### serving 배포 동결

봉인 등록 PR 머지 시점부터 G3 종결 verdict가 확정될 때까지 serving 배포는 정확히
0건이어야 한다. 해당 구간의 배포 이력을 G3 evidence packet에 포함하고, 구간 안에
serving 배포가 하나라도 있으면 동결 수용조건을 충족했다고 판정하지 않는다.

## in-process 신뢰 한계

**one-shot 보증은 in-process 코드 신뢰를 전제한다 — 이 전제는 이 PR 이전의 브로커
클라이언트에도 동일하게 적용되던 것이며, 이 경로가 새로 만든 노출은 “모듈 상태 조작만으로
도달 가능해졌다”는 점이다.** 등록·소비된 봉인이 있는 프로세스에서 private module state까지
임의로 조작할 수 있는 코드는 이 보증의 신뢰 경계 밖이다.

검증자가 확인한 역사적 차이도 축소해서 표현하면 안 된다. **이 PR(ROB-1319) 이전에는
non-legacy `grant_only=False`가 무조건 `CONTRACT_MISMATCH`라 어떤 모듈 상태 조작으로도
`authorizes_send=True`에 도달할 수 없었다.** `_grant_only` 플립 하나만으로 현재 경계를
우회할 수 있다는 뜻은 아니며, 빈 registry에서는 여전히 어떤 봉인 인가에도 도달하지 못한다.
단, 봉인이 등록된 이후에는 이 문장이 성립하지 않는다.

## 봉인 등록 PR 체크리스트

봉인 등록은 운영자 승인 PR이어야 하며, 머지와 G3 집행은 상류 운영자가 수행한다.

1. `lane_id`, `physical_account_id`, `expires_at`, `seal_digest` 네 필드만 등록한다.
2. `expires_at`은 집행 당일 KST의 확인된 XKRX 정규장 마감 이하여야 한다.
3. UTC 표기는 canonical `Z` 형식만 쓴다(예: `2026-08-28T06:30:00Z`).
   `+00:00` 형식은 엄격한 canonical 검사에서 거부되며, 해당 행만이 아니라 registry
   전체가 `bounded_send_registry_unavailable`로 닫힌다.
4. digest는 정확한 세 바인딩 필드(`lane_id`, `physical_account_id`, `expires_at`)의
   canonical serialization으로 다시 계산해 대조한다.
5. 등록 전 새 state 디렉터리가 정리·artifact 관리 대상이 아님을 확인하고, 기존 소비
   마커는 절대 제거하지 않는다.

봉인 등록 PR 머지, G3 릴레이, 실제 send, 브로커 호출과 후속 배포는 각각 별도 운영
범위다.
