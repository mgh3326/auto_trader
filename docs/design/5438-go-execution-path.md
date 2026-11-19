# #5438 — Go 실행 경로 설계: 전략 → 의도 → Go 실행기 → 게이트웨이 → Upbit

hk task 5438이 요구한 설계 문서다. 상위 결정은 `hk:doc decision/2026-11-19/at-go-executor`(전략 실행을 Claude 세션에서 Go 실행기로)와 `hk:doc decision/2026-11-19/at-next-go`(4885 실험의 병행 보유·§11 권고값)다. 이 문서는 설계 문서다. 코드·마이그레이션·env·스케줄러·sleeve 행은 바뀌지 않는다.

인용 기준: auto_trader `origin/main` `0c3526cff`, fillwire `origin/main` `c86bdc8`. 표기는 4885와 같다 — **[사실]** = 그 SHA에서 확인, **[추정]** = 확인하지 않은 추론, **[의견]** = 이 문서의 권고. 운영자가 정해야 하는 것은 전부 §10 "운영자 결정 필요" 표에 번호(D1…)로 모으고, 본문에서는 권고값으로만 쓴다.

이 문서가 기대는 기존 설계(고치지 않는다, 4885만 §4·§9 지시 줄을 고친다):

- `docs/design/1440-intent-gateway-contract.md` — E1 게이트웨이 계약(이하 "E1 계약").
- `docs/design/4885-upbit-trend-long-live.md` — Upbit 롱 추세 실험(규칙 §3, 하방 §5, 중단 §8, 값 §11).
- `docs/design/3795-mandate.md` — 위임장(조항 §3.1, 인증된 승인 §3.5, 전송 경계 §4.6). T4c fence 메커니즘은 hk 5286에서 미결.
- fillwire `internal/exec`(상태 기계·노출·저널), `internal/exec/e1`(E1 클라이언트), `cmd/execmock`, `internal/upbit/reader.go`.

## 0. 현재 상태 요약 [사실]

| 조각 | 지금 | 이 설계가 바꾸는 방향 |
|---|---|---|
| 게이트웨이 계좌 | `ExecutionIntentV1.account: Literal["kis_mock"]`, `venue: Literal["krx"]`, `MOCK_ACCOUNTS = {"kis_mock"}`, DB CHECK `account IN ('kis_mock')` (`app/schemas/execution_intent.py:115-116`, `decisions.py:20`) | `upbit_live` 추가, 계좌별 forwarder |
| 수량·가격 | child `quantity`·`limit_price_krw` 정수, intent `max_quantity` 정수 1~1,000,000 | Upbit는 소수 수량(최대 8자리) 필요 → 별도 필드 |
| 단건 상한 | `LOCKED_ENVELOPE.max_single_notional_krw`(kis_mock runner 상수, 5,000,000) | Upbit는 위임장 상한이 더 작아 그쪽이 지배 |
| kill | `review.kis_mock_runner_control` `ACTIVE`만 submit/amend 허용 | Upbit 전용 kill 행 필요 |
| fillwire E1 클라이언트 | `Config.Account`가 정확히 `MockAccount`가 아니면 `Off`(`internal/exec/e1/client.go:112-124`), `Child.Qty int64`·`Price int64`(`machine.go:44-51`) | Upbit 계좌·소수 수량 지원 |
| fillwire Upbit | `internal/upbit/reader.go` — 공개 웹소켓 `orderbook`(`.15`)·`trade` 기록기, 무인증, 주문 없음 | 실행기의 호가 공급원 |
| 실행기 시각 규칙 | `StaleAfter` 기본 2초, 그보다 오래된 호가로는 새 submit/amend 안 함(`executor.go:21-28`, `machine.go:426`) | 그대로 재사용 |
| 4885 주문 | 결정마다 `order_proposal_create` → Telegram 카드(건별 승인) | 의도 경로 + 위임장으로 개정(§7) |
| Upbit 호가 단위 | 레포에 KR 호가 함수만 있음(`app/mcp_server/tick_size.py`), Upbit KRW 호가표 함수 없음 | 5441이 순수 함수로 추가 |

## 1. 전 구간 흐름과 소유

### 1.1 흐름

```
[Dagu 결정 잡 14:20 / 점검 잡 20:20·02:20·08:20]   (hk 5440, 등록은 운영자 승인)
   │  일봉·sleeve 장부·위임장 읽기 → 4885 §3 규칙 → 결정 행 + intent 저장
   ▼
review.execution_intents  (Python store_intent 만 씀, 불변)
   │  intent id 를 실행기 큐에 노출(읽기 전용 GET)
   ▼
[Go 실행기 fillwire cmd/execupbit]  (hk 5439)
   │  Upbit 공개 웹소켓 호가 → 지정가·타이밍 결정 → child 요청
   ▼  POST /execution-intents/children (루프백, 토큰, confirm 헤더)
[Python 게이트웨이 E1]  (hk 5441)
   │  env 게이트 → confirm → 위임장 검사 → kill → 예약(저널 커밋) → forwarder 1회
   ├── execution_mode=shadow → UpbitShadowForwarder (브로커 import 없음, 기록만)
   └── execution_mode=live   → UpbitLiveForwarder → 기존 Upbit 주문 서비스
                                   │
                                   ▼
                     live_order_ledger / execution_ledger (기존 writer·대사)
   ▲
[채점] sleeve report(ROB-1470) + 4885 §7 지표 — 원장만 읽음
```

### 1.2 소유 표

