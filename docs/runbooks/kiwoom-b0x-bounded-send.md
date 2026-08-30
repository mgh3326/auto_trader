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
