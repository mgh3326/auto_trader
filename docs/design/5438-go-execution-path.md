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

### 1.3 계좌 이름 대응

| 표면 | 값 |
|---|---|
| 게이트웨이 intent `account` | `upbit_live` (새 값) |
| sleeve `(account_mode, market)` | `("upbit","crypto")` (`SLEEVE_ACCOUNT_MARKETS`, 4885 §2.2-5) |
| 원장 식별 `(broker, account_mode)` | `("upbit","live")` (`vocabulary.LEDGER_IDENTITY`) |
| 주문 원장 | `review.live_order_ledger` (US·crypto live, ROB-407) |

5441은 이 대응을 `app/services/execution_intent_gateway/` 안 상수 하나로 두고 세 표면 모두 그 상수에서 읽는다.

### 1.4 sleeve 귀속 (선행 변경) [사실 → 요구]

[사실] sleeve 장부는 체결을 **sleeve 자기 제안 rung의 브로커 주문번호**로만 귀속한다(`app/services/strategy_sleeves/store.py:15-25`). 직접 주문은 sleeve를 받지 않는다(승인·shadow 계약 "전략 슬리브"). 그래서 게이트웨이 주문은 지금 그대로는 sleeve 장부·손실 한도·예산·채점에 **보이지 않는다**.

요구(5441 소유): sleeve 귀속에 두 번째 출처를 더한다 — `review.execution_child_requests`에서 `intent.sleeve_id`가 그 sleeve인 submit child의 `broker_order_id`(Upbit 주문 UUID)와 일치하는 in-effect `execution_ledger` 행(`("upbit","live")`, 같은 심볼·side, intent 저장 이후). 에피소드 root는 rung lineage 대신 intent의 `decision_ref`다. 기존 rung 귀속은 바이트 동일하게 남는다. 이 변경이 들어가기 전에는 I4·I8·I9와 4885 §7 채점이 성립하지 않으므로 live 전환 불가다. 또 sleeve 생성 자체가 4885 Q7의 `CANARY_RULES` 규칙 id 추가 PR(별도, 5440 선행)에 막혀 있다.

### 1.5 Upbit 체결 증거

- `filled_qty`(E1 `/intents/<id>` `view`)와 `/fills`는 `upbit_live` intent에 대해 in-effect `execution_ledger` 행 중 `broker='upbit'`, `account_mode='live'`, `broker_order_id`가 저널의 submit child 번호와 같은 것만 센다. Upbit 주문번호는 UUID라 날짜 identity가 없다(ROB-1379 supersede 규칙 그대로).
- `uncertain`/`sending` 해소: 기존 `live_reconcile_orders`(ROB-407)가 쓰는 `live_order_ledger` 행과 위 체결 행만 증거다. 게이트웨이 질의는 지금처럼 브로커를 부르지 않는다. Upbit `identifier`(= child 키)로 찾은 주문은 존재 증거이고, 못 찾은 것은 부재 증거가 아니다.
- **지연**: Upbit `execution_ledger` 대사는 Dagu 30분 주기다(`deploy/dagu/at-batch2`, hk 4896). fillwire가 Upbit 체결을 실시간 ingest하는지는 확인하지 않았다 [추정 — 없다고 가정]. 그래서 실행기는 체결 확인을 기다리며 추격하지 않는다(§5 R4).

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
| I9 | **매도는 보유 이상 못 함(현물 롱 전용)** | intent `inventory`(소수), sleeve 귀속 보유 | `min_position_after < 0` 불가; 매도 intent 수량 ≤ 보유(live = sleeve 귀속 보유, shadow = §3.6 shadow 장부) | 스키마(기존 inventory 규칙의 소수판), `store_intent`가 장부 대조 | §1.4 sleeve 귀속, §3.6 | 저장 거부 `inventory_constraint_exceeded` |
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

### 2.2 경계 규칙 (리뷰 1회차 반영, 구속력 있음)

아래 규칙은 §2 표와 같은 효력이며 표와 충돌하면 이 절이 우선한다. 번호(B1…)는 티켓 Acceptance가 인용한다.