| 구성 | 소유 티켓 | 결정하는 것 | 쓰는 것 | 쓰지 않는 것 |
|---|---|---|---|---|
| 전략 잡 | hk 5440 | 신호·동작(진입/유지/청산), 크기, 가격 경계, 만료 | `review.execution_intents`(`store_intent` 경유), 결정 로그 행(§6.4) | 브로커, 원장, child |
| 위임장 | hk 5441(테이블·검사), 운영자(행) | 어떤 전략 버전이 어떤 sleeve·상한·기간 안에서 의도를 낼 수 있는가 | `review.execution_mandates`(운영자 명령만) | — |
| Go 실행기 | hk 5439(fillwire) | 의도 범위 안의 타이밍·지정가, 추격·취소 | 로컬 저널(fillwire `internal/exec/journal.go`), Postgres 아무것도 안 씀 | 브로커, DB |
| 게이트웨이 | hk 5441 | 허용/거부, 한 번 전송 | `review.execution_child_requests`(저널), shadow 기록 | 원장(브로커 서비스가 씀) |
| 브로커 서비스 | 기존 | Upbit 주문 전송 | `review.live_order_ledger`(기존 writer), 쿨다운(Redis) | — |
| 원장·대사 | 기존 | 체결 증거 | `execution_ledger`(대사·fillwire 체결 ingest) | — |
| 채점 | 기존 sleeve report + 4885 §7 | 에피소드 결과 | 없음(읽기) | — |
| 운영(pi) | hk 5442 | 프로세스 기동·감시·킬 | 런북, Dagu 정의(등록은 승인 후) | 코드 |

**Claude 세션 단계적 축소 조건:** 4885 실험이 이 경로로 실주문(live)을 시작하고 §9.2의 실주문 첫 2주 점검을 통과하면, Upbit 네 코인에 대한 정기 매매 세션의 제안 생성을 중단한다(세션은 연구·감시로 남는다). 다른 시장의 세션은 각 시장이 같은 경로에 올라올 때 같은 조건으로 줄인다.

## 2. T3 불변식 표

wake §T3 형식: 불변식 · 입력 · 전이 · 강제 위치 · 의존 · 관측 결과. "강제 위치"의 파일은 5441·5439가 만들거나 고칠 곳이다.

| # | 불변식 | 입력 | 전이 | 강제 위치 | 의존 | 관측 결과 |
|---|---|---|---|---|---|---|
| I1 | **승인된 위임장 행 없이 주문 없음.** `upbit_live` intent는 저장 시점과 child 예약 시점 모두 유효한 위임장이 있어야 한다 | intent `mandate_id`, 현재 시각, 위임장·철회 행 | `store_intent`: 없음/만료/철회/불일치 → 저장 거부. child 예약: 같은 검사 실패 → `refused` | `store.store_intent`, `decisions.decide`(새 `mandate_check`), DB FK `execution_intents.mandate_id → execution_mandates.id` | §4 위임장 테이블 | 거부 사유 `mandate_missing`/`mandate_expired`/`mandate_revoked`/`mandate_scope_mismatch`, 저널 `refused` 행 |
| I2 | **intent 단위 상한**: 누적 수량·명목·가격 경계·만료 | intent `max_quantity_decimal`·`max_notional_krw`·`limit_price_krw`·`max_slippage_bps`·`expires_at`, 저널 high-water | 초과 → `refused` | `decisions._caps_refusal`, `_price_within_bound`(기존, 소수 수량으로 일반화) | E1 계약 "How caps count" | `quantity_cap_exceeded`·`notional_cap_exceeded`·`price_outside_bound`·`authorization_expired` |
| I3 | **intent 상한 ⊆ 위임장 상한**: 한 intent의 `max_notional_krw` ≤ 위임장 `per_intent_max_notional_krw`, `expires_at` ≤ min(위임장 만료, 저장 + 위임장 `max_intent_ttl`), 가격 경계 ≤ 위임장 `price_band_bps`, 심볼 ∈ 위임장 심볼 | intent, 위임장 | 위반 → 저장 거부 | `store_intent`(Python 검증 + DB 트리거 대조) | I1 | `IntentConflict` 아닌 `MandateScopeViolation`, 행 없음 |
| I4 | **sleeve 단위 상한**: sleeve의 열린 매수 노출 + 새 intent 명목 ≤ sleeve `budget_cap`; 동시 열린 매수 intent 수 ≤ 위임장 `max_open_intents` | sleeve 장부(`book`), 열린 intent 합 | 초과 → 저장 거부(intent 생성 시) + child 예약 시 sleeve 재판정 실패 → `refused` | `store_intent` + 게이트웨이 예약 직전 `strategy_sleeves.gate`(같은 advisory lock) | ROB-1470 sleeve 게이트 | `sleeve_budget_exceeded`·`sleeve_used_unmeasured`(미측정 = 거부) |
| I5 | **child 키당 1회 전송, 불명은 재전송 없음** | `idempotency_key`, 저널 | 이미 있는 키 → 첫 답 재생; 예외·실패·무응답 → `uncertain`/`sending` 유지 | 기존 `gateway.handle` 예약·저널 트리거(E1 계약 Processing order 4·6), Upbit forwarder는 `identifier`=child 키 | E1 저널, Upbit `identifier` 고유성 | 재생 시 `replayed=true`; Upbit 측 같은 identifier 재사용 불가가 2차 방어 |
| I6 | **shadow는 아무것도 보내지 않고 live와 증명 가능하게 구분** | intent `execution_mode`(불변), 위임장 `mode` | shadow intent → `UpbitShadowForwarder`만 선택 가능, 결과 상태 `shadowed`(브로커 번호 NULL) | forwarder 선택표(`gateway.__init__`), shadow 모듈 정적 import 가드 테스트, DB CHECK `execution_mode='shadow' ⇒ broker_order_id IS NULL`, intent·위임장 mode 일치 트리거 | §3 wire, §4 위임장 | 저널 `state='shadowed'`, `live_order_ledger` 행 0 |
| I7 | **kill switch** — 세 단계 | (a) env `EXECUTION_INTENT_GATEWAY_ENABLED`, (b) 계좌 kill 행 `review.execution_account_control(account='upbit_live')`, (c) 위임장 철회, (d) 실행기 로컬 kill | (a) 꺼짐 → DB 접근 전 전부 거부; (b) `ACTIVE` 아님/읽기 실패 → submit·amend 거부(cancel 허용); (c) → 새 intent·submit·amend 거부; (d) → 실행기가 child를 만들지 않음 | 기존 env 게이트, `decisions`의 kill 읽기를 계좌별로 일반화, I1, fillwire `DoneKilled` | E1 계약 Cancel rules | `gateway_disabled`·`submissions_killed`·`mandate_revoked` |
| I8 | **sleeve 손실 한도 도달 → 새 intent 중단** | sleeve 장부 손실, `loss_limit` | 손실 ≥ 한도 → sleeve `stopped` 버전(기존 `system:sleeve_loss_gate`), 이후 `store_intent`·submit·amend 거부; 청산(sell) intent는 허용 | `store_intent` + 예약 직전 sleeve 판정 | ROB-1470 `dispatch_gate` 동작 재사용 | `sleeve_loss_limit_reached`, sleeve 버전 행 |
| I9 | **매도는 보유 이상 못 함(현물 롱 전용)** | intent `inventory`(소수), sleeve 귀속 보유 | `min_position_after < 0` 불가; 매도 intent 수량 ≤ sleeve 귀속 보유 | 스키마(기존 inventory 규칙의 소수판), `store_intent`가 sleeve 장부 대조 | sleeve 장부 귀속 | 저장 거부 `inventory_constraint_exceeded` |
| I10 | **손실매도 가드·8일 쿨다운·loss_cut은 완화하지 않는다** | 계정 평단(`avg_buy_price`), 쿨다운(Redis), 매도 지정가 | 매도 지정가 < 평단×1.01 → 게이트웨이는 보내지 않고 `refused/loss_sell_requires_card`, 전략 잡이 loss_cut 카드 제안 생성(§2.1 권고 A); 매수 심볼이 쿨다운 → `refused/cooldown_active` | 게이트웨이 예약 전 사전검사(읽기만) + 하위 `place_order` 가드(그대로, 2차) | 기존 `order_validation.py:191-200`, `order_execution.py` 쿨다운 | 저널 `refused` + 카드 경로 이관 기록 |
| I11 | **Go는 브로커·DB에 닿지 않는다** | — | — | fillwire 정적 테스트(기존 `internal/upbit/noorder_test.go` 패턴), 루프백 URL 검사(`e1.checkURL`) | E1 계약 Roles | 테스트 |
| I12 | **결정 시각당 intent 1개(멱등)** | 전략 버전, 심볼, 결정 시각, 동작 | 같은 입력 → 같은 `intent_id`(uuid5) → `store_intent` no-op | 전략 잡(§6.3) + 기존 `IntentConflict` | E1 intent 불변 | 재실행 시 행 수 불변 |

