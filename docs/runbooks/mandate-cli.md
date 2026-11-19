# 위임장·계획 운영 CLI (hk 3795 T2)

`scripts/mandate.py`는 위임장 조항·활성화 프로필의 승인 요청, 분할 매수 계획 등록, 운영자 계획 이벤트, 상태 조회를 담는 수동 CLI다. 스케줄러 연결이 없고, 주문·브로커 모듈을 import하지 않는다. 설계 정본은 `docs/design/3795-mandate.md`의 §3.2·§3.5·§5.2·§8 T2다.

## 공통 규칙

- **데이터베이스는 명시한다.** `--database-url-env NAME`(권장, 환경 변수 이름만 쓴다) 또는 `--database-url URL`. URL 값은 출력하지 않는다.
- **쓰기는 `--commit`일 때만 일어난다.** 없으면 같은 저장소 검사를 돌린 뒤 롤백한다. 미리보기에서 거부된 것은 커밋해도 거부된다. `status`는 읽기 전용이다.
- **actor는 `operator:<이름>`이어야 한다.** 예: `operator:cli:mgh`. `system:`으로 시작하거나 다른 값이면 DB에 닿기 전에 거부한다(`mandate_invalid_fields`가 아니라 CLI 오류, 종료 코드 1).
- **브로커를 읽지 않는다.** 편입 보유 증거(`adopted_evidence`)와 계좌 참조(`account_ref`, `scope_account_refs`)는 운영자가 입력 파일에 적는다. 저장소는 모양과 일치만 검사한다. 1주 가격만 `daily_candles`의 마지막 완결 일봉 종가에서 읽는다.
- **입력 파일은 닫힌 키의 JSON 객체다.** 모르는 키와 빠진 키는 거부한다. 금액은 10진 문자열로 쓴다(JSON 실수는 문자열로 받는다).
- **출력은 JSON 한 줄**이다. 오류는 stderr에 `{"error": ...}`로 나오며 종료 코드는 1이다.

## 1. 조항 승인 요청 — `request-terms`

sleeve의 다음 live 버전과 조항을 승인 카드용 스냅샷으로 만든다. 승인 자체는 Telegram 카드 콜백(T3b)에서만 기록되며, 이 명령은 승인을 기록하지 않는다.

입력 파일의 닫힌 키: `basket_key`, `account_ref`, `universe_rule`, `allowed_symbols`, `entry_max_premium_bps`, `per_order_cap`, `daily_cap`, `sector_cap_pct`, `basket_drawdown_freeze_pct`, `account_drawdown_freeze_pct`, `tranche_rule`(`weights`·`triggers_pct_from_first`·`min_gap_sessions`), `expires_at`(시간대 포함 ISO 8601), `budget_cap`, `loss_limit`, `prereg`(`metric`·`min_effect_pct`·`horizon_sessions`·`max_duration_days` 넷 전부), 선택 `loss_sell_mode`.

`per_order_cap`과 `daily_cap`은 입력이 정책 전역 상한을 넘으면 거부된다(상한 값은 `config/trading_policy.yaml`의 `order_proposals.auto_approve`에서 읽는다).

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL request-terms --sleeve-key mock-kr-sleeve --input /secure/clause-2026-11.json --approval-ref hk:5638 --actor operator:cli:mgh
```

미리보기가 통과하면 같은 명령에 `--commit`을 붙인다.

## 2. 활성화 프로필 승인 요청 — `request-profile`

회차 상한(1~3)과 효력 세션, 만료를 담은 프로필을 요청한다. 현재 live 조항의 `terms_id`에 묶인다. 회차 확대는 새 프로필 버전이다.

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL request-profile --sleeve-key mock-kr-sleeve --terms-id 12 --max-tranche-index 1 --effective-session 2026-11-23 --expires-at 2027-06-30T00:00:00+09:00 --approval-ref hk:5638 --actor operator:cli:mgh
```

## 3. 계획 등록 — `register-plan`

종목 하나의 분할 매수 계획을 `proposed`로 등록한다. 등록 시 종목·바구니·sleeve 예약을 잡고 I1(영역 안 종목당 미해제 계획 1개)·I2·I3·I4·I15·I17과 열린 `loss_cut` 거부를 검사한다.