| # | 규칙 | 소유 |
|---|---|---|
| B1 | **매수 지정가와 기존 매수 가드.** `_preview_buy`는 지정가가 현재가(Upbit 마지막 체결가)보다 높으면 거부한다(`order_validation.py:1118`). 가드는 유지한다: 실행기는 최우선 매도호가 ≤ 마지막 체결가(같은 웹소켓 `trade` 스트림)일 때만 그 매도호가로 submit하고, 아니면 기다린다(R2). 기다리다 `CancelAfter`가 지나면 submit 없이 `DoneDeadline`. 가드 완화는 §10 D14 | 5439, 5441 |
| B2 | **승인한 가격 = POST 가격.** 기존 crypto 경로는 `adjust_price_to_upbit_unit`로 가격을 다시 맞춘다. Upbit forwarder는 그 함수의 결과가 게이트웨이가 검증한 `limit_price_krw`와 다르면 **보내기 전에** 거부한다(`refused/price_normalization_mismatch`, 미전송 증명). 수량도 같은 방식으로 POST 본문 직전 값과 비교한다. 게이트가 승인한 (가격, 수량)과 POST 본문의 일치가 계약이다 | 5441 |
| B3 | **결과 기록 실패 후 결속.** `finish` 실패로 child가 `sending`·`broker_order_id=NULL`이면 귀속·`/fills`는 `identifier`(= child 키)로 결속된 유일한 `live_order_ledger`/Upbit 주문 증거를 사용한다. 결속이 없거나 둘 이상이면 그 child는 **미해결**이고 sleeve 사용액·보유는 `unmeasured`(0 아님) → 매수 intent 거부 | 5441 |
| B4 | **응답 유실 대사 주체.** 새 읽기 전용 명령 `scripts/execution_intent_upbit_resolve.py`(preview 기본, `--commit`)가 `sending`/`uncertain` Upbit child마다 `fetch_order_by_identifier(child 키)`를 부르고, 찾으면 기존 `live_order_ledger` 서비스로 행을 기록한다(하드룰 5). 못 찾음·조회 실패는 미전송 증거가 아니며 child는 미해결로 남는다. 운영자 수동 복구 절차는 5442 런북. 이 명령은 점검 잡과 같은 슬롯에서 돈다(D8 승인 범위) | 5441, 5442 |
| B5 | **미해결 주문은 슬롯을 넘어 예약을 유지.** 같은 sleeve·심볼에 브로커 종결과 체결 수량이 증명되지 않은 child(상태 `sending`·`uncertain`, 또는 종결 증거 없는 `accepted`)가 있으면 `store_intent`가 새 intent를 거부한다(`unresolved_prior_order`). intent 만료·슬롯 변경으로 풀리지 않는다 | 5441 |
| B7 | **매도에는 명목 상한이 없다.** `per_intent_max_notional_krw`·I2 명목 상한은 매수에만 적용한다. 매도 intent의 상한은 수량뿐이고 증명된 보유 이하(I9)다 — 상승한 포지션도 전량 청산 가능 | 5441 |
| B9 | **평단 미증명 = 자동 매도 없음.** 게이트웨이는 매도 child마다 계정 평단을 읽어 양의 유한값이고 읽기가 성공했을 때만 비교한다. 누락·0·읽기 실패면 `refused/avg_price_unproven`(전송 0) → B13 카드 경로 | 5441 |
| B10 | **철회 fence.** 철회 writer와 child 예약은 같은 위임장 행 `FOR UPDATE`를 잡는다(S2와 같은 잠금). 철회 커밋 이후 예약되는 child는 전부 거부된다. 철회 커밋 이전에 예약된 child는 막을 수 없다(pre-send 재확인은 best-effort, 3795 §4.6과 같음) — `revoke` 명령은 그런 child 목록을 출력해 운영자가 취소·검토한다. §4.3의 "즉시"는 이 의미다 | 5441 |
| B11 | **전략 코드 SHA.** v2 intent에 필수 불변 필드 `strategy_code_sha`(40자 hex)를 둔다. `store_intent`와 예약이 위임장 값과 대조한다 | 5440, 5441 |
| B12 | **실제 계좌 결속.** 위임장에 `account_ref`(Upbit 자격증명 식별자의 HMAC 지문, 수동 계좌 스냅샷 hk 3800과 같은 방식)를 저장·해시한다. forwarder는 전송 직전 설정된 자격증명으로 같은 지문을 계산해 다르면 `refused/account_mismatch`. 지문을 계산할 수 없으면 live 위임장을 만들 수 없다 | 5441 |
| B13 | **손실매도 거부의 래치.** 청산 결정은 결정 행(B15)에 `exit_pending`으로 래치된다. 게이트웨이 `loss_sell_requires_card`·`avg_price_unproven` 거부는 저널에 남고, 다음 점검 잡이 래치된 청산을 다시 읽어 B14 경로로 넘긴다. 래치는 포지션 0이 증명되거나 카드가 생성될 때만 풀린다(가격 회복으로 풀리지 않음) | 5440 |
| B14 | **loss_cut 카드 증거.** 기존 loss_cut은 72시간 이내 `retrospective_id`·`approval_issue_id`와 허용 caller가 필요하다. 1단계에서 잡은 이것을 만들지 않는다: 래치된 손실 청산이 있으면 ops Discord 알림 + 결정 행 `needs_operator_card`를 남기고, 운영자 세션이 기존 절차로 카드를 만든다. 잡이 증거를 만들고 직접 제안하는 것은 §10 D15 | 5440, 5442 |
| B15 | **결정 저장소.** `review.decision_evidence`에는 신호값·σ̂·no-op 사유·intent id 열이 없고 매 호출 새 행이다. 5440이 추가형 마이그레이션으로 `review.upbit_trend_decisions`(유일 키 `(strategy, symbol, decision_at, action)`, 신호·σ̂·크기·사유·intent payload·`intent_id`·래치 상태, append-only + 래치 상태 전진만)와 유일 writer 서비스를 만든다. §6.4의 `decision_evidence` 기록은 선택적 연결로 내려간다 | 5440 |
| B16 | **재실행 멱등.** 잡은 먼저 결정 행을 찾고 있으면 그 행에 저장된 intent payload를 그대로 `store_intent`한다(호가를 다시 읽지 않음). 결정 행과 intent는 한 트랜잭션에 쓴다 | 5440 |
| B17 | **shadow 판정.** shadow intent의 sleeve 판정은 `strategy_sleeves.gate`가 아니라 shadow 장부(§3.6)로 계산하는 별도 함수 `evaluate_shadow_sleeve`(예산·손실·보유)이며, shadow sleeve 상태(`shadow`)를 받는다. 이 함수의 결과는 `shadowed` 기록만 허용하고 live forwarder를 고를 수 없다 | 5441 |
| B18 | **shadow 원가는 FIFO.** §3.6의 진입가는 시간순 가상 체결을 기존 sleeve와 같은 FIFO로 접어 남은 lot의 원가로 계산한다(청산된 에피소드의 매수는 빠진다) | 5441 |
| B19 | **잡의 입력 표면.** 호가: 무인증 공개 REST orderbook(`/v1/orderbook`) 읽기. 계정 평단: 기존 인증 읽기 `fetch_my_coins`(읽기 전용, 주문 모듈 import 없음). 둘 중 하나라도 실패하면 그 종목은 intent 없음 + 결정 행 사유(`input_unavailable`), 열린 청산은 B13 래치 유지 | 5440 |
| B20 | **fillwire fill·view 검증.** `LedgerFill.Execution`의 가격 상한(10,000,000)·수량 상한(1,000,000)은 계좌별로: `upbit_live`는 가격 ≤ 10,000,000,000 KRW, 수량은 1e-8 고정소수. `/intents/<id>` `view`의 `filled_qty`·`order.qty`도 v2에서 decimal 문자열이고 계좌별로 파싱한다 | 5439, 5441 |

### 2.3 경계 규칙 (리뷰 2회차 반영, §2.2보다 우선)