### 2.1 I10 — 평단 아래 전략 청산의 선택지

**사실.** 손실매도 가드는 계정 평단(`avg_buy_price`, 업비트 계정 전체) 기준 `×1.01` 미만 매도를 거부하고, 크립토 매도가 `avg×(1−0.045)` 이하면 그 심볼 매수를 8일 막는다. 운영자 답(at-next-go 1, 병행 A)으로 기존 보유 약 872만 원이 같은 계정에 남으므로 **평단은 실험 진입가가 아니라 기존 보유를 섞은 계정 평단**이다. 따라서 실험 leg가 이익이어도 가드가 걸리거나, 손실이어도 안 걸릴 수 있다 [추정 — 네 코인의 계정 평단은 읽지 않았다].

| 선택지 | 내용 | 가드 변경 | 장점 | 단점 |
|---|---|---|---|---|
| **A (권고)** | 평단×1.01 이상 매도만 의도 경로. 미만이면 게이트웨이가 `loss_sell_requires_card`로 거부(전송 0)하고, 전략 잡이 같은 결정을 기존 `exit_intent="loss_cut"` 제안 + 2클릭 카드로 낸다(4885 §4의 기존 청산 경로 유지) | 없음 | 하드룰 4 완전 준수, 오늘 바로 가능 | 손절 지연(사람 2클릭)이 그대로 남는다 — 4885 §5.2 한계 유지 |
| B | 위임장에 묶인 `strategy_exit_ctx`(기존 `scalping_exit_ctx` 우회와 같은 형태, `order_validation.py:1237`)로 가드를 건너뛰는 자동 청산 | **가드 완화** | 손절 지연 제거 | 운영자 결정 + deviation record(3795 §4.5 형식) + blocking 리뷰 필요. 3795 Q5 = B(`loss_sell_mode=human_card`만)와 충돌 |
| C | 평단 아래 청산을 아예 안 함(보유 유지) | 없음 | 단순 | 4885 §3.4 hard stop·신호 반전 규칙 위반 → 실험 무의미 |

권고는 A이고, B는 §10 D5로 남긴다. 쿨다운은 어느 선택지에서도 바꾸지 않는다: 쿨다운으로 거부된 진입은 4885 §5.4대로 `blocked_by_cooldown`으로 센다.

## 3. Upbit child wire 계약 변경

### 3.1 원칙

1. **kis_mock 바이트 동일.** 기존 필드·검증·fillwire 픽스처(`tests/fixtures/fillwire_e1/`)는 한 글자도 바뀌지 않는다. 새 계좌는 새 필드로만 표현한다.
2. **계좌가 필드 집합을 정한다.** 판별자는 intent의 `account`이고, child는 intent를 id로만 가리키므로 게이트웨이가 저장된 intent의 계좌로 child 필드 집합을 고른다.
3. **소수는 JSON 문자열.** 부동소수 왕복 오차를 없애려고 소수 수량은 문자열 decimal이다(기존 `/fills` 응답의 `price`와 같은 관행).

### 3.2 intent (`ExecutionIntentV1` 확장, `schema_version` 2)

`schema_version=1`은 `kis_mock` 전용으로 그대로 둔다. `upbit_live`는 `schema_version=2`만 받는다.

