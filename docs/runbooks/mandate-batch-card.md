# 위임장 아침 묶음 승인 카드 (hk 3795 T3a)

`scripts/mandate_batch.py`는 하루 1회 장 시작 전에 운영자가 손으로 돌리는 CLI다. 바구니의 승인 대기 계획을 한 배치로 묶어 Telegram 카드로 보내고, 응답은 새 콜백 `mpb`가 받아 계획 이벤트 하나로 기록한다. 스케줄러 연결이 없다(Q8 = A): **발송하지 않은 날은 승인도 없다.** 주문·브로커 모듈을 import하지 않고, 기존 주문 승인 콜백과 W5 inbox는 바뀌지 않았다. 설계 정본은 `docs/design/3795-mandate.md`의 §3.5·§5.1·§8 T3a다.

## 먼저 설정할 것

- **`MANDATE_APPROVER_TELEGRAM_USER_IDS`** — 위임장 카드에 응답할 수 있는 Telegram 사용자 id를 쉼표로 적는다(정수 십진수만). **기본은 빈 값이고 빈 값이면 아무도 승인할 수 없다.** 항목 하나라도 형식이 틀리면 목록 전체가 빈 값으로 취급된다(fail-closed). 이 값은 env 변경이므로 배포 때 내용 기준 승인을 받는다.
- 카드는 승인 allowlist(`ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR`)의 첫 chat으로 보낸다. **봇은 기존 승인 메시지와 같은 `TELEGRAM_TOKEN`(TradeNotifier가 쓰는 BotFather 토큰)이고, 웹훅을 등록한 봇이어야 한다.** 카드를 보내는 봇과 버튼 응답·메시지 수정을 하는 봇이 같아야 하며, 이 CLI(`scripts/mandate_batch.py`)는 `ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN`을 읽지 않는다(조항·프로필 카드를 발송하는 T3b `scripts/mandate.py`는 이 변수를 읽으므로 두 종류의 카드를 함께 쓰는 배포에서는 지우지 않는다). 콜백은 기존 webhook(`/trading/api/telegram/callback`) 하나로 들어오며 `ORDER_PROPOSALS_TELEGRAM_ENABLED`가 꺼져 있으면 받지 않는다.
- 마이그레이션 `20261120_hk5648_mandate_batches`가 적용돼 있어야 한다(운영자 적용).

## 공통 규칙

- **데이터베이스는 명시한다.** `--database-url-env NAME`(권장) 또는 `--database-url URL`. URL 값은 출력하지 않는다.
- **actor는 `operator:<이름>`이어야 한다.** 아니면 DB에 닿기 전에 거부한다.
- **`--commit`이 없으면 쓰지도 보내지도 않는다.** 같은 저장소 검사를 돌린 뒤 롤백하므로 미리보기에서 거부된 것은 커밋해도 거부된다.
- **`--commit`이면 행을 먼저 커밋하고 그 뒤에 보낸다.** 카드에 실린 nonce는 출력에 나오지 않는다(카드 버튼 안에만 있고 DB에는 digest만 있다). 보내기가 실패하면 행은 남고 종료 코드 2가 나온다: `resend`로 새 카드를 보낸다.
- 종료 코드: 0 정상, 1 입력·거부·DB 오류, 2 커밋됐지만 발송 실패.

## 1. 배치 만들기와 발송 — `create`

바구니(영역 `live`/`mock` + `basket_key`)의 다음 거래일 배치를 만든다. 대상 거래일 T는 지금 이후 처음 개장하는 정규장 세션이고, 정규장 중에 만들면 다음 거래일이다.

