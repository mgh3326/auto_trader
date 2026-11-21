# Upbit KRW shadow 실험: sleeve·위임장 생성 절차 (hk 5852 T9a)

설계: `docs/design/5852-upbit-krw-shadow-strategy.md` §7.5·§8·§9(T9a). 이 런북은 운영자가 실행하는 절차다. 스케줄러·Dagu 등록, live 전환, 주문은 이 런북의 범위가 아니다.

## 이 런북이 하는 일과 하지 않는 일

- **한다**: 팔 4개의 shadow sleeve(`scripts/strategy_sleeves.py`)와 shadow 위임장 4개(`scripts/execution_mandates.py`)를 미리보기 후 운영자가 `--commit`으로 만든다. 확인과 되돌림 명령을 적는다.
- **하지 않는다**: `--state live`, `--mode live`, live 위임장(`--approval-ref`, `--confirm-*`), `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED` 변경, 손실매도 가드·쿨다운 변경, 스케줄 등록, 주문. 이 런북의 명령에는 위 플래그가 없다.
- 이 런북을 실행해도 코드가 행을 만들지 않는다. 행은 운영자가 만든다.

## 전제 (실행 전에 확인)

1. 이 런북의 T9a PR이 머지되어 있고, 배포된 DB에 `review.strategy_sleeves`와 `review.execution_mandates`가 있다.
2. 위임장의 `--symbol`은 봉인된 U0 목록(T2 산출)의 심볼이다. U0이 아직 없으면 위임장을 만들지 않는다. 이 런북 작성 시점에는 U0 목록이 없으므로 `KRW-<U0 심볼>`은 자리표시자다. 심볼마다 `--symbol`을 반복한다.
3. `--strategy-code-sha`는 T4 일일 잡의 코드 커밋 sha(40자)다. T4가 머지되기 전에는 값을 정할 수 없다.
4. `--strategy` 라벨은 T4가 의도에 붙이는 이름과 같아야 한다. 아래 표의 라벨을 권고하며, T4 설계와 맞춘 뒤 확정한다.
5. DB URL은 운영자 셸의 환경변수 `TRADING_DB_URL`에 넣는다. 명령은 변수 이름만 받고 값을 출력하지 않는다.

## 값 (Q5 권고값, 운영자가 확정)

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 팔당 예산 `--budget-cap` | 1950000 KRW | Q5. 동시 4포지션 × 480,000 = 1,920,000 ≤ 1,950,000 (§5.3) |
| 손실 한도 `--loss-limit` | 1000000 KRW (누적) | Q5, §8.3 S3 |
| 실행 경로 `--execution-path` | `go` | 5438 S7. 위임장은 go 경로의 빈 sleeve에만 붙는다 |
| 상태 `--state` | `shadow` | 위임장 `shadow`는 shadow 상태의 sleeve에만 붙는다 |
| 사전등록 `--metric` | 위 명령의 문구 | §8.1. 대조군은 현금(0) |
| `--min-effect-pct` | 1.0 | §8.1 `delta_min` +1.0%p |
| `--horizon-sessions` | 30 | §8.2 "30세션 성숙" (아래 주의 2) |
| `--max-duration-days` | 91 | §8.2 총 91일. 위임장 수명 91일(F6)과 같다 |
| 위임장 `--per-intent-max-notional-krw` | 480000 | D9 상한, §5.3 |
| `--max-open-intents` | 4 | D9 상한, §4.3 |
| `--price-band-bps` | 30 | D9 상한, §5.2 `max_slippage_bps=30` |
| `--max-intent-ttl-seconds` | 43200 | D9 상한 12시간, §5.1 익절 갱신 주기 |
| `--expires-at` | 생성일 + 91일 (`+09:00`) | D9 수명 상한 91일 |

주의 (확정 전에 알고 있을 것):