| 필드 | v2 규칙 |
|---|---|
| `schema_version` | 정확히 `2` |
| `account` | 정확히 `upbit_live` |
| `venue` | 정확히 `upbit` |
| `execution_mode` | `shadow` 또는 `live`. 필수, 기본값 없음, 불변. 위임장 `mode`와 같아야 함(I6) |
| `mandate_id` | 필수 UUID, `review.execution_mandates.id` FK(I1) |
| `sleeve_id` | 필수, 위임장의 sleeve와 같아야 함 |
| `symbol` | `KRW-BTC`·`KRW-ETH`·`KRW-SOL`·`KRW-XRP` 형식(`^KRW-[A-Z0-9]{2,10}$`) **이고** 위임장 심볼 목록에 있음 |
| `side` | `buy`/`sell` |
| `max_quantity_decimal` | decimal 문자열, `^[0-9]+(\.[0-9]{1,8})?$`, 0 초과, ≤ 1,000,000 |
| `max_notional_krw` | 정수 KRW, 5,000 이상(Upbit 최소 주문, dots §2) ~ 위임장 `per_intent_max_notional_krw` 이하 |
| `limit_price_krw` | 정수 KRW, Upbit KRW 호가 단위에 맞는 값(§3.4), 1 ~ 10,000,000,000 |
| `max_slippage_bps` | 정수 0 ~ min(500, 위임장 `price_band_bps`) |
| `expires_at` | 저장 후 위임장 `max_intent_ttl`(권고 12시간) 이내 **이고** 위임장 만료 이전 |
| `inventory` | `position_at_authorization`·`min_position_after`·`max_position_after`를 decimal 문자열로(같은 정규식, 0 이상) |
| `strategy`·`policy_version`·`model_version`·`authorization_ref` | 기존 규칙. `authorization_ref`는 `mandate:<mandate_id>` 형식 |
| `decision_ref` | 필수, 결정 행 키(§6.4) — I12 멱등 근거 |

`max_quantity`(정수)는 v2에서 금지(있으면 422), `max_quantity_decimal`은 v1에서 금지.

### 3.3 child

| 필드 | `kis_mock` (불변) | `upbit_live` |
|---|---|---|
| `action` | `submit`/`amend`/`cancel` | `submit`/`cancel` (**amend 금지** → 422 `amend_unsupported_for_account`; Upbit 정정은 취소+신규로만, 실행기가 새 child 키로 표현) |
| `quantity` | 정수, 필수(submit·amend) | **금지** |
| `quantity_decimal` | **금지** | decimal 문자열, submit 필수, `^[0-9]+(\.[0-9]{1,8})?$`, 0 초과 |
| `limit_price_krw` | 정수 | 정수, §3.4 호가 검사 |
| `target_idempotency_key` | 기존 | 기존(cancel) |
| 미지 필드 | 거부 | 거부 |

검증 순서(게이트웨이 라우트): JSON 파싱 → 공통 필드 → intent 조회 → 계좌별 필드 집합 검사(위반은 기존처럼 422가 아니라 E1이 이미 intent를 읽은 뒤이므로 `refused/child_shape_invalid_for_account`, 저널 행 없음 — 아무것도 결정하지 않았기 때문). 수량 자릿수 초과(9자리 이상)는 잘라 쓰지 않고 거부한다.

명목 계산: `notional = quantity_decimal × limit_price_krw`를 `Decimal`로 정확히 계산하고 상한 비교는 올림 없이 정확히 한다. high-water·누적 규칙은 E1 계약 그대로(cancel이 노출을 풀지 않음). 새 Upbit 단건 상한은 `LOCKED_ENVELOPE`(kis_mock runner 상수)를 쓰지 않고 위임장 `per_intent_max_notional_krw`가 대신한다 — 이 상수를 넓히는 것이 아니라 다른 계좌에 적용하지 않는 것이다.

### 3.4 KRW 호가 단위

5441은 `app/core/`에 순수 함수 `upbit_krw_tick(price: Decimal) -> Decimal`을 추가한다(Upbit 공개 호가표, dots §2 기준, 표의 출처 URL과 확인일을 docstring에 둔다). 규칙: `limit_price_krw % tick == 0`이 아니면 `refused/price_off_tick`. 실행기는 같은 표를 Go로 가져가며(5439), 두 구현은 공유 픽스처(구간 경계값 목록 JSON)로 대조한다. 1 KRW 미만 호가 구간의 종목은 v2가 받지 않는다(정수 KRW 필드라서) — `refused/price_tick_unsupported`. 현재 유니버스 네 코인은 해당하지 않는다 [추정 — 실행 시 호가로 확인].

### 3.5 outcome

기존 `accepted`/`uncertain`/`sending`/`refused`/`conflict`에 **`shadowed`** 하나를 더한다: shadow 모드에서 게이트웨이가 모든 검사를 통과시켜 저널에 기록했고, 아무것도 보내지 않았다. `broker_order_id`는 항상 비어 있다. 실행기는 `shadowed`를 "가상 접수"로 다루고(§5.6), 절대 `accepted`로 바꾸지 않는다. 저널 트리거는 `sending → shadowed`를 허용하되 그 행의 intent가 `execution_mode='shadow'`일 때만이다(DB CHECK/트리거, I6).

### 3.6 fillwire E1 클라이언트 호환 (5439 요구사항)

- `Config.Account`에 `UpbitLiveAccount = "upbit_live"`를 추가한다. 모드 판정은 기존과 같고, `upbit_live` + `Send`는 추가 플래그 `UpbitSendEnabled`가 있어야 한다(없으면 `DryRun`보다 약한 `Off`).
- `Child`에 `QtyDecimal string \`json:"quantity_decimal,omitempty"\``를 더한다. kis_mock child 직렬화는 바이트 동일(기존 contract test가 고정).
- 내부 수량은 정수 `int64`에서 **고정소수 정수(1e-8 단위, `int64`)**로 일반화한다 — kis_mock은 배율 1, Upbit는 배율 1e8. `exec.Fill.Qty`도 같은 배율. 기존 테스트 골든은 배율 1에서 바뀌지 않는다.
- `/fills` 응답의 `qty`·`cumulative_qty`는 v2 intent에 대해 decimal 문자열로 온다(게이트웨이 5441이 계좌별로 직렬화). `reads.go`는 intent 계좌로 파서를 고른다.
- outcome `shadowed`를 인식한다. 모르는 status는 지금처럼 unknown으로 다룬다.

## 4. 최소 위임장

### 4.1 무엇인가

