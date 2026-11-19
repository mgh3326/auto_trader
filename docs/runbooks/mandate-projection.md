# 위임장 체결 투영 CLI (hk 3795 T4b)

`scripts/mandate_projection.py`는 원장의 authoritative 체결과 계획 결속 제안의 rung 상태를 읽어, 증거가 입증하는 계획 이벤트만 `system:mandate_projection`으로 추가한다. 설계 정본은 `docs/design/3795-mandate.md`의 §3.2 "회차 기준값"·§3.7·§8 T4b다. 스케줄러 연결이 없고, 주문·브로커 모듈을 import하지 않으며, 원장과 주문 표에는 쓰지 않는다(하드룰 5·6).

## 공통 규칙

- **데이터베이스는 명시한다.** `--database-url-env NAME`(권장) 또는 `--database-url URL`. URL 값은 출력하지 않는다.
- **기본은 미리보기다.** 같은 판정과 같은 저장소 검사를 돌린 뒤 롤백한다. 이벤트는 `--commit`일 때만 쓰고, 계획마다 따로 커밋한다. 한 계획이 거부돼도 다른 계획은 계속 돈다(종료 코드 1).
- **대상**: `--plan-id N`(반복 가능) 또는 `--sleeve-key KEY`(그 sleeve의 `released`되지 않은 계획 전부). 둘 중 하나만 쓴다.
- **보유는 입력이다.** 이 명령은 브로커를 읽지 않는다. `--holdings FILE`은 운영자가 브로커 조회로 만든 `{"readings": [{"account_ref", "symbol", "quantity", "checked_at"}]}` JSON이다(닫힌 키, `quantity`는 10진 문자열, `checked_at`은 시간대 포함 ISO 8601).
- **출력은 JSON 한 줄**이다. 계획마다 `events`(쓸 이벤트)와 `holds`(쓰지 못한 이유, 닫힌 코드)가 나온다.

## 무엇을 쓰는가

| 이벤트 | 조건 |
|---|---|
| `tranche_filled` / `tranche_partially_filled` / `tranche_unfilled` | 제안의 모든 rung이 종결이고, authoritative 체결이 rung의 보고 수량을 덮는다. 누계가 회차 금액의 95% 이상이거나 체결이 있고 잔여가 1주 값(제안 지정가) 미만이면 `filled`, 체결 0이면 `unfilled`, 그 외 `partially_filled` |
| `first_tranche_settled` / `exhausted` | 1차 회차 완료 / 3차 회차 완료 |
| `closed` | `thesis_broken` 뒤 모든 매수 주문이 종결이고 **계획 계좌**의 보유 조회가 0 |
| `orders_settled` | 계획에 결속된 모든 제안의 rung이 종결, 불명 0, 닫히지 않은 fence 0, 기록된 체결이 원장과 일치 |
| `holdings_disposed` | 한 번도 승인되지 않은 계획은 `never_adopted`. 그 외는 `scope_account_refs`의 **모든** 계좌 조회가 0인 `zero_holdings`. `kept_outside_basket`은 이 명령이 쓰지 않는다(운영자 CLI) |
| `released` | 위 둘이 끝나면 같은 실행에서 쓴다. 종목 예약 해제 행은 DB가 같은 트랜잭션에서 만든다 |

`exhausted` 계획은 전 계좌 0 조회가 있을 때만 정리를 시작한다. `thesis_broken` 계획은 `closed`나 `retired` 없이는 정리하지 않는다.

## 쓰지 않는 경우(`holds`)

`rung_open`, `outcome_unknown`(`unverified` 등), `fill_not_booked`(rung은 체결을 보고하지만 authoritative 원장 행이 없다 — websocket 단독 행은 증거가 아니다), `fill_attribution_unknown`, `fence_unclosed`(fence가 커밋됐는데 broker 주문번호가 없는 제안은 미전송 증거가 아니다), `bound_proposal_not_in_stream`, `recorded_fill_mismatch`(종결 이벤트 뒤 늦게 들어온 체결), `scope_account_unread`, `holdings_stale`(마지막 체결보다 오래됐거나 `--max-holdings-age-hours` 초과), `holdings_remain`, `holdings_unknown`, `event_refused`. 이유가 풀릴 때까지 이벤트는 쓰지 않는다.

## 1. 미리보기

```bash
uv run python -m scripts.mandate_projection --database-url-env MANDATE_DB_URL --plan-id 41
```

```bash
uv run python -m scripts.mandate_projection --database-url-env MANDATE_DB_URL --sleeve-key mock-kr-sleeve --holdings /secure/holdings-2026-11-27.json
```

## 2. 쓰기

미리보기의 `events`를 확인한 뒤 같은 명령에 `--commit`을 붙인다. 계획마다 커밋하므로 한 번에 여러 계획을 지정해도 된다.

```bash
uv run python -m scripts.mandate_projection --database-url-env MANDATE_DB_URL --plan-id 41 --plan-id 42 --holdings /secure/holdings-2026-11-27.json --max-holdings-age-hours 3 --commit
```

## 하지 않는 것

- 원장(`execution_ledger` 등)과 주문 표에 쓰지 않는다. 체결은 원장 대사(기존 경로)가 먼저 기록하고, 이 명령은 그 결과를 읽는다.
- 주문 제안을 만들거나 보내지 않고, 미결 주문을 취소하지 않고, 보유를 팔지 않는다(§3.7).
- 보유를 브로커에서 읽지 않는다. 오래됐거나 범위 계좌가 빠진 조회는 증거가 아니다.
- 스케줄러·cron·TaskIQ에 등록하지 않는다(하드룰 6).

## 확인

```bash
uv run pytest tests/services/mandate/test_projection_rules.py tests/services/mandate/test_projection_cli.py -v
```

DB 왕복 테스트(`tests/services/mandate/test_projection_db.py`)는 CI의 실행 소유 테스트 데이터베이스에서만 돈다. 로컬 DB에 접속하지 않는다.