입력 파일의 닫힌 키: `sleeve_key`, `symbol`, `planned_amount`, `adopted_amount`, `adopted_evidence`(`checked_at`·`holdings`[`account_ref`·`quantity`·`amount`]·`price`·`price_source`, `holdings`는 범위의 모든 계좌를 0도 포함해 적는다), `account_ref`, `scope_account_refs`, `thesis`, `invalidation_conditions`(최대 3개), `sector_key`.

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL register-plan --input /secure/plan-005930.json --actor operator:cli:mgh
```

## 4. 운영자 계획 이벤트 — `event`

닫힌 목록만 쓴다: `suspended`, `resumed`, `retired`, `tranche_closed_partial`, `thesis_broken`, `thesis_evidence_refreshed`, `holdings_disposed`(항상 `kept_outside_basket`). `approved`·`rejected`·fence·체결·`released`는 이 명령으로 기록할 수 없다.

모든 이벤트는 `--reason`(사유, `reason_code`로 기록)이 필요하다. 이벤트별로 다른 플래그만 받으며, 다른 플래그가 붙으면 거부한다.

- `suspended`·`retired`·`holdings_disposed`: 플래그 없이 `--reason`만.
- `resumed`: `--effective-session YYYY-MM-DD` 필수. `resume_target`은 상태에서 정한다(1차 체결 전이면 `approved`, 1차 완료면 `active`).
- `tranche_closed_partial`: `--tranche-index 1|2|3` 필수. 1차면 `first_fill_*`은 상태의 누적 체결을 그대로 쓴다.
- `thesis_broken`: `--retrospective-id N`(양의 정수)과 `--approval-issue-id S` 필수. §5.2의 증거 순서대로, 두 id를 먼저 확보한 뒤에 기록한다. 이벤트를 먼저 쓰고 회고를 나중에 만들면 손실 매도 가드가 받지 않는다.
- `thesis_evidence_refreshed`: `--retrospective-id`와 `--approval-issue-id` 필수. `thesis_seq`는 가장 최근 `thesis_broken`에서 상태가 정한다. 낡은 증거를 덮어쓰지 않고 덧붙인다.

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL event --plan-id 41 --event suspended --reason "운영자 정지 2026-11-24" --actor operator:cli:mgh
```

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL event --plan-id 41 --event resumed --effective-session 2026-11-25 --reason "재개" --actor operator:cli:mgh
```

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL event --plan-id 41 --event thesis_broken --retrospective-id 318 --approval-issue-id hk:5700 --reason "실적 가이던스 하향" --actor operator:cli:mgh
```

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL event --plan-id 41 --event holdings_disposed --reason "계획 계좌 밖 보유로 둠" --actor operator:cli:mgh
```

## 5. 상태 조회 — `status`

읽기 전용이다. `--plan-id`는 한 계획의 권한·정리 상태, 회차, 미결 fence, 이벤트 이력을 보여 준다. `--sleeve-key`는 그 sleeve의 계획 목록과 상태다.

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL status --plan-id 41
```

```bash
uv run python -m scripts.mandate --database-url-env MANDATE_DB_URL status --sleeve-key mock-kr-sleeve
```

## 하지 않는 것

- 승인 기록(`mandate_approvals`)을 만들지 않는다. 승인은 Telegram 카드(T3b)에서만 생긴다.
- 주문 제안을 만들지 않고 주문을 내지 않는다. 계획 결속 제안(T4a)과 전송(T4c·T5)은 별도 티켓이다.
- 스케줄러·cron·TaskIQ에 등록하지 않는다. 실행은 운영자가 직접 한다(하드룰 6).
- 원장(`execution_ledger` 등)에 쓰지 않는다.

## 확인

```bash
uv run pytest tests/services/mandate/test_cli.py -v
```

DB 왕복 테스트(`tests/services/mandate/test_cli_db.py`)는 CI의 실행 소유 테스트 데이터베이스에서만 돈다. 로컬 DB에 접속하지 않는다.