"이 전략 버전이, 이 sleeve 안에서, 이 상한·이 기간 동안, 이 모드(shadow/live)로 의도를 낼 수 있다"는 한 행. 3795 `review.mandate_terms`의 **부분집합**이다(포크가 아님): 같은 의미의 열은 같은 이름·같은 의미를 쓰고, 3795 T4가 구현되면 이 행은 `mandate_terms`의 한 terms 버전으로 흡수된다(§4.5).

### 4.2 테이블 `review.execution_mandates` (append-only, 추가형 마이그레이션 — 하드룰 18 GRANT 포함, 5441)

| 열 | 형 | 의미 | 3795 대응 |
|---|---|---|---|
| `id` | uuid PK | | — |
| `terms_version` | text CHECK = `execution-mandate/min-v1` | 스키마 버전 | `terms_version` |
| `strategy` | text | 전략 라벨(예 `upbit-trend-long-v1`) | — (3795는 sleeve 규칙 id로 대체) |
| `strategy_code_sha` | text(40) | 전략 잡 코드 SHA. 다른 SHA의 잡이 낸 intent는 거부 | — |
| `sleeve_id`·`sleeve_version_id` | FK | 승인 대상 sleeve와 그 버전 | `sleeve_version_id` |
| `account` | text CHECK IN (`upbit_live`) | | `account_ref`(불투명 참조는 5441이 기존 Upbit 계정 조회로 채움) |
| `mode` | text CHECK IN (`shadow`,`live`) | | — (3795는 활성화 프로필) |
| `allowed_symbols` | text[] | 정규화 심볼 | `allowed_symbols` |
| `per_intent_max_notional_krw` | numeric | intent 하나의 명목 상한(4885 `LEG_CAP` 480,000) | `per_order_cap` |
| `daily_max_buy_notional_krw` | numeric | UTC일 매수 child 합 상한 | `daily_cap` |
| `max_open_intents` | int | 동시 열린 매수 intent 수(4885: 4) | — |
| `price_band_bps` | int | 기준 호가 대비 허용 폭 | `entry_max_premium_bps` |
| `max_intent_ttl` | interval | intent 만료 최대 길이(권고 12h) | — |
| `expires_at` | timestamptz | 위임장 만료(4885 최대 기간 84일 + 청산 7일 이하) | `expires_at` |
| `loss_sell_mode` | text CHECK = `human_card` | 평단 아래 매도는 카드(§2.1 A) | `loss_sell_mode` |
| `approval_ref` | text | live: 3795 §3.5 승인 기록 id. shadow: 운영자 CLI 기록 | `approval_id` |
| `content_sha256` | text | 위 내용 열 + sleeve 버전 값의 해시 | `content_sha256` |
| `created_by`·`created_at` | | | — |

철회: `review.execution_mandate_revocations(mandate_id UNIQUE, revoked_by, reason, revoked_at)` append-only. 두 테이블 UPDATE·DELETE·TRUNCATE 트리거 거부, downgrade는 행이 있으면 거부(E1 테이블과 같은 규칙).

### 4.3 누가 쓰는가

- `scripts/execution_mandates.py propose ...` — 기본 **preview**(내용과 `content_sha256` 출력, 쓰기 0). `--commit`이 있어야 쓴다. 운영자만 실행(시스템 actor 이름 거부, 기존 sleeve store 규칙).
- **shadow 위임장**: `--commit`으로 바로 행 생성. 주문이 나갈 수 없으므로 CLI 기록으로 충분하다 [의견].
- **live 위임장**: CLI는 승인 요청(초안)만 만들고, 행은 3795 §3.5의 운영자 principal 대조 Telegram 카드 콜백으로만 생긴다. 3795 §3.5가 아직 구현되지 않았으면 live 위임장은 만들 수 없다 → §10 D2(대안: 운영자 CLI `--commit` + 해시를 hk 결정 doc에 남기는 임시 방식).
- `revoke` 명령: preview 기본, `--commit`으로 철회 행. 철회는 즉시 새 intent·submit·amend를 막고 cancel은 막지 않는다.

### 4.4 게이트웨이 검사

`store_intent`와 child 예약(submit) 두 번: 위임장 존재 · 미철회 · `now < expires_at` · `mode == intent.execution_mode` · 심볼·sleeve·전략·`strategy_code_sha` 일치 · intent 상한 ⊆ 위임장 상한 · sleeve 버전이 현재 live/shadow 상태와 위임장 `mode`에 맞음(live 위임장은 sleeve `state='live'`). 예약 시 검사는 intent 행 잠금 아래에서 읽는다(E1 Expiry 판정과 같은 위치). 전송 직전 재검사(pre-send hook)는 E1 계약 Open items "Expiry and kill at the wire"를 Upbit forwarder에서 닫는다: Upbit place 경로의 `pre_send_hook`에서 철회·kill·만료를 다시 읽고 실패면 `proven_not_sent`로 기록한다.

### 4.5 3795와의 관계와 hk 5286

- 이 최소판은 3795의 바구니·회차 계획·업종 상한·낙폭 동결·활성화 프로필을 **갖지 않는다**. 단일 sleeve·단일 전략용이다.
- 3795 T4 구현 시 마이그레이션: 각 `execution_mandates` 행 → `mandate_terms`의 `terms_version='execution-mandate/min-v1'` 행으로 옮기거나, `mandate_terms`가 이 테이블을 참조하게 한다. 어느 쪽인지는 3795 T4 브리프가 정한다.
- hk 5286(T4c fence 메커니즘)은 여기서 정하지 않는다. 최소판이 필요한 것은 "전송 직전 위임장 재확인" 하나이고 그것은 §4.4의 pre-send hook으로 충분하다. 5286의 결론이 다른 메커니즘이면 5441은 그 결론을 따른다.

## 5. Go 실행기 타이밍 정책 (hk 5439 요구사항)