| # | 규칙 | 대체·보완 | 소유 |
|---|---|---|---|
| B21 | **shadow는 카드·제안을 만들지 않는다.** B13·B14·B9의 래치·알림·카드 지시는 `execution_mode='live'` 결정에만 적용한다. shadow의 거부(`loss_sell_requires_card` 등)는 결정 행에 `shadow_exit_refused`로 기록만 하고, 운영자 카드 요청·알림을 내지 않는다(공유 계좌의 실제 코인을 팔 위험 차단) | B13·B14 | 5440 |
| B23 | **계좌 결속은 운영자가 확인한 실계좌에.** B12 보완: Upbit 공개 API가 계좌 식별자를 주는지 확인하지 않았다 [추정 — 없다고 가정]. 그래서 live 위임장 승인 요청은 자격증명 지문과 함께 그 자격증명으로 읽은 계좌 잔고 요약(통화·수량, 금액 포함)을 승인 카드에 싣고, 운영자가 그것이 의도한 계좌임을 승인으로 확인한다. 자격증명은 계좌 신원의 대체물이 아니므로 이 확인 없이는 live 위임장을 만들지 않는다(§10 D17) | B12 | 5441 |
| B25 | **래치 해제 조건.** B13 대체: `exit_pending`은 (a) 그 leg 보유 0이 체결 증거로 증명되거나 (b) 운영자가 `scripts/upbit_trend_job.py resolve-exit`(preview 기본, 사유 필수, 기록 남김)로 해소할 때만 풀린다. 카드 생성·만료·미체결은 래치를 풀지 않고, 래치 행은 카드 상태(활성/만료/체결)를 따로 기록한다 | B13 | 5440 |
| B27 | **평단 미증명은 조사 상태.** B9 보완: 기존 loss_cut 확인은 평단 0 이하를 거부하므로(`revalidation.py:1139-1150`) 카드로도 청산할 수 없다. 평단 미증명이면 결정 행 `needs_operator_investigation` + ops 알림(live만)이고 래치는 유지한다. 운영자는 평단 데이터를 바로잡거나 기존 경로 밖에서 처리한 뒤 B25(b)로 해소한다. 가드는 바꾸지 않는다 | B9 | 5440, 5442 |
| B28 | **호가 정규화는 Upbit 호가표 하나.** 기존 `adjust_price_to_upbit_unit`은 1,000~5,000원 구간을 5원 단위로 맞춰 공식 호가표(1원)와 다르다. 게이트웨이 출처 주문은 `upbit_krw_tick`으로 이미 검증된 가격이므로 forwarder는 그 함수를 거치지 않는 인자(`price_prevalidated=True`)로 기존 crypto place 함수를 부르고, POST 직전 본문 가격이 검증값과 같은지 B2대로 확인한다. 기존 함수의 전역 수정은 이 설계 범위 밖(§11) | B2 | 5441 |
| B29 | **증명된 미전송 상태.** 저널에 새 종결 상태 `not_sent`를 더한다: `sending → not_sent`는 forwarder가 브로커 함수 호출 전 또는 `pre_send_hook` 예외로 POST 전에 멈췄을 때만(같은 트랜잭션에 사유 `price_normalization_mismatch`·`mandate_revoked`·`account_mismatch`). 재생은 `refused`와 같은 답(사유 포함)을 주고, `not_sent`는 노출·B5 예약에 포함되지 않는다. DB 트리거가 다른 출처의 이 전이를 거부한다 | B2, E1 Open item | 5441 |
| B30 | **취소 child 복구는 대상 주문으로.** B4 보완: `cancel` child는 `target_idempotency_key`의 원 submit identifier/UUID로 주문을 조회해, 취소·종결 상태와 체결 수량이 증명되면 해소한다. 조회 실패·부재는 미해결 유지이고 재전송 권한이 아니다 | B4 | 5441 |
| B31 | **슬롯당 결정 하나.** B15 대체: 유일 키는 `(strategy, symbol, decision_at)`이고 `action`은 열이다. 잡은 먼저 그 키로 claim 행을 INSERT(충돌 시 기존 행 사용)한 뒤 결정을 채우므로 동시 실행의 두 번째는 첫 결정을 따른다 | B15 | 5440 |
| B32 | **재실행 재생 규칙.** B16 대체: 결정 행에 intent가 없으면 그대로 반환. intent id가 이미 `review.execution_intents`에 있으면 `store_intent`를 부르지 않고 성공 no-op(만료 검사 생략, 새 전송 권한 없음). 없고 만료 전이면 저장한다. 없고 만료 후면 결정 행 `expired_before_store`로 끝낸다 | B16 | 5440 |
| B33 | **읽기 표면의 전이 import.** `app.services.brokers.upbit` 패키지 초기화가 `orders`를 import하므로 잡은 그 패키지를 import하지 않는다. 5440이 계좌·호가 읽기만 하는 모듈을 패키지 밖에 두거나 5441이 패키지 초기화를 분리하고, 정적 테스트는 직접 import가 아니라 **전이 import 폐쇄**에서 주문 모듈 부재를 확인한다(retro/v1 가드와 같은 방식) | B19, §8.1-6 | 5440 |
| B34 | **소수 체결가.** B20 보완: `upbit_live` 체결가는 decimal 문자열이고 fillwire는 계좌별로 정수(kis_mock, `IsInt` 유지)와 정확한 유리수/고정소수(Upbit, 1e-8)를 고른다. 명목 = 수량 × 가격을 정확히 계산한다. kis_mock 계약·골든은 그대로 | B20 | 5439, 5441 |

### 2.4 단순화 (리뷰 4회차, 설계 세션 결정 — §2.2·§2.3보다 우선)

리뷰가 경계 규칙을 계속 늘린 근본 원인은 (1) 카드 경로와 sleeve를 공유한 것, (2) 달력 일별 상한이었다. 그래서 다음 넷으로 단순화하고, 그것에 밀려난 규칙(B6·B8·B22·B24·B26·B35·B36)과 위임장 `daily_max_buy_notional_krw`는 삭제했다.