1. **`--min-effect-pct`의 단위**: sleeve는 "sleeve 예산 대비 %"로 정의한다. §8.1의 `delta_min`은 "에피소드 의도 진입금액 대비 %p"다. 두 단위가 다르다. sleeve 쪽 값은 기록용이고, 5852의 판정은 재생 원장(T7)이 §8.1대로 한다.
2. **`--horizon-sessions`**: sleeve 보고서의 H는 "H번째 완성 종가로 평가한 값"이다. 5852의 판정은 청산까지의 실현 결과를 쓴다(§8.1). 30은 §8.2의 성숙 기간을 옮긴 값이며, 이 H로 sleeve 보고서가 내는 수치는 판정이 아니다.
3. **예산**: §5.3의 1,920,000은 4 × 480,000 계산이고, Q5는 여유를 둔 1,950,000을 쓴다.
4. 사전등록 4필드는 shadow 동안 바꿀 수 있지만, 이 실험에서는 바꾸지 않는다(S6). 바꾸면 새 에포크이므로 새 `sleeve_key`를 만든다.

## 팔 표

| 팔 | `sleeve_key` | `rule_id` | 위임장 `--strategy` 라벨 | 설명 |
| --- | --- | --- | --- | --- |
| A0 | `upbit-5852-a0` | `at5852.upbit_krw_trend_shadow` | `upbit-krw-shadow-a0` | 추세, 게이트 끔 |
| A1 | `upbit-5852-a1` | `at5852.upbit_krw_trend_shadow` | `upbit-krw-shadow-a1` | 추세, 게이트 켬 |
| B0 | `upbit-5852-b0` | `at5852.upbit_krw_pullback_shadow` | `upbit-krw-shadow-b0` | 눌림목, 게이트 끔 |
| B1 | `upbit-5852-b1` | `at5852.upbit_krw_pullback_shadow` | `upbit-krw-shadow-b1` | 눌림목, 게이트 켬 |

게이트 켬·끔은 sleeve에 저장되지 않는다. T4가 결정 로그(`review.decision_evidence`)에 기록한다. 팔마다 sleeve 하나와 위임장 하나다.

## 절차

### 1. sleeve 미리보기 (4팔)

미리보기는 아무것도 쓰지 않는다. 출력을 확인한다.

```bash
uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-a0 \
  --rule-id at5852.upbit_krw_trend_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 추세, 게이트 끔" \
  --actor "operator:<이름>"

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-a1 \
  --rule-id at5852.upbit_krw_trend_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 추세, 게이트 켬" \
  --actor "operator:<이름>"

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-b0 \
  --rule-id at5852.upbit_krw_pullback_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 눌림목, 게이트 끔" \
  --actor "operator:<이름>"

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-b1 \
  --rule-id at5852.upbit_krw_pullback_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 눌림목, 게이트 켬" \
  --actor "operator:<이름>"
```

### 2. sleeve 커밋 (4팔)

미리보기가 맞으면 같은 명령에 `--commit`을 붙여 실행한다. 출력의 `sleeve_id`를 기록한다. 미리보기 출력의 `sleeve_id`는 롤백된 값이라 존재하지 않는다.

```bash
uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-a0 \
  --rule-id at5852.upbit_krw_trend_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 추세, 게이트 끔" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-a1 \
  --rule-id at5852.upbit_krw_trend_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 추세, 게이트 켬" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-b0 \
  --rule-id at5852.upbit_krw_pullback_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 눌림목, 게이트 끔" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL \
  create \
  --sleeve-key upbit-5852-b1 \
  --rule-id at5852.upbit_krw_pullback_shadow \
  --account-mode upbit \
  --market crypto \
  --state shadow \
  --execution-path go \
  --budget-cap 1950000 \
  --loss-limit 1000000 \
  --metric "에피소드 비용 차감 순손익 ÷ 의도한 진입 금액, 현금(0) 대비 평균 차이 (5852 §8.1)" \
  --min-effect-pct 1.0 \
  --horizon-sessions 30 \
  --max-duration-days 91 \
  --reason "hk 5852 shadow 눌림목, 게이트 켬" \
  --actor "operator:<이름>" \
  --commit
```