| # | 요구사항 |
|---|---|
| R1 | 호가 공급원은 `internal/upbit/reader.go`의 공개 웹소켓 `orderbook` 스트림을 재사용한다(무인증). 주문용 private 웹소켓·REST 키를 쓰지 않는다 |
| R2 | **진입(buy)**: submit 지정가 = 최우선 매도호가(taker, 4885 §4.3·Q10). 그 가격이 intent 경계(`limit_price_krw × (1+bps)`)를 넘으면 보내지 않고 기다린다. 수량 = min(intent 잔량, floor(남은 명목 / 가격, 8자리)) |
| R3 | **청산(sell)**: 지정가 = 최우선 매수호가. 경계(`limit × (1−bps)`) 아래면 보내지 않는다. 평단×1.01 미만 청산은 게이트웨이가 거부하므로(I10) 실행기는 `loss_sell_requires_card`를 받으면 그 intent를 `DoneRefused`로 끝내고 재시도하지 않는다 |
| R4 | **추격**: 접수 후 `ChaseAfter`(권고 30초) 동안 체결이 없거나 잔량이 남고 호가가 불리하게 움직였으면 cancel → 새 키로 submit(Upbit에 amend 없음). 한 intent의 cancel+재submit은 최대 `MaxChases`(권고 5회). 넘으면 cancel 후 `DoneCap` |
| R5 | **만료**: intent `expires_at` − `ExpiryMargin`(권고 60초)에 새 submit 금지, 열린 주문은 cancel. 만료 후 남은 잔량은 다음 결정 시각의 새 intent 몫이다 |
| R6 | **신선도**: 호가가 `StaleAfter`(2초, 기존 기본값)보다 오래됐거나 미래 시각이거나 bid/ask가 비면 새 submit 없음(기존 `machine.go:426`). 웹소켓 끊김 동안 새 submit 없음, cancel은 허용 |
| R7 | **재연결**: 끊기면 지수 백오프로 재연결(기존 reader 동작). 재연결 직후 첫 호가가 동기화될 때까지 새 submit 없음. 재시작 시 로컬 저널을 재생하고, `sending`/`uncertain` child가 있으면 E1 `/intents/<id>` 질의가 `reconciled=true`를 줄 때까지 그 intent에서 아무것도 하지 않는다(E1 계약 Query) |
| R8 | **불명 결과**: `uncertain`/`sending`/비200 응답은 재전송하지 않는다. 그 intent는 대사될 때까지 보류 |
| R9 | **shadow**: 실행기 로직은 live와 같다. `shadowed` 답을 받으면 같은 시각 호가로 가상 체결을 로컬 저널에만 기록한다(매수: 그 시각 최우선 매도호가에 전량, 매도: 최우선 매수호가). 이 가상 체결은 E1·원장에 쓰지 않는다. shadow 채점용 출력 파일 형식은 5439가 정하고 5440 채점이 읽는다 |
| R10 | **요청 한도**: Upbit 주문 12회/초(dots §4)보다 훨씬 낮게 — 실행기 전체 child 1회/초 상한 |
| R11 | **kill**: 로컬 kill 파일/신호면 새 child 없음, 열린 주문 cancel은 계속(E1 Cancel rules) |
| R12 | **intent 수신**: 게이트웨이의 새 읽기 전용 목록 `GET /execution-intents/intents?account=upbit_live&open=true`(5441)를 `PollEvery`(권고 15초)마다 읽는다. Go는 intent를 만들거나 바꾸지 않는다 |

## 6. 전략 잡 (hk 5440)

### 6.1 언제

| 잡 | 시각(KST) | 하는 일 |
|---|---|---|
| 결정 | 14:20 (crypto 슬롯, at-next-go 2) | 4885 §3 신호 계산, 진입·신호반전·time exit intent 생성 |
| 손절 점검 | 20:20 · 02:20 · 08:20 | hard stop(진입가 대비 −10%) 판정 → 매도 intent |

Dagu DAG 정의는 `deploy/dagu/`에 사본으로 두고 **등록은 운영자 승인**이다(하드룰 6, at-go-executor "주지 않는 것"). 등록 전에는 같은 명령을 운영자가 수동으로 돌린다.

> 4885 §1.4·§3.3은 결정 시각을 09:00 KST 일봉 마감으로 적었고 at-next-go 2는 결정 슬롯을 14:20으로 정했다. 신호는 **09:00 KST에 마감된 일봉**으로 계산하고, 14:20은 그 신호로 intent를 내는 실행 시각이다(5시간 20분 지연은 측정 대상). 이 해석은 §10 D7.

### 6.2 무엇을 읽는가

`daily_candles`(Upbit KRW 일봉), sleeve 장부(`app/services/strategy_sleeves/book`·`report`), 위임장 행, 열린 intent와 그 `/fills`, 직전 결정 로그. 브로커를 부르지 않는다 — 단 hard stop 판정의 현재가는 공개 시세 읽기(`fetch_multiple_tickers`, 무인증) 한 번을 허용한다.

### 6.3 intent 만들기와 멱등

- `intent_id = uuid5(NAMESPACE, f"{strategy}|{strategy_code_sha}|{symbol}|{decision_at.isoformat()}|{action}")`. 같은 결정 시각 재실행은 같은 id → `store_intent` no-op(I12). 다른 내용이면 `IntentConflict`로 실패하고 잡이 비영(非0)으로 끝난다(경보).
- 진입: `max_notional_krw` = 4885 §3.5 크기(`LEG_CAP × min(1, σ*/σ̂)`), 50,000 미만이면 intent 없음 + `skipped_below_floor` 기록. `limit_price_krw` = 잡 시점 최우선 매도호가(호가 단위), `max_slippage_bps` = 위임장 `price_band_bps`. `max_quantity_decimal` = `max_notional / limit_price`를 8자리 내림.
- 청산: 수량 = sleeve 귀속 보유 전량. 가격 = 최우선 매수호가. 평단×1.01 미만이 **예상되면** intent 대신 바로 loss_cut 제안 카드(§2.1 A)를 낸다 — 게이트웨이 거부를 기다리지 않는다. 판정 실패(평단 읽기 실패)는 intent를 내고 게이트웨이 판정에 맡긴다.
- 쿨다운 중인 심볼의 진입은 intent를 만들지 않고 `blocked_by_cooldown`.

### 6.4 결정 로그