- **승인 마감** = T의 정규장 개장. **배치 만료** = T의 정규장 종료. 만료 뒤에는 응답을 받지 않는다.
- 카드 대상: (a) `proposed` 계획(신규), (b) 위임장 버전이 바뀌어 현재 결속 조항이 live 조항과 다른 `approved`/`active` 계획(재승인). 계좌 참조가 live 조항과 다른 계획은 카드를 만들지 않고 `skipped`로 출력한다.
- 배치의 첫 메시지는 "오늘 적용 기준" 요약이다(승인 대상 카드 수, 오늘 armed 회차). armed 회차 목록이 길면 여러 메시지로 나뉜다. 모든 메시지에는 발급 식별자(영역·배치·카드 번호)가 붙고, 논지가 길어 카드가 여러 메시지로 나뉘면 앞의 맥락 메시지와 버튼이 달린 카드가 같은 식별자를 갖는다. 승인할 것이 없어도 배치는 만들어져 그 거래일을 차지한다.
- **배치 단일성(I22)**: 바구니 × 대상 거래일에 배치는 하나다. 그 바구니의 앞 배치가 열려 있으면(만료 전이고 응답 없는 계획이 있음) 새 배치는 거부된다(`mandate_batch_open`). 배치를 만든 뒤 등록한 계획은 그 배치에 들어가지 않고 다음 배치에 들어간다.

```bash
uv run python -m scripts.mandate_batch --database-url-env MANDATE_DB_URL create --realm mock --basket-key basket-1 --actor operator:cli:mgh
```

미리보기가 통과하면 같은 명령에 `--commit`을 붙인다.

## 2. 카드에 응답하기

카드의 버튼은 둘이다: 승인, 기각. 응답은 계획 이벤트 하나가 된다.

| 카드 종류 | 승인 | 기각 |
|---|---|---|
| 신규 계획(`proposed`) | `approved` | `rejected` |
| 재승인 | `terms_rebound` | `retired`(추가 권한만 멈춘다. 미결 주문·보유·예약은 그대로, 카드에 미결 건수를 보여 준다) |

효력 세션은 응답 시각이 정한다.

| 응답 시각 | 효력 |
|---|---|
| 승인 마감 전(T 개장 전) | T |
| 승인 마감 뒤 ~ 만료 전(T 장중) | T 다음 거래일 |
| 만료 뒤 | 효력 없음. 이벤트를 쓰지 않고 다음 배치에 다시 들어간다 |

콜백이 받는 조건(전부 참일 때만 이벤트를 쓴다):

1. 허용된 chat이고 버튼 데이터가 정확히 `mpb:<a|r>:<카드 id>:<nonce>`다.
2. 발신자가 `MANDATE_APPROVER_TELEGRAM_USER_IDS`에 있다. **목록에 없으면 아무 행도 쓰지 않고 nonce도 소비하지 않는다**(카드는 운영자가 누를 수 있는 채로 남는다).
3. nonce가 그 카드의 digest와 같고, 그 카드가 계획의 가장 새 카드이고, 배치가 만료 전이고, 이 배치에서 이 계획에 아직 응답이 없다. 응답 이벤트 자체가 nonce를 소비한다: 같은 버튼을 다시 눌러도 아무 행도 쓰이지 않는다.
4. 계획·조항 해시가 카드 스냅샷과 같고 그 조항이 아직 sleeve의 live 조항이다(낡았으면 무기록, 다음 배치에서 다시 요청).

## 3. 재발송 — `resend`

같은 배치에서 응답 없는 계획마다 다음 카드를 새 nonce로 보낸다. 새 배치가 아니고 스냅샷도 그대로이며, 앞 카드의 버튼은 더 이상 받지 않는다. 만료된 배치는 재발송할 수 없다.

```bash
uv run python -m scripts.mandate_batch --database-url-env MANDATE_DB_URL resend --batch-id 9 --actor operator:cli:mgh
```

## 4. 상태 조회 — `status`

읽기 전용이다. 배치 시각, 계획별 종류·가장 새 카드 번호·응답 이벤트를 보여 준다.

```bash
uv run python -m scripts.mandate_batch --database-url-env MANDATE_DB_URL status --batch-id 9
```

## 하지 않는 것

- 주문·제안 생성이나 전송, 자동승인 완화, 스케줄러·cron 등록은 없다.
- 조항·활성화 프로필 승인(`mta`)은 T3b다. 이 카드는 계획 승인만 다룬다.
- 만료된 응답 없는 계획에는 이벤트를 쓰지 않는다. `proposed`로 남거나 재승인 대기(`mandate_terms_stale`)로 남아 다음 배치에 들어간다.