### 3. 위임장 미리보기 (4팔)

위임장은 shadow sleeve(`--state shadow`, `--execution-path go`)에만 붙는다. sleeve 하나에 미철회 위임장은 하나다(S7). 명령의 `--symbol`은 U0 심볼로 바꾸고 반복한다.

```bash
uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-a0 \
  --strategy upbit-krw-shadow-a0 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>"

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-a1 \
  --strategy upbit-krw-shadow-a1 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>"

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-b0 \
  --strategy upbit-krw-shadow-b0 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>"

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-b1 \
  --strategy upbit-krw-shadow-b1 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>"
```

### 4. 위임장 커밋 (4팔)

shadow 위임장은 `--commit`만으로 만들어지며 승인 참조가 필요 없다(D12). shadow 위임장은 주문을 보낼 수 없다. 출력의 위임장 id를 기록한다.

```bash
uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-a0 \
  --strategy upbit-krw-shadow-a0 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-a1 \
  --strategy upbit-krw-shadow-a1 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-b0 \
  --strategy upbit-krw-shadow-b0 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>" \
  --commit

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL \
  propose \
  --sleeve-key upbit-5852-b1 \
  --strategy upbit-krw-shadow-b1 \
  --strategy-code-sha "<T4 코드 커밋 sha 40자>" \
  --mode shadow \
  --symbol "KRW-<U0 심볼>" \
  --per-intent-max-notional-krw 480000 \
  --max-open-intents 4 \
  --price-band-bps 30 \
  --max-intent-ttl-seconds 43200 \
  --expires-at "<생성일 + 91일, +09:00>" \
  --actor "operator:<이름>" \
  --commit
```

### 5. 확인

```bash
uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL report

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL list
```

`report`는 sleeve마다 `shadow` 상태, 예산, 손실 한도, 사전등록을 보여 준다. `list`는 위임장과 그 상태를 보여 준다. 4팔이 모두 보이고 `live`가 없어야 한다.

### 6. 기록

hk 문서에 다음을 남긴다: 팔 4개의 `sleeve_id`, 위임장 4개의 id, 실행자, 실행 시각, 각 커밋 명령의 미리보기 출력, `--strategy-code-sha` 값, U0 해시.

## 되돌림

- **위임장 철회**: 미리보기 후 `--commit`으로 철회한다. 철회 전에 예약된 자식 의도는 명령 출력에 나열된다.

```bash
uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL revoke --mandate-id "<mandate id>" --reason "<이유>" --actor "operator:<이름>"

uv run python -m scripts.execution_mandates --database-url-env TRADING_DB_URL revoke --mandate-id "<mandate id>" --reason "<이유>" --actor "operator:<이름>" --commit
```

- **sleeve 정지**: 상태를 `stopped`로 바꾼다. 이력은 지워지지 않는다.

```bash
uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL set --sleeve-key "<sleeve key>" --state stopped --reason "<이유>" --actor "operator:<이름>"

uv run python -m scripts.strategy_sleeves --database-url-env TRADING_DB_URL set --sleeve-key "<sleeve key>" --state stopped --reason "<이유>" --actor "operator:<이름>" --commit
```

정지된 실험을 재개하려면 새 에포크(새 `sleeve_key`, 새 사전등록)를 만든다. 같은 sleeve를 되살리지 않는다.

## 금지

- 이 실험의 어떤 sleeve도 `--state live`로 바꾸지 않는다. live 전환은 이 실험의 승인 범위가 아니다(§8.3 끝 문단).
- 위임장에 `--mode live`, `--approval-ref`, `--confirm-account-ref`, `--confirm-content-sha256`을 쓰지 않는다.
- cron, systemd, Dagu, TaskIQ 등록을 하지 않는다. 일일 잡의 Dagu 사본은 T4가 만들며 등록은 운영자 승인이다.
- `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`를 바꾸지 않는다.