| # | 규칙 | 소유 |
|---|---|---|
| S1 | **Go 경로 전용 sleeve.** 위임장은 생성 시점부터 Go 경로로 고정된 **빈** sleeve(새 epoch, S7)에만 붙는다. 이 sleeve의 주문은 게이트웨이 child와 S5의 청산 인계 카드 둘뿐이고, 둘 다 위임장의 `account_ref`로 대조된다. 일반 카드 제안은 이 sleeve를 태그할 수 없다(S7 DB 트리거). 평단 아래 청산(I10)은 손실매도 가드를 바꾸지 않으므로(하드룰 4) 게이트웨이로 보낼 수 없고, S5 청산 인계로만 카드가 된다 | 5440, 5441 |
| S2 | **매도 intent는 (sleeve, 심볼)당 하나.** 종결되지 않은(저장됨·child가 `sending`/`uncertain`/종결 증거 없는 `accepted`) 매도 intent가 같은 sleeve·심볼에 있으면 새 매도 intent를 거부(`open_sell_intent_exists`). 그 매도 intent의 수량 ≤ 증명된 자기 보유(live = 귀속 체결, shadow = §3.6). 검사는 `store_intent`와 child 예약에서 같은 잠금(위임장 행 `FOR UPDATE`, B10과 같은 잠금) 아래 한다 | 5441 |
| S3 | **달력 일별 상한 없음.** 상한은 intent 단위(명목·수량·가격 경계·만료, I2·I3) + sleeve 예산(I4) + sleeve 누적 손실 한도(I8)뿐이다. 4885에는 이것으로 충분하다 | 5441 |
| S4 | **live·shadow 구분과 `not_sent`.** sleeve 상태 검사: `execution_mode=live` 전송은 sleeve `live`에서만, `execution_mode=shadow`는 sleeve `shadow`에서 가상 매수·매도 모두 허용(B17). sleeve가 `paused`/`stopped`이면 잡은 자동 매도하지 않고, 운영자 청산은 S6 하나로만 받는다(매수는 거부). E1 조회의 lineage·복구 계산은 `not_sent`를 `refused`처럼 제외하고 children 감사 목록에만 남긴다; 응답 유실 뒤 재조회가 `not_sent` submit을 working order로 돌려주지 않는 사례를 E1 계약 표에 추가한다 | 5441 |

### 2.5 청산 인계·운영자 청산·경로 고정 (리뷰 5회차, 설계 세션 인수 — §2.2~§2.4보다 우선)

리뷰 5회차의 세 지적은 하나의 빈틈에서 나왔다: S1이 평단 아래 청산을 "경로 밖"으로 보냈고, 그 경계가 정의되지 않았다. 손실매도 가드는 바꾸지 않으므로(하드룰 4) 그 청산은 카드일 수밖에 없다. 그래서 카드가 이 sleeve에 닿는 유일한 길을 아래처럼 좁게 정의한다. B13·B25·B27의 "경로 밖 처리"·`resolve-exit` 서술은 이 절로 대체된다(래치·조사 상태 자체는 유지).

| # | 규칙 | 소유 |
|---|---|---|
| S5 | **청산 인계(exit handoff).** (a) 게이트웨이가 매도를 `loss_sell_requires_card`로 거부하거나 B13 래치가 걸리면, 같은 위임장 잠금 아래 `review.execution_exit_handoffs(sleeve_id, symbol, mandate_id, state, opened_at)` 행을 `open`으로 쓴다(유일: 같은 sleeve·심볼에 열린 인계 하나). (b) 인계가 `open`인 동안 그 sleeve·심볼의 `store_intent`와 submit·amend 예약은 매수·매도 모두 `exit_handoff_active`로 거부된다(cancel은 허용). 실행기는 그 심볼의 살아 있는 주문을 cancel한다. (c) 인계는 그 sleeve·심볼의 모든 child가 브로커 종결과 체결 수량의 **양성 증거**(B5·B30과 같은 증거 규칙)를 가질 때만 `ready_for_card`로 간다 — 취소 결과 불명·`uncertain`이 하나라도 있으면 `open`에 머문다. (d) 카드는 `ready_for_card` 인계에서만, **인계당 활성 제안 하나**로 만든다: 같은 잠금 아래 `UPDATE ... SET state='card_claimed', proposal_id=<새 id> WHERE state='ready_for_card'`가 한 행을 바꾼 경우에만 그 제안을 INSERT한다(두 번째 세션은 0행 → 거부). 수량 = 그 시점에 증명된 자기 보유이고, 그 수량 예약은 이 제안의 전송·종결 증거까지 유지된다. 카드 제안은 **sell rung 정확히 하나**만 가진다: 부모·자식에 걸친 지연(DEFERRABLE INITIALLY DEFERRED) 제약 트리거가 커밋 시 인계 제안의 rung 수 = 1, side=sell, rung 수량 ≤ 인계 예약 수량을 확인하고 아니면 트랜잭션 전체를 거부한다(rung을 나중에 INSERT해도 빠져나갈 수 없다). 카드가 종결(체결·부분 체결 후 취소/만료·무체결 만료·취소·거절)되면 브로커 종결과 누적 체결량의 양성 증거를 확인한 뒤 체결분을 rung으로 sleeve에 귀속하고, **증명된 잔여 자기 보유가 0이면 `closed`, 0보다 크면 `ready_for_card`로 돌아가 새 claim**을 받는다. 종결·누적 체결량을 증명하지 못하면 `card_claimed`에 머물고 그 심볼은 막힌 채 운영자 조사 대상(B27과 같은 알림)이다. 위임장 `account_ref` 대조(B12)는 카드 생성, 카드 dispatch, 승인 콜백의 재검증(`revalidate_and_submit`), **실제 POST 직전**(`pre_send_hook`) 네 번 하고, 지문을 계산할 수 없으면 전송하지 않는다(카드 발송 뒤 자격증명이 바뀐 경우 차단). 제안 행은 `exit_handoff_id`를 가진다. (e) 인계의 `closed` 전이는 위 (d)의 "증명된 잔여 0"일 때뿐이고, 카드 체결은 그 제안 rung으로 sleeve에 귀속된다(기존 rung 귀속, 계좌는 (d)에서 증명됨). 인계가 `closed`되기 전에는 그 심볼에 새 intent가 없다. 리뷰 r4627665293을 닫는다 | 5440, 5441 |
| S6 | **운영자 청산(operator exit).** `scripts/execution_mandates.py exit --mandate ... --symbol ... --max-quantity ...`(preview 기본, `--commit`, 사람 actor만)가 `review.execution_operator_exits(mandate_id, symbol, max_quantity, reason, created_by, expires_at ≤ 1시간)` append-only 행을 쓰고, 그 행을 가리키는 매도 intent(`origin='operator_exit'`)를 저장한다. 이 intent의 `expires_at`은 현재보다 미래이고 운영자 청산 행의 `expires_at` 이하여야 하며, §3.2·I3의 "위임장 만료 이전" 비교는 `origin='operator_exit'`이고 유효한 운영자 청산 행을 가리킬 때만 스키마와 DB 트리거 양쪽에서 면제된다(만료된 위임장에서도 청산 intent를 저장할 수 있게). 이 intent에 한해 `store_intent`·child 예약·전송 직전 검사가 **sleeve 상태 검사 대신** 다음을 본다: 운영자 청산 행 존재·미만료, 매도뿐, 수량 ≤ 행의 `max_quantity` 및 증명된 자기 보유, S2(살아 있는 매도 하나), S5(열린 인계 없음), B12 계좌 대조, kill (a)(b)(d). 위임장 철회·만료·sleeve `paused`/`stopped`는 이 intent를 막지 않는다(보유를 남기지 않기 위함). `strategy_sleeves.gate.evaluate_sleeve_gate`는 이 intent에 쓰지 않고, 별도 함수 `evaluate_operator_exit`가 위 조건을 한 곳에서 판정한다 — `store_intent`·예약·pre-send hook이 같은 함수를 호출한다. 평단 아래면 운영자 청산도 S5 인계로 간다. 리뷰 r4627665327을 닫는다 | 5441 |
| S7 | **경로는 sleeve 생성 시 고정.** `review.strategy_sleeves`(정체성 불변 테이블)에 `execution_path text NOT NULL DEFAULT 'card' CHECK IN ('card','go')`를 더하고(추가형 마이그레이션, 기존 행은 `card`) 생성 뒤 변경을 기존 불변 트리거가 거부한다. 위임장은 `execution_path='go'`이고 귀속 이력이 0인 sleeve에만 결속된다. `review.order_proposals` INSERT 트리거: `sleeve_id`가 `go` sleeve를 가리키면 `exit_handoff_id`가 `card_claimed` 상태이고 `proposal_id`가 이 제안 id인 인계를 가리키며 side=sell·수량 ≤ 인계 수량일 때만 허용, 그 밖은 거부. 경로가 생성 시점에 정해지므로 "위임장 결속 직전에 카드가 생기는" 경쟁 창이 없다 — 위임장보다 먼저 생긴 `go` sleeve에도 일반 카드는 처음부터 붙을 수 없다. 리뷰 r4627665310을 닫는다 | 5441 |