4885 §4의 "레포 밖 운영자 파일" 대신 결정 행을 DB에 둔다: 기존 `review.decision_evidence`(hk 4182, `DecisionEvidenceService`만 쓰기)에 결정 시각마다 종목별 한 행(신호값, σ̂, 크기, 동작 또는 no-op 사유, 생성한 intent id). intent `decision_ref`가 그 행을 가리킨다. no-op도 행이 있어야 분모가 생긴다.

### 6.5 손절 점검이 매도 intent가 되는 방식

점검 잡은 열린 leg마다 현재가 ≤ 진입가×0.90이면 `action=exit_hard_stop` 매도 intent(같은 멱등 키 규칙, `decision_at`=점검 슬롯 시각)를 낸다. 이미 열린 매도 intent가 있으면 새로 내지 않는다. 진입가는 sleeve 장부의 귀속 체결 평균(계정 평단이 아님).

## 7. 4885 개정

이 PR은 4885에서 다음 줄만 고친다(§3 규칙과 §11 값은 그대로):

| 4885 위치 | 바뀌는 것 |
|---|---|
| §4 머리 | "카드 경로"는 이 문서의 의도 경로로 대체된다는 안내 줄 추가. 기존 §4 본문은 의도 경로가 live로 열리기 전까지의 대체 경로이자, 평단 아래 청산(§2.1 A)에서 계속 쓰는 loss_cut 카드 절차로 남는다 |
| §9 첫 줄·"건별 주문 승인" 줄 | "자동 집행·위임장 미승인"을 "자동 집행은 5438 의도 경로 + 최소 위임장으로 개정(at-go-executor), 그 경로의 shadow→live 전환·위임장 행 승인은 별도 운영자 행위"로 바꾼다 |
| §9 "스케줄러" 줄 | 결정·점검 Dagu 잡 등록도 별도 승인임을 명시 |

**봉인 보류.** 4885 manifest는 이 경로(§10 운영자 결정)가 확정되고 shadow 단계 종료 기준(§9.1)을 통과할 때까지 seal하지 않는다(draft, `k=0` 유지). 봉인 구간 시작은 live 전환 이후다.

## 8. 티켓 경계와 닫힌 Acceptance

### 8.1 hk 5440 — 전략 잡 (auto_trader)

범위: `app/services/upbit_trend/`(신호·크기·결정·intent 작성), `scripts/upbit_trend_job.py`(`decide`/`stop-check`, preview 기본), Dagu 사본 2개(미등록). 5441의 스키마 v2·위임장 검사에 의존.

1. 4885 §3.3~3.5 규칙을 순수 함수로 구현하고, 경계값(NO_SIGNAL, 바닥 50,000, σ̂ 스케일, 8자리 내림) 테스트가 있다.
2. `decide`·`stop-check`는 기본 preview(쓰기 0)이고 `--commit`일 때만 `store_intent`·결정 행을 쓴다.
3. 같은 결정 시각 재실행은 intent·결정 행 수를 바꾸지 않는다(테스트).
4. 평단×1.01 미만 예상 청산은 intent가 아니라 기존 loss_cut 제안을 만든다(테스트, 제안 도구 호출 대체).
5. 쿨다운 심볼 진입은 intent 없음 + `blocked_by_cooldown` 결정 행.
6. 브로커 주문 모듈을 import하지 않는다(정적 테스트).
7. Dagu YAML은 `deploy/dagu/` 사본뿐이며 등록하지 않는다.
8. 4885 §7 지표를 sleeve report와 결정 행에서 계산하는 읽기 전용 보고 명령이 있다.

### 8.2 hk 5441 — 게이트웨이 Upbit forwarder (auto_trader, T3)

범위: 스키마 v2, 위임장 두 테이블 + 계좌 kill 행 + 저널 `shadowed` 상태(추가형 마이그레이션, GRANT 포함), `UpbitShadowForwarder`·`UpbitLiveForwarder`, `upbit_krw_tick`, intent 목록 GET, `scripts/execution_mandates.py`.

1. kis_mock 경로의 요청·응답·픽스처가 바이트 동일하다(기존 테스트 무변경 통과).
2. §2 I1~I10의 각 행에 실패 방향 테스트가 하나씩 있고 이름에 행 번호가 있다(`test_i<n>_*`).
3. shadow forwarder 모듈은 브로커·주문 모듈을 import하지 않는다(정적 테스트), DB가 shadow intent의 `broker_order_id` 기록을 거부한다.
4. live forwarder는 env `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`(기본 false) AND 위임장 `mode='live'` AND intent `execution_mode='live'` AND 계좌 kill `ACTIVE`일 때만 기존 Upbit 주문 서비스를 부르며, Upbit `identifier`에 child 키를 넣는다.
5. 손실매도 가드·쿨다운·loss_cut 코드와 상수는 diff에 없다.
6. 위임장 CLI는 preview 기본, `--commit` 필요, live 위임장 행은 §4.3 결정(D2)대로만 생긴다.
7. 마이그레이션 downgrade는 행이 있으면 거부한다.
8. `docs/design/1440-intent-gateway-contract.md`와 `docs/agent-contracts/broker-and-ledger-contracts.md` "Execution Intent Gateway" 절을 같은 PR에서 갱신하고, 루트 `AGENTS.md` 하드룰 4 예외가 필요하면 그 문구는 운영자 결정(D3) 인용과 함께만 넣는다.

### 8.3 hk 5442 — pi 운영 (런북·배치)

1. 게이트웨이 서버·Go 실행기를 pi(또는 D6이 정한 호스트)에서 루프백으로 띄우는 런북이 있다(재시작 정책 없음 vs 있음은 D6).
2. 기동 순서·정지 순서·kill 세 단계(§2 I7) 각각의 명령과 확인 방법이 있다.
3. shadow→live 전환 체크리스트(§9.2)와 롤백(§9.3) 절차가 명령 단위로 있다.
4. heartbeat·장애 알림 경로(기존 ops Discord, 결정 169 범위)가 정의되고 레인 이벤트를 쓰지 않는다.
5. 런북의 명령·플래그는 실제 argparse/flag와 대조하는 테스트가 있다(기존 플레이북 테스트 패턴).
6. env 값·Dagu 등록·재시작 정책은 런북에 "운영자 승인 후"로 표시되고 이 티켓이 적용하지 않는다.

