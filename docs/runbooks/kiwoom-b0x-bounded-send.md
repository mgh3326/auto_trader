# Kiwoom B0-X bounded-send

ROB-1319가 연 KR Kiwoom bounded-send owner 경로의 봉인, 만료, one-shot 상태를
운영하는 런북이다. 이 문서는 봉인을 등록하거나 주문을 집행하지 않는다. 프로덕션
`scripts/b0x/kr/kiwoom_bounded_send_seals.toml`은 별도 운영자 승인 PR 전까지
`registered_seals = []`를 유지하며, 이 경로에는 스케줄러가 없다.

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

## in-process 신뢰 한계

**one-shot 보증은 in-process 코드 신뢰를 전제한다 — 이 전제는 이 PR 이전의 브로커
클라이언트에도 동일하게 적용되던 것이며, 이 경로가 새로 만든 노출은 “모듈 상태 조작만으로
도달 가능해졌다”는 점이다.** 등록·소비된 봉인이 있는 프로세스에서 private module state까지
임의로 조작할 수 있는 코드는 이 보증의 신뢰 경계 밖이다.

검증자가 확인한 역사적 차이도 축소해서 표현하면 안 된다. **이 PR(ROB-1319) 이전에는
non-legacy `grant_only=False`가 무조건 `CONTRACT_MISMATCH`라 어떤 모듈 상태 조작으로도
`authorizes_send=True`에 도달할 수 없었다.** `_grant_only` 플립 하나만으로 현재 경계를
우회할 수 있다는 뜻은 아니며, 빈 registry에서는 여전히 어떤 봉인 인가에도 도달하지 못한다.

## 향후 봉인 등록 PR 체크리스트

봉인 등록은 이 런북과 별도의 운영자 승인 PR이어야 한다.

1. `lane_id`, `physical_account_id`, `expires_at`, `seal_digest` 네 필드만 등록한다.
2. `expires_at`은 집행 당일 KST의 확인된 XKRX 정규장 마감 이하여야 한다.
3. UTC 표기는 canonical `Z` 형식만 쓴다(예: `2026-08-28T06:30:00Z`).
   `+00:00` 형식은 엄격한 canonical 검사에서 거부되며, 해당 행만이 아니라 registry
   전체가 `bounded_send_registry_unavailable`로 닫힌다.
4. digest는 정확한 세 바인딩 필드(`lane_id`, `physical_account_id`, `expires_at`)의
   canonical serialization으로 다시 계산해 대조한다.
5. 등록 전 새 state 디렉터리가 정리·artifact 관리 대상이 아님을 확인하고, 기존 소비
   마커는 절대 제거하지 않는다.

봉인 등록, G3 릴레이, 실제 send, 브로커 호출과 배포는 각각 별도 승인 범위다.