### 2.6 5441이 닫을 열린 요구 (리뷰 7회차 범위 축소, 설계 세션 결정)

S5(d)·(e)의 카드 종결 판정에서 남은 두 경계를 구현 티켓 5441의 요구로 고정한다. §2.5와 충돌하면 이 절이 우선한다.

| # | 요구 | 실패 방향 |
|---|---|---|
| O1 | **브로커 전송 없이 끝난 인계 카드.** 승인 전 만료나 거절처럼, 내구 승인·전송 경계(승인 콜백 durable inbox의 코어 진입 마커와 송신 원장 행)가 "브로커 전송 없음"을 증명할 때만 인계를 `ready_for_card`로 되돌린다. 코어 진입 이후 결과가 불명이면(W5 `internal_error`·`dead_letter` 등) 전송 여부를 모르므로 `card_claimed`를 유지한다 | 코어 진입 후 불명 → `card_claimed` 유지, 새 claim 거부 |
| O2 | **잔량 재claim의 증거.** 잔량 재claim(`ready_for_card` 복귀)은 그 제안 rung에 귀속된 in-effect authoritative `execution_ledger` 체결 합이 브로커 누적 체결 수량과 **정확히 같을 때만** 한다. 다르거나 어느 한쪽을 읽지 못하면 인계는 `card_claimed`에 머물고 그 심볼의 sleeve 장부는 `unmeasured`로 표시된다(0으로 보지 않음) | 체결 합 ≠ 브로커 누적 → `card_claimed` + `unmeasured` |

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
| `max_notional_krw` | 정수 KRW, 5,000 이상(Upbit 최소 주문, dots §2). 매수는 위임장 `per_intent_max_notional_krw` 이하, 매도는 위임장 상한을 적용하지 않음(B7) |
| `strategy_code_sha` | 필수 40자 hex, 위임장 값과 같아야 함(B11) |
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

### 3.6 shadow 장부

shadow는 원장 행이 없으므로 Python 쪽에 가상 보유가 필요하다. 그렇지 않으면 전략 잡이 "롱" 상태를 영영 보지 못해 손절 점검·청산이 검증되지 않는다.

- 정의: `shadowed` submit child 하나 = 그 child의 `quantity_decimal`이 그 child의 `limit_price_krw`에 저널 기록 시각에 전량 체결된 것으로 본다(taker 지정가라는 가정). shadow 보유 = intent의 sleeve·심볼별 `Σ buy − Σ sell`, 진입가 = 그 매수 child들의 가중 평균.
- 소유: 5441이 읽기 함수(`shadow_positions(sleeve_id)`, 저널만 SELECT)를 제공하고, 5440은 `execution_mode='shadow'`일 때 sleeve 장부 대신 이것을 읽는다. 쓰기 없음.
- 한계: 가상 체결은 체결률·슬리피지를 과대평가한다. shadow 단계는 경로 검증이지 성과 판정이 아니며 4885 §7 채점에 넣지 않는다. 실행기의 R9 가상 체결은 교차 확인용이다.

### 3.7 fillwire E1 클라이언트 호환 (5439 요구사항)

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