### 8.4 fillwire hk 5439 — Upbit 실행기 (요구사항만)

1. §3.6 호환 변경: kis_mock child 직렬화·기존 골든 무변경, `quantity_decimal`·`shadowed`·`upbit_live` 지원.
2. 수량 고정소수(1e-8) 일반화가 kis_mock 배율 1에서 기존 테스트를 바꾸지 않는다.
3. §5 R1~R12 각각에 테스트가 있다(R4 추격 상한, R5 만료 margin, R6 신선도, R7 재연결·재시작 보류, R8 무재전송).
4. 새 `cmd/execupbit`는 기본 `Off`, `upbit_live` 전송은 별도 플래그가 있어야 하고, 브로커 API·DB를 import하지 않는다(정적 테스트).
5. Upbit 호가표 Go 구현이 auto_trader 공유 픽스처와 일치한다.
6. shadow 가상 체결 출력 파일 형식이 문서화된다.

## 9. 롤아웃

### 9.1 shadow 단계 종료 기준

1. shadow 위임장 + shadow sleeve로 **최소 14일**, 결정 잡 14회 이상 실행.
2. 모든 intent가 `shadowed` 또는 `refused`로 끝나고 `uncertain`/`sending` 0건, `live_order_ledger`의 해당 전략 행 0건.
3. 실행기 재시작·웹소켓 끊김 각 1회 이상을 겪고 R7 동작이 로그로 확인됨.
4. I1~I10 거부 사유가 의도한 경우에만 나타남(예상 밖 거부 0).
5. shadow 가상 체결 대비 결정 시각 호가의 차이(가상 슬리피지)가 기록되어 4885 §6 측정 형식으로 보고됨.

### 9.2 live 전환 (운영자 승인)

다음이 모두 있을 때 운영자가 전환한다(각각 별도 운영자 행위, at-go-executor "주지 않는 것"):
- 4885 manifest seal PR 병합, sleeve `live` 전환, live 위임장 행(§4.3), env `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED=true`, 실주문 경로 배포의 내용 기준 승인, Dagu 등록.
- 첫 2주는 매일 체결·원장·sleeve 장부 대조. 불일치 1건이면 롤백.

### 9.3 롤백

1. 즉시: 계좌 kill 행 `GLOBAL_FREEZE`(submit·amend 차단, cancel 허용) → 실행기 로컬 kill → 위임장 철회.
2. 열린 주문은 실행기 cancel 또는 운영자가 기존 경로로 취소. `uncertain` 행은 대사로만 해소.
3. 보유 청산은 4885 §8대로 카드 경로.
4. 코드 롤백은 env off로 충분하다. 마이그레이션 downgrade는 저널을 잃으면 키 재사용 위험이 있어 하지 않는다(E1 Rollback).

## 10. 운영자 결정 필요

| # | 결정 | 권고 | 근거 |
|---|---|---|---|
| D1 | 4885 첫 live 전에 shadow 단계를 거치고 종료 기준은 §9.1 | **예**, 14일·§9.1 다섯 항목 | at-go-executor 2의 "그림자 → 실주문" |
| D2 | live 위임장 행의 승인 방법 | **3795 §3.5 principal 대조 카드**. 그것이 늦으면 임시로 운영자 CLI `--commit` + `content_sha256`을 hk 결정 doc에 기록 | 3795 Q1의 "인증된 승인" 조건 |
| D3 | 하드룰 4 관점: 게이트웨이에 `upbit_live`(실계좌) forwarder를 추가하는 것 자체 | **예외로 기록**: 새 env 게이트(기본 false) + 위임장 + 계좌 kill + 호출별 confirm 헤더 유지, 기존 게이트는 완화하지 않음. AGENTS.md 하드룰 4에 결정 인용 한 줄 | E1이 지금 mock 전용이고 실계좌 forwarder는 "새 실행 표면" |
| D4 | 평단 아래 전략 청산 방식 | **A — loss_cut 카드 유지**(§2.1) | 가드 완화 없음 |
| D5 | (D4가 B일 때만) 위임장 결속 `strategy_exit_ctx` 가드 우회 | **지금은 아님**. live 데이터로 카드 지연 손실이 측정된 뒤 deviation record와 함께 별도 결정 | 3795 Q5 = B와 일관 |
| D6 | 게이트웨이·실행기 호스트와 재시작 정책 | **pi, 수동 기동(재시작 정책 없음) + heartbeat 감시**. 24시간 운용을 위해 재시작 정책이 필요하면 별도 결정 | E1 "스케줄러·유닛·재시작 정책 없음" 현행 |
| D7 | 결정 시각 해석 | **신호 = 09:00 KST 마감 일봉, 실행 = 14:20 슬롯** | 4885 §3.3 + at-next-go 2 |
| D8 | Dagu 결정·점검 잡 등록 | **shadow 단계 시작 때 등록 승인 요청**(shadow도 잡이 필요) | 하드룰 6 |
| D9 | 위임장 상한 값 | `per_intent_max_notional_krw`=480,000, `max_open_intents`=4, `daily_max_buy_notional_krw`=1,950,000, `price_band_bps`=30, `max_intent_ttl`=12h, `expires_at`=시작+91일 | 4885 §3.5·§11, 3795 30bp 시험값 |
| D10 | Claude 정기 매매 세션 축소 시점 | §1.2의 조건(live 첫 2주 통과) | at-go-executor 4 |

## 11. 알았지만 건드리지 않은 것

- E1 계약의 KIS mock 코디네이션 라우트·amend 차단(hk 3745) — 이 경로와 무관.
- 3795 바구니·회차 계획·낙폭 동결 — 최소판에 넣지 않았다.
- Upbit 계정 평단을 sleeve 단위로 분리하는 것 — 가드의 입력을 바꾸는 일이라 범위 밖(D4 근거).
- 키움 모의 KR을 같은 경로에 올리는 것 — at-go-executor 2의 다음 단계, 별도 설계.