`origin='operator_exit'` 매도는 아래 대신 S6의 `evaluate_operator_exit`를 쓴다. 그 밖의 intent는 `store_intent`와 child 예약(submit) 두 번: 열린 청산 인계 없음(S5) · 위임장 존재 · 미철회 · `now < expires_at` · `mode == intent.execution_mode` · 심볼·sleeve·전략·`strategy_code_sha` 일치 · intent 상한 ⊆ 위임장 상한 · sleeve 버전이 현재 live/shadow 상태와 위임장 `mode`에 맞음(live 위임장은 sleeve `state='live'`). 예약 시 검사는 intent 행 잠금 아래에서 읽는다(E1 Expiry 판정과 같은 위치). 전송 직전 재검사(pre-send hook)는 E1 계약 Open items "Expiry and kill at the wire"를 Upbit forwarder에서 닫는다: Upbit place 경로의 `pre_send_hook`에서 철회·kill·만료를 다시 읽고 실패면 `proven_not_sent`로 기록한다.

### 4.5 3795와의 관계와 hk 5286

- 이 최소판은 3795의 바구니·회차 계획·업종 상한·낙폭 동결·활성화 프로필을 **갖지 않는다**. 단일 sleeve·단일 전략용이다.
- 3795 T4 구현 시 마이그레이션: 각 `execution_mandates` 행 → `mandate_terms`의 `terms_version='execution-mandate/min-v1'` 행으로 옮기거나, `mandate_terms`가 이 테이블을 참조하게 한다. 어느 쪽인지는 3795 T4 브리프가 정한다.
- hk 5286(T4c fence 메커니즘)은 여기서 정하지 않는다. 최소판이 필요한 것은 "전송 직전 위임장 재확인" 하나이고 그것은 §4.4의 pre-send hook으로 충분하다. 5286의 결론이 다른 메커니즘이면 5441은 그 결론을 따른다.

## 5. Go 실행기 타이밍 정책 (hk 5439 요구사항)

| # | 요구사항 |
|---|---|
| R1 | 호가 공급원은 `internal/upbit/reader.go`의 공개 웹소켓 `orderbook` 스트림을 재사용한다(무인증). 주문용 private 웹소켓·REST 키를 쓰지 않는다 |
| R2 | **진입(buy)**: submit 지정가 = 최우선 매도호가(taker, 4885 §4.3·Q10). 단 그 값 ≤ 마지막 체결가일 때만(B1). 그 가격이 intent 경계(`limit_price_krw × (1+bps)`)를 넘으면 보내지 않고 기다린다. 수량 = min(intent 잔량, floor(남은 명목 / 가격, 8자리)) |
| R3 | **청산(sell)**: 지정가 = 최우선 매수호가. 경계(`limit × (1−bps)`) 아래면 보내지 않는다. 평단×1.01 미만 청산은 게이트웨이가 거부하므로(I10) 실행기는 `loss_sell_requires_card`를 받으면 그 intent를 `DoneRefused`로 끝내고 재시도하지 않는다 |
| R4 | **추격 없음(1단계)**: intent당 submit 1회. E1 상한 규칙상 cancel은 노출을 풀지 않고(E1 "How caps count"), 체결 증거는 최대 30분 늦으므로(§1.5) 같은 intent 안의 재submit은 상한에 막힌다. 접수 후 `CancelAfter`(권고 120초)에 잔량이 있을 수 있으면 cancel 하고 `DoneDeadline`으로 끝낸다. 남은 몫은 다음 슬롯의 새 intent다. 추격(취소 확인 증거로 노출 해제)은 §10 D11 |
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
- 청산: 수량 = sleeve 귀속 보유 전량. 가격 = 최우선 매수호가. 평단×1.01 미만이 **예상되면**, live 결정은 intent 대신 바로 S5 청산 인계를 연다(카드는 인계에서만) — 게이트웨이 거부를 기다리지 않는다. shadow 결정은 B21대로 결정 행만 남기고 인계·카드·제안을 만들지 않는다. 판정 실패(평단 읽기 실패)는 intent를 내고 게이트웨이 판정에 맡긴다.
- 쿨다운 중인 심볼의 진입은 intent를 만들지 않고 `blocked_by_cooldown`.

### 6.4 결정 로그

4885 §4의 "레포 밖 운영자 파일" 대신 결정 행을 DB에 둔다: 기존 `review.decision_evidence`(hk 4182, `DecisionEvidenceService`만 쓰기)에 결정 시각마다 종목별 한 행 — 동작이 있으면 `decision_kind='direct_order'`, no-op이면 `declined`로 둔다 [추정 — `decision_kind`가 `proposal`/`direct_order`/`declined` 셋뿐(`app/services/decision_evidence.py:22`)이고 intent 경로 전용 값이 없다. 의미가 맞지 않으면 5440이 추가형으로 값을 더한다](신호값, σ̂, 크기, 동작 또는 no-op 사유, 생성한 intent id). intent `decision_ref`가 그 행을 가리킨다. no-op도 행이 있어야 분모가 생긴다.

### 6.5 손절 점검이 매도 intent가 되는 방식

점검 잡은 열린 leg마다 현재가 ≤ 진입가×0.90이면 `action=exit_hard_stop` 매도 intent(같은 멱등 키 규칙, `decision_at`=점검 슬롯 시각)를 낸다. 이미 열린 매도 intent가 있으면 새로 내지 않는다. 진입가는 sleeve 장부의 귀속 체결 평균(계정 평단이 아님).

## 7. 4885 개정

이 PR은 4885에서 다음 줄만 고친다(§3 규칙과 §11 값은 그대로):

| 4885 위치 | 바뀌는 것 |
|---|---|
| §4 머리 | "카드 경로"는 이 문서의 의도 경로로 대체된다는 안내 줄 추가. 기존 §4 본문은 의도 경로가 live로 열리기 전까지의 대체 경로이자, 평단×1.01 미만 또는 평단 미증명 청산(§2.1 A, B9·B27, S5 청산 인계)에서 계속 쓰는 loss_cut 카드 절차로 남는다 |
| §9 첫 줄·"건별 주문 승인" 줄 | "자동 집행·위임장 미승인"을 "자동 집행은 5438 의도 경로 + 최소 위임장으로 개정(at-go-executor), 그 경로의 shadow→live 전환·위임장 행 승인은 별도 운영자 행위"로 바꾼다 |
| §9 "스케줄러" 줄 | 결정·점검 Dagu 잡 등록도 별도 승인임을 명시 |

**봉인 보류.** 4885 manifest는 이 경로(§10 운영자 결정)가 확정되고 shadow 단계 종료 기준(§9.1)을 통과할 때까지 seal하지 않는다(draft, `k=0` 유지). 봉인 구간 시작은 live 전환 이후다.

## 8. 티켓 경계와 닫힌 Acceptance

### 8.1 hk 5440 — 전략 잡 (auto_trader)

선행: 4885 Q7 `CANARY_RULES` 규칙 id PR, 5441 병합. 범위: `app/services/upbit_trend/`(신호·크기·결정·intent 작성), `scripts/upbit_trend_job.py`(`decide`/`stop-check`, preview 기본), Dagu 사본 2개(미등록). 5441의 스키마 v2·위임장 검사에 의존.

1. 4885 §3.3~3.5 규칙을 순수 함수로 구현하고, 경계값(NO_SIGNAL, 바닥 50,000, σ̂ 스케일, 8자리 내림) 테스트가 있다.
2. `decide`·`stop-check`는 기본 preview(쓰기 0)이고 `--commit`일 때만 `store_intent`·결정 행을 쓴다.
3. 같은 결정 시각 재실행은 intent·결정 행 수를 바꾸지 않는다(테스트).
4. live 결정의 평단×1.01 미만 예상 청산은 intent가 아니라 S5 청산 인계를 연다. shadow 결정은 결정 행만 남기고 인계·제안을 만들지 않는다(둘 다 테스트).
5. 쿨다운 심볼 진입은 intent 없음 + `blocked_by_cooldown` 결정 행.
6. 브로커 주문 모듈을 import하지 않는다(정적 테스트).
7. Dagu YAML은 `deploy/dagu/` 사본뿐이며 등록하지 않는다.
8. §2.2·§2.3 B11·B13~B16·B19·B21·B25·B27·B31~B33을 구현하고 각각 테스트가 있다(`review.upbit_trend_decisions` 마이그레이션 포함).
9. 4885 §7 지표를 sleeve report와 결정 행에서 계산하는 읽기 전용 보고 명령이 있다.

### 8.2 hk 5441 — 게이트웨이 Upbit forwarder (auto_trader, T3)

범위: 스키마 v2, 위임장 두 테이블 + 청산 인계·운영자 청산 테이블(S5·S6) + `strategy_sleeves.execution_path`·제안 INSERT 트리거(S7) + 계좌 kill 행 + 저널 `shadowed` 상태(추가형 마이그레이션, GRANT 포함), `UpbitShadowForwarder`·`UpbitLiveForwarder`, `upbit_krw_tick`, intent 목록 GET, `scripts/execution_mandates.py`.

1. kis_mock 경로의 요청·응답·픽스처가 바이트 동일하다(기존 테스트 무변경 통과).
2. §2 I1~I10의 각 행에 실패 방향 테스트가 하나씩 있고 이름에 행 번호가 있다(`test_i<n>_*`).
3. shadow forwarder 모듈은 브로커·주문 모듈을 import하지 않는다(정적 테스트), DB가 shadow intent의 `broker_order_id` 기록을 거부한다.
4. live forwarder는 env `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`(기본 false) AND 위임장 `mode='live'` AND intent `execution_mode='live'` AND 계좌 kill `ACTIVE`일 때만 기존 Upbit 주문 서비스를 부르며, Upbit `identifier`에 child 키를 넣는다.
5. 손실매도 가드·쿨다운·loss_cut 코드와 상수는 diff에 없다.
6. 위임장 CLI는 preview 기본, `--commit` 필요, live 위임장 행은 §4.3 결정(D2)대로만 생긴다.
7. 마이그레이션 downgrade는 행이 있으면 거부한다.
8. §2.2~§2.5 B2~B5·B7·B9~B12·B17·B18·B20·B23·B28~B30·B34·S1~S7 각각에 실패 방향 테스트가 있다. S5는 인계 `open` 동안 매수·매도 예약 거부, 미해결 child가 있으면 `ready_for_card`로 못 감, rung 2개 이상·인계 예약 초과 rung 카드 커밋 거부, 부분 체결 후 취소된 카드의 잔량이 `ready_for_card`로 돌아감, §2.6 O1(코어 진입 후 불명 결과는 `card_claimed` 유지, 전송 없음이 증명된 만료·거절만 복귀)·O2(원장 체결 합 ≠ 브로커 누적이면 `card_claimed` + `unmeasured`), `ready_for_card` 없이 `go` sleeve 카드 INSERT 거부를 포함한다. 추가로: 한 인계에서 두 번째 카드 claim 거부(S5 d), 카드 발송 뒤 자격증명이 바뀌면 승인 재검증·POST 직전 지문 불일치로 전송 0(S5 d), 만료된 위임장에서 운영자 청산 intent 저장·전송 성공(S6). S6은 `paused`/`stopped`·위임장 철회 상태에서 운영자 청산 매도가 저장·예약·전송 직전 모두 같은 `evaluate_operator_exit`로 통과하고 매수는 거부됨을 포함한다. S7은 `execution_path` 변경 거부와 `card` sleeve 위임장 결속 거부를 포함한다.
9. §1.4 sleeve 귀속 두 번째 출처와 §1.5 Upbit 체결 증거 질의, §3.6 `shadow_positions`가 있고, 기존 rung 귀속 결과가 바이트 동일하다(골든).
10. `docs/design/1440-intent-gateway-contract.md`와 `docs/agent-contracts/broker-and-ledger-contracts.md` "Execution Intent Gateway" 절을 같은 PR에서 갱신하고, 루트 `AGENTS.md` 하드룰 4 예외가 필요하면 그 문구는 운영자 결정(D3) 인용과 함께만 넣는다.

### 8.3 hk 5442 — pi 운영 (런북·배치)

1. 게이트웨이 서버·Go 실행기를 pi(또는 D6이 정한 호스트)에서 루프백으로 띄우는 런북이 있다(재시작 정책 없음 vs 있음은 D6).
2. 기동 순서·정지 순서·kill 세 단계(§2 I7) 각각의 명령과 확인 방법이 있다.
3. shadow→live 전환 체크리스트(§9.2)와 롤백(§9.3) 절차가 명령 단위로 있다.
4. heartbeat·장애 알림 경로(기존 ops Discord, 결정 169 범위)가 정의되고 레인 이벤트를 쓰지 않는다.
5. 런북의 명령·플래그는 실제 argparse/flag와 대조하는 테스트가 있다(기존 플레이북 테스트 패턴).
6. env 값·Dagu 등록·재시작 정책은 런북에 "운영자 승인 후"로 표시되고 이 티켓이 적용하지 않는다.

### 8.4 fillwire hk 5439 — Upbit 실행기 (요구사항만)

1. §3.7 호환 변경: kis_mock child 직렬화·기존 골든 무변경, `quantity_decimal`·`shadowed`·`upbit_live` 지원.
2. 수량 고정소수(1e-8) 일반화가 kis_mock 배율 1에서 기존 테스트를 바꾸지 않는다.
3. §5 R1~R12 각각에 테스트가 있다(R4 intent당 submit 1회, R5 만료 margin, R6 신선도, R7 재연결·재시작 보류, R8 무재전송).
4. 새 `cmd/execupbit`는 기본 `Off`, `upbit_live` 전송은 별도 플래그가 있어야 하고, 브로커 API·DB를 import하지 않는다(정적 테스트).
5. Upbit 호가표 Go 구현이 auto_trader 공유 픽스처와 일치한다.
6. shadow 가상 체결 출력 파일 형식이 문서화된다.
7. §2.3 B34(계좌별 체결가 표현), §2.2 B1(최우선 매도호가 ≤ 마지막 체결가일 때만 매수)과 B20(계좌별 fill·view 검증)에 테스트가 있다.

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
| D9 | 위임장 상한 값 | `per_intent_max_notional_krw`=480,000, `max_open_intents`=4, `price_band_bps`=30, `max_intent_ttl`=12h, `expires_at`=시작+91일 | 4885 §3.5·§11, 3795 30bp 시험값 |
| D10 | Claude 정기 매매 세션 축소 시점 | §1.2의 조건(live 첫 2주 통과) | at-go-executor 4 |
| D11 | 같은 intent 안의 추격(cancel 후 재submit) | **1단계는 없음**(R4). 필요해지면 브로커 확인 cancel + 체결 수량 증거로 노출을 푸는 E1 규칙 변경을 별도 설계 | E1 cap 규칙, §1.5 지연 |
| D12 | shadow 위임장을 운영자 CLI `--commit`만으로 만드는 것 | **예** — shadow는 전송이 불가능(I6) | §4.3 |
| D14 | 기존 매수 가드(지정가 ≤ 마지막 체결가) 완화 | **아니오** — B1로 가드 안에서 실행. 체결률이 측정상 낮으면 별도 결정 | 하드룰 4 |
| D15 | 무인 잡이 회고·`approval_issue_id`를 만들고 허용 caller로 loss_cut 제안을 직접 생성 | **1단계는 아니오** — B14대로 알림 + 운영자 카드. 필요 시 env(`LOSS_CUT_ALLOWED_AGENT_IDS`) 변경 승인과 함께 별도 결정 | 기존 loss_cut 증거 계약 |
| D16 | 정지(`paused`/`stopped`) sleeve 포지션을 잡이 자동 청산 | **아니오** — 운영자 청산(S6)으로만, 4885 §8의 운영자 판단 유지 | 4885 §8 |
| D17 | live 위임장의 계좌 결속을 운영자 확인(지문 + 잔고 요약)으로 받는 것 | **예**, 브로커 계좌 식별자를 얻는 방법이 확인되면 그것으로 교체 | 3795 §4.6, B23 |
| D13 | 실행기 상수 | `CancelAfter` 120초, `ExpiryMargin` 60초, `StaleAfter` 2초(기존), `PollEvery` 15초, child 1회/초 | §5 |
| D18 | 달력 일별 매수 상한을 두지 않음(S3) | **예** — 위험은 intent 상한(4885 `LEG_CAP` 480,000)·sleeve 예산(1,950,000)·누적 손실 한도(1,000,000)로 묶인다. 일별 상한은 자정 경계 규칙만 늘렸다 | at-next-go 2, 리뷰 1~4회차 |
| D19 | 평단 아래 청산은 청산 인계(S5)로 카드에 넘기고, 인계 중 그 심볼의 Go 주문을 막음 | **예** — 가드 완화 없음(D4 A). 손절이 사람 2클릭만큼 늦는 것은 4885 §5.2 한계 그대로 | 하드룰 4 |

## 11. 알았지만 건드리지 않은 것

- E1 계약의 KIS mock 코디네이션 라우트·amend 차단(hk 3745) — 이 경로와 무관.
- 3795 바구니·회차 계획·낙폭 동결 — 최소판에 넣지 않았다.
- Upbit 계정 평단을 sleeve 단위로 분리하는 것 — 가드의 입력을 바꾸는 일이라 범위 밖(D4 근거).
- `adjust_price_to_upbit_unit`의 1,000~5,000원 구간이 공식 호가표와 다른 것(B28) — 다른 호출자에게 영향이 있어 전역 수정은 별도 작업.
- 키움 모의 KR을 같은 경로에 올리는 것 — at-go-executor 2의 다음 단계, 별도 설계.
- 4885 §1.3(하드룰 6 "세션의 수동 작업")·§5.2("스케줄러가 없으므로")·§10(시작 순서)는 Dagu 잡·의도 경로를 전제로 하지 않는다. brief가 4885는 §4·§9만 고치라고 했으므로 그대로 두었다 — 5438 §7 안내 줄이 우선한다.
