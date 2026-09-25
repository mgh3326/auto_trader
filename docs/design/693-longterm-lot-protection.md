# #693 — 한 계좌 안에서 장기 배분 수량 보호 (설계, Stage 1)

- 상태: **설계 초안 — 운영자 검토용. 코드 변경 0.** 구현은 이 문서가 승인된 뒤 별도 PR 로 한다.
- 입력: 운영자 결정(2026-09-25: 별도 계좌를 쓰지 않는다. 운영자가 종목·수량별로 목적(장기/전술)을
  표시하고, 전술 매도 레인은 장기 수량을 건드리지 못한다) + #670 조사 보고서(12개 지점 확인,
  "모든 경로가 브로커 집계 sellable 로 사이징한다 · `manual_only`/`hold_until`/`lot_context` 는 보호가 아니다").
- 개정: r2 — 독립 tester(xAI Grok 4.7) r1 지적 반영: Upbit 취소-재발주 재장전(BLOCKER), 정정 종류별 조건, 스코프별 모드,
  가드 키 정규화·원값 입력·H 입력, Toss `orderAmount`, 레거시 분할 루프, 인용 줄 번호.
- 기준 커밋: `e9477c7a6` (origin/main). 본문의 `file:line` 은 전부 이 커밋에서 직접 읽어 확인했다.
  #670 보고서의 줄 번호와 다르면 그 사이 코드가 이동한 것이다(§2.4).
- 범위: **live 계좌만** — `kis_live`(KR·US), `toss_live`(KR·US), `upbit_live`(crypto).
  mock/paper/demo(kis_mock·kiwoom_mock·alpaca paper·binance demo)는 운영자 자금이 아니고 장기 배분이
  없으므로 범위 밖이다(§7 Q3).

## 0. 요약

1. **보호 수량 P** 는 `(account_scope, market, symbol)` 마다 운영자가 선언하는 하나의 숫자다. 로트(lot)를
   추적하지 않는다 — 브로커가 로트를 구분해 주지 않기 때문이다(#670 #3). 보호는 "이 계좌의 이 종목 보유가
   P 밑으로 내려가게 만드는 매도는 auto_trader 가 보내지 않는다" 라는 **바닥(floor)** 이다.
2. 새 매도 주문의 허용 한도는 `headroom = broker_sellable − P` 이다. 브로커 sellable 은 이미 미체결 매도를
   뺀 값이므로(§1.4) 이 한 줄이 "모든 미체결 매도가 체결돼도 보유 ≥ P" 를 보장한다. `headroom ≤ 0` 이면
   그 종목의 새 매도는 전부 거부된다.
3. 강제는 **두 겹**이다. (L1) 사이징 읽기 — 매도 수량을 정하거나 보여 주는 모든 곳이 브로커 sellable 대신
   `headroom` 을 본다. (L2) **송신 직전 최종 가드** — 브로커 HTTP 로 가는 live 매도·정정 경로 6개 지점(G1–G6)
   각각에서 신선한 브로커 값을 다시 읽어 검사하고, 판정할 수 없으면 보내지 않는다(fail-closed).
4. P 를 바꾸는 것은 **운영자뿐**(관리자 세션 + CSRF + 확인 대화상자). MCP 쓰기 도구는 만들지 않는다 —
   전술 레인의 주체가 바로 MCP consumer 이기 때문이다. 모든 변경은 append-only 개정 이력에 남는다.
5. 장기 수량을 팔고 싶으면 운영자가 **먼저 P 를 낮추고** 그다음 평소처럼 판다. "보호 수량을 소진하는 매도"
   라는 호출 단위 우회 경로는 두지 않는다.
6. 롤아웃은 `off → shadow(차단 없이 would_block 기록) → enforce`. 스케줄러 등록 0 · 게이트 기본 off.

---

## 1. 데이터 모델

### 1.1 계좌 식별 — 현재 코드의 사실

- live 계좌 번호는 어디에도 저장되지 않는다. KIS 는 배포당 한 계좌(`app/core/config.py:237` `kis_account_no`),
  Toss·Upbit 도 자격증명 한 벌 = 한 계좌다.
- 원장이 쓰는 스코프 문자열: `KISLiveOrderLedger.account_mode='kis_live'`(클래스 `app/models/review.py:392`, 기본값 `:430`),
  `LiveOrderLedger.account_scope ∈ {kis_live, upbit_live}`(클래스 `review.py:484`, 컬럼 `:515`),
  `TossLiveOrderLedger.account_mode='toss_live'`(클래스 `review.py:626`, CHECK `:640-641`).
- `BrokerAccount`(`app/models/manual_holdings.py:44`)는 **수동 보유 전용** 테이블이라 live 원장이 참조하지 않는다.
  여기에 붙이지 않는다.

따라서 키는 원장과 같은 어휘인 `account_scope ∈ {kis_live, toss_live, upbit_live}` + `market ∈ {kr, us, crypto}`
+ `symbol`(DB 형식, `app/core/symbol.py::to_db_symbol` 로만 정규화 — 하드룰 8)이다. 같은 종목을 KIS 와 Toss 에
나눠 들고 있으면 행이 두 개다(계좌별 보호). 계좌가 배포당 둘 이상이 되면 키에 계좌 식별자를 더하는
마이그레이션이 필요하다(§7 Q12).

### 1.2 테이블 (제안, 마이그레이션은 구현 PR 에서)

**`review.protected_positions` — 현재값(head), 행당 한 키**

| 컬럼 | 타입 | 제약 |
|---|---|---|
| `id` | bigint PK | |
| `account_scope` | text | CHECK `IN ('kis_live','toss_live','upbit_live')` |
| `market` | text | CHECK `IN ('kr','us','crypto')`, 그리고 scope×market 조합 CHECK(`upbit_live⇔crypto`) |
| `symbol` | text | NOT NULL, 공백 거부 CHECK, DB 형식 |
| `protected_quantity` | numeric(28,8) | CHECK `>= 0` (0 = 해제됨, 행은 이력 연결을 위해 남긴다) |
| `purpose` | text | CHECK `IN ('long_term')` — 닫힌 어휘(§7 Q9) |
| `revision` | int | 단조 증가, 낙관적 동시성 토큰 |
| `last_confirmed_broker_held` | numeric(28,8) | 마지막 선언/재확인 시 신선하게 읽은 브로커 보유량 (§4.3 drift 판정 기준점) |
| `last_confirmed_at` | timestamptz | |
| `updated_by_user_id` | int | NOT NULL |
| `updated_at` / `created_at` | timestamptz | |

UNIQUE `(account_scope, market, symbol)`.

**`review.protected_position_revisions` — append-only 감사 이력**

| 컬럼 | 설명 |
|---|---|
| `id`, `protected_position_id` FK, `revision` | UNIQUE `(protected_position_id, revision)` |
| `action` | `declare` / `increase` / `decrease` / `release` / `reconfirm` |
| `previous_quantity`, `new_quantity` | |
| `broker_held_observed`, `broker_sellable_observed`, `broker_observed_at` | 변경 시점에 신선하게 읽은 브로커 증거. 읽기 실패면 변경 자체가 거부되므로(§1.3) NULL 불가 |
| `reason` | NOT NULL, 공백 거부 — 운영자 메모 |
| `actor_user_id`, `origin` | `origin ∈ ('invest_ui','operator_cli')` |
| `idempotency_key` | UNIQUE `(actor_user_id, idempotency_key)` — 더블클릭 재전송 방지 |
| `recorded_at` | |

UPDATE/DELETE/TRUNCATE 는 DB 트리거로 거부한다. 선례: `ExternalCashDeclaration`
(`app/models/funding_advisory.py:35`) 과 트리거 템플릿 `alembic/versions/20260815_funding_advisory.py:262-288`
(`reject_*_mutation()` + `BEFORE UPDATE OR DELETE` + `BEFORE TRUNCATE` + `REVOKE`). head 행 갱신과 revision
INSERT 는 한 트랜잭션에서 한다. head 는 revision 에서 언제든 재구성할 수 있어야 한다(테스트 항목, §6).

**시스템은 P 를 절대 쓰지 않는다.** 서비스 레이어의 쓰기 함수는 운영자 요청 경로(§5)에서만 호출된다.
시스템이 관측한 이상(shortfall·drift)은 P 를 바꾸지 않고 **상태로 계산해 보여 주고 알린다**(§1.4).

### 1.3 누가 바꾸나 · 쓰기 규칙

- 쓰기 = 관리자(`require_admin`) 세션 + `/invest/api/*` CSRF + 확인 플래그(§5). 쓰기 대상 사용자 행은
  manual_cash 와 같은 이유로 `MCP_USER_ID` 소유로 고정한다(`app/routers/invest_manual_cash.py:59-61` 패턴).
- **MCP 쓰기 도구 없음.** 읽기 도구(`get_protected_positions`)와 `get_holdings` 필드 추가만 한다.
  `trading_policy.yaml`("operator PR 로만 편집 — 쓰기 도구 없음") · manual_cash 와 같은 원칙이다.
- **증가·신규 선언**: 그 자리에서 브로커 보유량 H 를 **신선하게** 읽어야 하며(캐시 금지, ROB-1310 원칙),
  읽기 실패면 거부한다. `P_new ≤ H` 가 아니면 거부한다(보호가 보유를 넘을 수 없다).
- **감소·해제**: 보호를 푸는 방향 = 전술 매도를 가능하게 만드는 방향이므로 **확인이 더 무겁다**
  (종목코드 재입력, §5). 브로커 읽기는 증거 기록용이며, 읽기 실패 시에도 감소는 허용할지 → §7 Q10
  (권고: **거부** — 증거 칸 NOT NULL 유지. 운영자가 브로커 복구 후 다시 한다).
- 동시성: `expected_revision` 불일치면 409 `stale_form`, 최초 선언은 `INSERT … ON CONFLICT DO NOTHING` 후
  경합 패배를 `stale_form` 으로(manual_cash `app/services/manual_cash_settings.py` `save_manual_cash` 와 동일).

### 1.4 브로커 포지션과의 관계 — 산술과 상태

기호: `H` = 브로커 총 보유, `S` = 브로커 주문가능(sellable), `O` = 미체결 매도에 묶인 수량, `P` = 보호 수량.

**전제(구현 PR 에서 브로커별로 실모양 fixture 로 증명해야 함):** 세 브로커 모두 `S ≈ H − O` 이다.
- KIS: `ord_psbl_qty` 는 미체결 매도를 뺀 값이다. `_get_holdings_for_order` 도 `locked = hldg − ord_psbl` 로
  이 해석을 쓴다(`app/mcp_server/tooling/order_validation.py:815-826`, US `:836-849`).
- Upbit: `balance` = 주문가능, `locked` = 주문에 묶임(`app/services/brokers/upbit/client.py:222-237`).
- Toss: `sellableQuantity`(`app/services/brokers/toss/dto.py:251`) — 미체결 매도 차감 여부는 **미확인**,
  구현 PR 의 필수 확인 항목(§7 Q13). 차감하지 않는다면 Toss 공식은 `S − O_pending − P` 로 바뀐다.

새 매도 수량 `q` 의 허용 조건: **`q ≤ S − P`**. 이유: 모든 미체결 매도(O)와 이번 q 가 전부 체결돼도
`H − O − q ≥ P` ⇔ `q ≤ (H − O) − P = S − P`. 미체결 매도가 전술이든 운영자 수동이든 상관없다.

| 상태 | 조건 | 새 매도 | 표시·알림 |
|---|---|---|---|
| `unprotected` | 행 없음 또는 P = 0 | 기존 동작 그대로 | — |
| `covered` | `S ≥ P` | `q ≤ S − P` 만 허용 | 보호 칩 |
| `encroached` | `H ≥ P > S` | 전부 거부(headroom ≤ 0) | **경고**: 기존 미체결 매도가 전부 체결되면 보호가 깨진다. 자동 취소는 하지 않는다(브로커 mutation, §7 Q5) |
| `shortfall` | `H < P` | 전부 거부 | **경고**: 보유가 보호 밑이다(앱 수동 매도·출고·역분할 등). P 는 그대로 둔다(§4.4) |
| `unverified` | drift 검출(§4.3) 또는 `S` 미관측(§3.3) | 전부 거부 | 운영자 재확인 요청 |

**보호는 보유를 넘지 않는다**는 요구는 "쓰는 순간 `P ≤ H` 를 검사한다" 로 지키고, 이후 보유가 줄어드는
것은 막을 수 없으므로(브로커 앱 매도는 auto_trader 밖) `shortfall` 상태로 드러내고 전술 매도를 막는다.
P 를 자동으로 H 로 깎지 않는 이유는 §4.4.

---

## 2. P 를 빼야 하는 모든 소비자

"전술 매도 레인" 은 수량을 사람이 아닌 코드·LLM·표가 정하거나, 운영자 클릭 한 번으로 한 묶음이 나가는
경로다. 조사 결과 **서버 코드가 보유에서 매도 수량을 %로 계산해 live 에 보내는 곳은 사실상 없고**
(유일한 예외는 미배선 레거시 잡, 아래 C8), live 매도 수량은 전부 **제안 작성자(LLM 세션·decision table·운영자)가
정한 숫자**다. 따라서 보호의 무게는 사이징보다 **송신 게이트**(L2)에 실려야 한다. 그래도 L1 을 하는 이유는
(a) 세션이 받는 숫자(`sellable`)가 처음부터 맞아야 쓸데없는 제안·거부가 줄고, (b) `quantity=None` = "전량"
기본값이 여러 곳에 있기 때문이다.

### 2.1 L1 — 사이징·표시 읽기 (headroom 으로 치환)

공통 헬퍼(제안): `ProtectedQuantityService.headroom(scope, market, symbol, broker_sellable) -> Headroom`
(`Headroom = {broker_sellable, protected, tactical_sellable=max(0, S−P), state}`), Decimal 전용, 순수 산술 +
DB 조회 1회. `is_mock=True` 호출은 즉시 원값을 돌려준다.

| # | 소비자 | 현재 (file:line) | 변경 | headroom ≤ 0 일 때 |
|---|---|---|---|---|
| C1 | `_get_holdings_for_order` — 모든 KIS/Upbit 매도 프리뷰·검증·KR 정정·crypto 실행의 공통 입력 | KR `order_validation.py:815-826`, US `:836-849`, Upbit `:797-806` | 반환 dict 의 `quantity` 를 `tactical_sellable` 로, `protected_quantity`·`broker_sellable_quantity`(치환 전 브로커 원값)·`protection_state` 를 추가. `locked`·`total_quantity` 는 브로커 값 유지. 🔴 G1·G4 는 `quantity` 가 아니라 `broker_sellable_quantity` 를 읽는다 — `quantity` 에서 P 를 한 번 더 빼면 합법 매도를 막는 이중 차감이 된다(§6.1 테스트) | `quantity=0` → 기존 `quantity > available` 에러 경로(`:1408`)가 거부. 에러 메시지에 `protected=P` 병기 |
| C2 | `_preview_sell` — 프리뷰 수량 (제안 revalidate 가 쓰는 `observed_sellable_qty` 포함) | `order_validation.py:1094`, 비교 `:1134`, **시장가는 요청과 무관하게 전량 `:1163`**, 지정가 None=전량 `:1222`, 증거 `:1129-1130` | C1 경유로 자동 반영. `:1163` 은 전량 = tactical 전량이 된다(기존 결함은 §2.5 로 따로 기록) | 프리뷰 에러 → `revalidation.py:826-843`·`loss_cut_approval.py:618-635` 캡도 자동 0 |
| C3 | `_validate_sell_side` — `quantity=None` 이면 가용 전량 | `order_validation.py:1352`, 비교 `:1408`, None=전량 `:1418` | C1 경유 | 거부 |
| C4 | `_execute_crypto_order` — `quantity=None` 이면 holdings 전량 | `order_execution.py:256-260` | C1 경유. 단 정상 경로는 `_send_to_broker` 가 해석된 `order_quantity` 를 넘기므로(`order_execution.py:1186-1190`) None 이 오지 않음을 G1 에서 단언 | G1 이 선차단 |
| C5 | invest home 보유 읽기 — 화면·`loss_cut_approval` 증거·snapshot_backed `auto_emit`·collector 의 원천 | KR `app/services/invest_home_readers.py:142`(hldg 폴백)/`:165`, US `:174-178`/`:212` ; 스키마 `app/schemas/invest_home.py:75-77` | `sellableQuantity` 의미를 **tactical** 로 바꾸고 `brokerSellableQuantity`·`protectedQuantity`·`protectionState` 추가(`extra="forbid"` 라 스키마 동시 변경). 하류(`auto_emit.py:466`, `loss_cut_approval.py:186-189,634`, `collectors/portfolio.py:166`)는 코드 변경 없이 줄어든 값을 받는다 | `auto_emit` 은 `sellable ≤ 0` 이면 매도 검토 후보를 만들지 않는다(`:466`) — 완전 보호 종목은 전술 검토에서 자동 제외 |
| C6 | Toss sellable 파싱·캐시 — 표시용 | `brokers/toss/dto.py:251`, `client.py:401-405`, `toss_portfolio_service.py:289,308`, `toss_sellable_cache.py` | 원천(dto)은 **브로커 원값 유지**(G2/G3 가 fresh 원값이 필요). 표시 계층(`toss_portfolio_service`)에서만 headroom 적용 | 표시 0 |
| C7 | MCP `get_holdings` — 세션이 매도 수량을 고르는 1차 자료 | `app/mcp_server/tooling/portfolio_holdings.py:1286`, KIS `:338`, Upbit `:445`, Toss `:602` | 포지션마다 `protected_quantity`, `tactical_sellable_quantity`, `protection_state` 추가. 기존 `sellable` 류 필드도 tactical 로 | 세션이 0 을 본다 |
| C8 | **레거시 KIS 자동매도 잡** — 보유 전량을 가격 레벨로 균등분할해 직접 발주 | `app/services/kis_trading_service.py:360`(`//` 분할 `:433`), 수량원천 `app/jobs/kis_market_adapters.py:32,44,677,691`, 발주 `:108`/`:129` | 현재 **production 호출자 없음**(`app/jobs/kis_trading.py` 를 import 하는 곳은 테스트와 AGENTS.md 문서뿐, `app/tasks/**` 등록 없음). 구현 PR 에서 삭제 또는 G5 가드 — §7 Q11 | 가드 시: 보호 종목 skip |
| C9 | US action classifier — trim% × **총수량** | `app/services/action_report/us/action_classifier.py:263-266`, `_cap_executable :128-133` | 미배선(호출자는 `us/__init__.py` re-export 와 테스트뿐). 배선되는 순간 C1 과 같은 headroom 을 써야 한다는 정적 테스트만 추가 | — |
| C10 | US account snapshot sellable | `action_report/us/account_snapshot.py:364-378,398` | 미배선. C9 와 동일 처리 | — |

**의도적으로 빼지 않는 곳**: 브로커 원시 파싱 지점 `KISAccount.fetch_my_stocks`(`app/services/brokers/kis/account.py:534-541`),
Upbit `parse_upbit_account_row`(`upbit/client.py:222-237`), Toss dto. 여기서 빼면 한 번에 다 덮여 보이지만
(1) G1–G4 가 필요로 하는 **브로커 원값**이 사라지고, (2) 리컨실·mock baseline·화면의 "실제 보유" 가 거짓이
되며, (3) 보호가 조회 계층의 부수효과가 되어 테스트로 경계를 못 긋는다. 원값은 원천에서 그대로, 보호는
정책 계층에서.

**%만 내는 자문 코드**(수량 없음, 변경 불필요 · 표시만): `portfolio_action_classifier.py:89,96`(trim 20/25%,
`sellable_quantity` 는 `portfolio_action_service.py:75` 에서 항상 None), `portfolio_rotation_service.py:32,202,210`
(reduce 30/100%), `downside_watch_service.py:171`(메타데이터만, notify_only watch). 이들이 보호 종목에
trim 을 권하는 것은 "제안" 이고, 실제 매도는 G 가드에서 막힌다. 권고 문구에 `protection_state` 를 병기하는
것은 선택(§7 Q14).

### 2.2 L2 대상 — 전술 매도 레인 전수 (어떤 가드가 덮는가)

모든 live 매도는 결국 아래 **송신 지점 4군**으로 모인다. 레인은 그중 하나를 탄다.

| 송신 지점 | 함수 | 가드 |
|---|---|---|
| S-A KIS live KR/US + Upbit 신규 | `_place_order_impl`(`order_execution.py:1704`) → `_execute_and_record`(`:917`) → `_send_to_broker`(`:1178`) → `_execute_order`(`:177`) → KR `order_korea_stock`(`brokers/kis/domestic_orders.py:340`) / US `order_overseas_stock`(`overseas_orders.py:105`) / Upbit `place_sell_order`·`place_market_sell_order`(`brokers/upbit/orders.py:100,146`) | **G1** |
| S-B Toss live 신규 | `_toss_place_order_impl`(`orders_toss_variants.py:1334`) → `_fresh_sellable_preflight`(`:822`, 호출 `:1746`) → `client.place_order`(`:1756-1773` → `brokers/toss/client.py:422`) | **G2** |
| S-C 정정(modify) | KIS/Upbit `modify_order_impl`(`orders_modify_cancel.py:1882`): KR `:1662` → `modify_korea_order`(`domestic_orders.py:999`), US `_modify_kis_overseas :1732` → `:1826` → `modify_overseas_order`(`overseas_orders.py:905`), Upbit `_modify_upbit :1333` → `cancel_and_reorder`(`upbit/orders.py:396`, 취소 `:465`, 재발주 `:479` — 재발주는 사실상 **신규 주문**, §3.1.1) ; Toss `toss_modify_order`(`orders_toss_variants.py:1914`, 검사 `:2104`, 송신 `:2125`) | **G3**(Toss) / **G4**(KIS·Upbit) |
| S-D 레거시 직접 발주 | `kis_trading_service._process_sell_orders_impl` → `:108`/`:129` (미배선) | **G5** 또는 삭제 |

| # | 레인 | 수량은 누가 정하나 | 송신 경로 | 가드 | 운영자 클릭 없이 나가나 |
|---|---|---|---|---|---|
| T1 | 제안 파이프라인 **자동승인** | 제안 작성자(`order_proposal_tools.py:672`), 생성 시 보유 검사 없음(`service.py:806`) | `dispatch_proposal`(`order_proposals/dispatch.py:907`) → `revalidate_and_submit`(`revalidation.py:901`, 호출 `dispatch.py:1136`) → `_default_place_order_fn`(`:603`) → Toss `:675` / 그 외 `_place_order_impl :707` | G1 / G2 | **예** (`ORDER_PROPOSALS_AUTO_APPROVE`, 기본 False `config.py:912`; sell 은 `MODE=expanded` 에서 take_profit 판정 `auto_approve.py:572,1044`) |
| T2 | 제안 — Telegram 승인 (durable inbox 포함) · 웹 승인 | 동일 | `_handle_approve`(`order_proposals/telegram_callback.py:683`, `:834`) · `callback_inbox/worker.py:225` · `handle_web_approval`(`:1903`, 라우터 `app/routers/invest_loss_cut_approvals.py:143,189`) → 같은 `revalidate_and_submit` | G1 / G2 | 카드당 1클릭(손절은 2클릭) |
| T3 | 제안 — **replace**(정정 대체) | 동일 | `broker_gateway.cancel_target_order`(`order_proposals/broker_gateway.py:657`) + 새 발주 | G1 / G2 (새 발주가 다시 검사됨) | T1/T2 와 동일 |
| T4 | 에이전트 작성 전술 매도: trim_preplace, breakeven_reserve_trim, breakeven_extension_ladder, momentum_spike_profit_ladder, 저항 trim | **세션이 정함** — 정책은 텍스트만(`config/trading_policy.yaml:1204-1398`, 코드 소비자 없음) | MCP `order_proposal_create` → T1/T2 | G1 / G2 | T1 조건 충족 시 예 |
| T5 | 손절 `loss_cut` | 운영자/세션, 확인 시 ≤ sellable(`revalidation.py:842`, `loss_cut_approval.py:634`) | 제안 파이프라인만(직접 경로 금지 `orders_registration.py:209-224`) | G1 / G2 | 아니오(자동승인 불가 `auto_approve.py:698-702`) |
| T6 | 방어 trim `defensive_trim` · 현금조달 `cash_funding` | 작성자, cash_funding 은 `ceil(required/price)+1` 캡(`cash_funding_exemption.py:285-294`) | 제안 파이프라인만(직접 도구 거부: cash_funding `orders_registration.py:267-273`, defensive_trim `:274-282`, 실행단 `order_execution.py:1815`) | G1 / G2 | cash_funding 은 자동승인 가능(`auto_approve.py:1012-1039`) |
| T7 | decision table apply | 표에 쓴 `qty`(`decision_table_apply/service.py:361`), 검증기는 보유를 모름(`decision_table_validate/validator.py:631-645`) | `order_proposal_create` → T1/T2 | G1 / G2 | 한 번의 confirm 으로 다건 제안 → 각 제안은 T1/T2 |
| T8 | watch trigger repricing | 외부 LLM judge(`app/services/watch_trigger_repricing/judgement.py:72`) | `proposal_chain.py:209` → `order_proposal_create` | G1 / G2 | 현재 미배선(`WATCH_TRIGGER_REPRICING_ENABLED` 기본 False, 틱 호출자 없음) |
| T9 | MCP 직접 발주 `place_order` / `kis_live_place_order` / `toss_place_order` | 세션, **None=가용 전량** | `orders_registration.py:182,228,324` / `orders_kis_variants.py:509` / `orders_toss_variants.py:1848,2464` | G1 / G2 | 세션이 `dry_run=False(+confirm)` 로 부를 수 있음 — 운영자 카드 없음 |
| T10 | 웹 스크리너 주문 `POST /api/screener/order` (시장가 허용) | 페이로드, None=전량 | `app/routers/screener.py:213` → `screener_service.py:972` → `_place_order_impl` | G1 | 수동(로그인 세션) |
| T11 | MCP/웹 정정 `modify_order` / `kis_live_modify_order` / `toss_modify_order` | 호출자 `new_quantity` | S-C | G3 / G4 | 세션 호출 가능 |
| T12 | 레거시 KIS AI 자동매도 잡 | 보유 전량 균등분할 | S-D | G5 | 미배선 |
| — | 매도 아님(확인만): reserve-net(매수 전용 `support_reserve_net_consumer.py:563,571`), fill-event handoff(프롬프트 주입만), preopen bridge(프리뷰만), n8n/daily_scan(텍스트), portfolio rotation·downside watch(자문) | | | 없음 | |
| — | mock/paper/demo 전용: watch `auto_execute_mock`(`watch_auto_execute.py:255` 가 live 차단), kis_mock scalping, b0x mock 레인, kiwoom mock, alpaca paper, binance demo | | | 범위 밖(§7 Q3) | |

### 2.3 레인 정리 — 결론

- live 신규 매도의 송신 지점은 **S-A 와 S-B 두 개**뿐이고, 모든 제안 레인(T1–T8)과 직접 도구(T9–T10)가
  여기로 모인다. 여기에 가드를 두면 레인이 늘어도 덮인다.
- **정정(S-C)** 은 신규 발주 검사를 전혀 타지 않는 별도 송신 지점이다. KIS KR·US·Upbit 정정은 현재
  **수량 상한 검사가 로컬에 없다**(`orders_modify_cancel.py:1662` `final_quantity = new_quantity`;
  Upbit 은 취소가 먼저 나간 뒤 브로커 잔고 검사). 보호 기능 없이도 존재하는 구멍이며, 보호 기능은 여기에
  G4 를 **반드시** 새로 둬야 한다. #670 보고서가 다루지 않은 경로다.

### 2.4 #670 보고서와의 대조

| #670 항목 | 이번 확인 | 모순? |
|---|---|---|
| #5 US 주문 프리뷰 `order_preview.py` | 존재하나 호출자 없음, submit 비활성(`order_preview.py:348`) | 아니오 — 보호 판단에 영향 없음, 미배선 사실을 추가 |
| #4 US action classifier | 미배선(C9). trim% 가 sellable 이 아닌 **총수량** 기준(`:263-266`) | 아니오 — 배선 시 위험 요소를 추가 |
| #6 portfolio action classifier | %만 내고 수량 없음, `sellable_quantity` 는 항상 None | 아니오 |
| #7 order_validation `749-809, 1064-1110, 1306-1351` | 현재 `790-849, 1094-1163, 1352-1418` | 줄 이동만 |
| #8 Toss `683-772, 1557-1568, 1905-1920` | 현재 `773-872, 1746, 1914-2125` | 줄 이동만 |
| (누락) | 정정 경로 S-C, 레거시 잡 S-D, 스크리너 REST T10, decision table T7, watch repricing T8, 직접 MCP 도구 T9, `quantity=None`=전량 기본값 3곳 | 누락 보완 |

#670 의 결론("계좌 내 수량 소유권 메커니즘은 없다 · 브로커 집계 sellable 로 사이징한다")과 모순되는 발견은 없다.

### 2.5 보호와 별개로 발견한 기존 결함 (이 설계의 범위 밖, 후속 이슈 후보)

- `_preview_sell` 시장가 분기가 요청 수량을 무시하고 전량을 프리뷰(`order_validation.py:1163`) — 실행은 요청
  수량을 씀(`:1418`). 제안 자동승인은 지정가만이라 live 영향은 제한적.
- KIS/Upbit 정정 수량 상한 부재(§2.3).
- KIS sellable 필드가 비면 총보유로 폴백(`order_validation.py:817-821,840-844`) — 손절 프리뷰만 fail-closed(`:1122`).
  보호 종목에 대해서는 §3.3 이 이 폴백을 fail-closed 로 막는다.

---

## 3. 송신 직전 강제 (L2, defense in depth)

### 3.1 가드 함수 하나, 호출 지점 여러 개

```
assert_sell_within_protection(
    *, account_scope, market, symbol, quantity: Decimal,
    kind: Literal["new", "cancel_replace", "amend_broker_capped", "amend_uncapped"],
    fresh_broker_sellable: Decimal | None, fresh_broker_held: Decimal | None,
    sellable_observed: bool, amend_remaining_fresh: Decimal | None = None,
) -> None | ProtectionBlock
```

- 판정은 순수 산술 + protected_positions 조회 1회 + drift 판정(§4.3, `fresh_broker_held` 필요 — H 없이 sellable 만 받으면
  액면분할 fail-open 을 송신 경로에서 못 막는다). `kind` 별 조건은 §3.1.1.
- **키 정규화가 먼저다.** `market` 은 호출 지점의 내부 값(`equity_kr`/`equity_us`/`crypto`, `order_execution.py:162-169`
  `_normalize_market_type_to_external`)을 `kr`/`us`/`crypto` 로, `symbol` 은 **그 스코프의 live 원장이 저장하는 것과 같은
  문자열**(주식 `to_db_symbol`, Upbit 는 원장의 마켓 코드 형식)로 바꾼 뒤 조회한다. 매핑 실패·미지의 scope/market 은
  "행 없음 → 통과" 가 아니라 **거부** `protection_state_unavailable`. 가드 키 = 원장 키 동일성은 스코프별 테스트로 고정(§6.1).
- 정규화된 키에 행이 없거나 `P = 0` 이면 즉시 통과 — **보호 종목이 아니면 추가 브로커 읽기 0회**(기존 지연·동작 불변).
- `quantity` 는 **해석된 구체 수량**이어야 한다. None 이 오면 계약 위반으로 거부(`protected_quantity_unresolved`).
- `fresh_broker_sellable` 은 **치환 전 브로커 원값**이다. C1 이 치환한 `quantity` 를 넘기면 P 가 두 번 빠진다 —
  호출 지점은 `broker_sellable_quantity` 키(C1)나 Toss preflight 원값만 넘긴다.
- 차단 결과는 fixed `error_code` 어휘: `protected_quantity_exceeded` · `protected_quantity_encroached` ·
  `protected_quantity_shortfall` · `protected_sellable_unobserved` · `protected_state_unverified` ·
  `protected_quantity_unresolved` · `protection_state_unavailable`. 응답에 `protected_quantity`, `broker_sellable`,
  `headroom` 를 싣는다(비밀 아님).

#### 3.1.1 주문 종류별 조건 — 정정은 "Δ ≤ 0 이면 안전" 이 아니다

브로커 sellable 은 `S = H − O` 라서 **기존 주문이 체결돼도 변하지 않는다**(H 와 O 가 같이 준다). 그래서 정정이 체결된
만큼을 다시 "장전" 할 수 있는 경로에서는 스냅샷 잔량 기준 Δ 가 아무것도 보증하지 못한다. 최악은 기존 주문 잔량이
정정 직전에 전부 체결되는 경우이며, 그때 새로 장전되는 `new_q` 는 신규 주문과 똑같다.

| kind | 해당 경로 | 조건 | 근거 |
|---|---|---|---|
| `new` | G1·G2 신규 발주 | `q ≤ S − P` | §1.4 |
| `cancel_replace` | Upbit `cancel_and_reorder`(`upbit/orders.py:396`) | **두 번 검사.** ① 취소 전 사전검사 `new_q ≤ (S + 잔량_fresh) − P` (실패면 취소도 안 보낸다). ② **취소 성공 뒤·재발주 전**(`:465` 취소와 `:479` `place_sell_order` 사이, `cancel_and_reorder` 안): Upbit 잔고를 다시 읽어 `new_q ≤ S_after − P` — 취소가 끝났으므로 더 이상 체결이 끼어들 수 없다. ② 실패면 **재발주하지 않고** "취소됨·재발주 보류(protected)" 로 반환한다(주문은 사라지는 쪽 = 안전 방향, 응답·로그에 명시). **Δ ≤ 0 면제 없음** | `_modify_upbit` 은 `remaining_volume` 스냅샷을 고정해 넘기고(`orders_modify_cancel.py:1367-1369`, `:1391`), `cancel_and_reorder` 는 `new_quantity` 가 주어지면 잔량을 다시 읽지 않는다(`upbit/orders.py:436-437`). 취소 중 부분체결이 나면 체결 전 크기가 재발주된다 — H=100·P=60·잔여 매도 40·S=60 에서 25주 체결 뒤 40 재발주 → 총 65 매도, H=35 |
| `amend_broker_capped` | 브로커가 **자기 실제 잔량**에만 정정을 적용함이 확인된 경로. 후보: KIS KR(`QTY_ALL_ORD_YN="Y"`, `domestic_orders.py:1077`) — 브로커 동작 **미확인**(§7 Q19) | `new_q ≤ 잔량_fresh` 이면 통과(가격만 정정 포함), 아니면 `new_q − 잔량_fresh ≤ S − P` | 브로커가 체결분을 재장전할 수 없을 때만 Δ 가 의미를 가진다 |
| `amend_uncapped` | 위 확인이 없는 제자리 정정: KIS US `ORD_QTY`(`overseas_orders.py:965`), Toss KR 수량 정정, 그리고 확인 전의 KIS KR | **`new_q ≤ S − P`**(신규 주문과 동일, 가격만 정정도 포함) | 최악 경우(잔량 전부 체결 직후 정정 적용)를 덮는 유일한 조건. 대가: headroom 0 인 종목의 기존 전술 매도는 가격 정정이 막힌다 — 취소 + 신규 발주(G1/G2)로 대체. Q19 가 확인되면 `amend_broker_capped` 로 올린다 |

Toss US 정정은 API 가 수량 변경을 거부하므로 기존 preflight 가 **상속된 전체 수량**을 fresh sellable 과 비교한다
(`orders_toss_variants.py:2092-2108`). G3 는 이 검사를 **대체하지 않고 추가**한다(preflight 의 비교 수량은 계속 상속 전량).

| 가드 | 위치 (앞/뒤 기준점) | 앞에 두면 안 되는 것 | 신선한 S·H 의 출처 |
|---|---|---|---|
| **G1** | `_execute_and_record` 안, `side=="sell" and not is_mock` 일 때 **ROB-653 intent reserve(`order_execution.py:1087`) 직전**, 그리고 `_send_to_broker`(`:1178`) 보다 앞. kind=`new` | kis_mock 귀속 게이트(`:971-973`, 함수 최상단 불변 — 하드룰 7)보다 앞에 두지 않는다. reserve **뒤**에 두면 차단 시 멱등키가 소모되고 `_release_reserved_intent_after_send_failure`(`:1106`)의 "proven absence 만 해제" 규칙과 얽힌다 — 그래서 reserve 직전 | `_get_holdings_for_order` 를 **다시** 호출해 `broker_sellable_quantity`·`total_quantity` 사용(C1 의 early 값 재사용 금지 — `:1942` 이후 프리뷰·승인·reserve 사이 시간차) |
| **G2** | `_toss_place_order_impl` 의 `_fresh_sellable_preflight`(`:1746`) 반환 직후, `_toss_pre_send_hook` 조회(`:1756`)·POST 보다 앞. kind=`new`. 보호 종목 매도 페이로드에 `orderAmount`(`:1473-1478`)가 있으면 **거부**(`protected_quantity_unresolved`) — preflight 가 검사하지 않은 필드로 브로커가 수량을 정하게 두지 않는다 | preflight 보다 앞(fresh 값이 없음) | preflight 의 `fresh_sellable_quantity`(브로커 원값 — C6 을 원천에 적용하지 않는 이유) + H 는 같은 시점 Toss 보유 조회 |
| **G3** | `toss_modify_order` 의 fresh 검사(`:2104`) 직후, `client.modify_order`(`:2125`) 앞. KR 수량 정정 kind=`amend_uncapped`, US 는 기존 전량 preflight 에 추가 | 동일. 기존 US 전량 preflight 를 제거하거나 대체하지 않는다 | 동일 |
| **G4** | `modify_order_impl` 에서 KR `modify_korea_order` 호출(`:1677`) 직전, US `modify_overseas_order` 호출(`:1826`) 직전, Upbit 는 §3.1.1 `cancel_replace` 의 ①(`cancel_and_reorder` 호출 `:1391` 앞)과 ②(`cancel_and_reorder` 내부 `:465`–`:479` 사이) **둘 다** | Upbit ① 을 취소 뒤로 옮기지 않는다(차단 시 원주문만 사라짐). ② 를 생략하지 않는다(취소 중 체결 재장전) | **정정 호출 직전에 새로 읽는다.** `:1289` 의 읽기는 가격 하한 헬퍼 안이고 `new_price is None` 이면 실행되지 않으며 결과가 반환되지도 않는다 — 재사용 금지. `_get_holdings_for_order` 의 `broker_sellable_quantity`·`total_quantity` + 원주문 잔량 fresh 조회 |
| **G5** | 레거시 `kis_trading_service._process_sell_orders_impl` — **권고는 삭제**(§7 Q11). 남긴다면 각 `ops.place_order`(`:410`, `:437`, `:473`) 직전마다 락 안에서 S 를 **다시** 읽는다 | 루프 시작 전 한 번 읽은 S 로 분할 주문 여러 건을 검사하지 않는다 — 조각마다 통과하고 합계가 headroom 을 넘는다(`:432-433`, 루프 `:466-489`) | 매 호출 직전 fresh 읽기 |
| **G6 (정적)** | 테스트: 브로커 매도·정정 송신 함수(`order_korea_stock`, `order_overseas_stock`, `sell_overseas_stock`, `modify_korea_order`, `modify_overseas_order`, `place_sell_order`, `place_market_sell_order`, `cancel_and_reorder`, Toss `place_order`/`modify_order`)의 **호출자 allowlist**. 브로커 클라이언트 내부의 토큰·스로틀 재전송 자기호출(`domestic_orders.py:500,545,1134`, `overseas_orders.py:268,316,1015` — 같은 주문의 재POST)도 목록에서 분류한다. 새 호출 지점이 생기면 분류(가드됨/mock 전용/동일 주문 재전송) 전까지 RED | — | — |

### 3.2 동시성 — 두 요청이 같은 headroom 을 두 번 쓰는 문제

`S=70, P=60` 에서 10주 매도 두 건이 동시에 G1 을 통과하면 둘 다 `10 ≤ 10` 이고, 둘 다 체결되면 보유가
보호 밑으로 간다(`app/services/watch_trigger_repricing/selection.py:16-17` 이 같은 "sellable 이중 계산" 을 이미 지적).
→ **보호 종목에 한해** 가드 검사부터 브로커 송신 응답까지 `(account_scope, market, symbol)` 키의
PostgreSQL advisory lock(세션 레벨, 전용 커넥션)을 잡는다. 두 번째 요청은 락 획득 후 **다시 읽은** S 로
판정하므로 막힌다. 락 대기 상한(예: 10s) 초과는 `protection_state_unavailable` 로 거부.
- 이 락은 **브로커 fencing 이 아니다** — 브로커 앱의 수동 주문, 다른 배포, DB 재시작을 가로지르지 못한다
  (W5 advisory lock 한계와 같은 문구를 런북에 둔다). 그 잔여 위험은 §1.4 의 `encroached/shortfall` 상태로 드러난다.
- 비보호 종목은 락을 잡지 않는다(기존 동작 불변).

### 3.3 fail-closed 규칙 (보호 종목에 한해)

| 상황 | 동작 |
|---|---|
| 브로커 sellable 읽기 실패·비정상(NaN·음수) | 거부 `protected_sellable_unobserved` |
| KIS `ord_psbl_qty` 가 비어 총보유로 폴백됨(`sellable_observed=False`, 폴백 `order_validation.py:817-820`·플래그 `:827`, US `:840-844`·`:850`) | 거부 — 폴백 값은 미체결 매도를 모른다 |
| protected_positions 조회 실패(DB) | **모든 live 매도 거부** `protection_state_unavailable` — 어느 종목이 보호인지 모르면 비보호로 간주할 수 없다. live 매도는 원장 기록에 어차피 DB 가 필요하므로 가용성 손실은 추가로 거의 없다(§7 Q15) |
| drift 미해결(`unverified`) | 거부 |
| 모드 `shadow` | 위 판정을 전부 수행하되 차단하지 않고 `would_block` 이벤트(닫힌 필드: scope·market·symbol·error_code·quantity·headroom)만 기록 |

---

## 4. 부분 체결 · 리컨실 · 분할/병합 · 수동 매도

### 4.1 부분 체결

보호는 로트가 아니라 **브로커 현재값에 대한 바닥**이므로 부분 체결을 따로 추적하지 않는다. 부분 체결된
매도의 잔량은 브로커 `O` 에 남아 `S` 를 계속 줄이고, 다음 판정은 새 S 로 한다. 제안 rung 의 잔량 재발주·
replace 도 G1/G2 를 다시 탄다(T3).

예외는 **정정이 기존 주문의 체결분을 다시 장전할 수 있는 경로**다. `S` 는 기존 주문의 체결로 변하지 않으므로
(H 와 O 가 같이 준다) 스냅샷 잔량 기준 "Δ ≤ 0" 은 안전 근거가 못 된다. Upbit 취소-재발주는 취소 뒤 재조회(§3.1.1
`cancel_replace` ②)로, 브로커 동작이 확인되지 않은 제자리 정정은 신규 주문과 같은 조건(`amend_uncapped`)으로 덮는다.

### 4.2 리컨실

- 리컨실(`kis_live_reconcile_orders_impl` `app/mcp_server/tooling/kis_live_ledger.py:912`,
  `live_reconcile_orders_impl` `live_order_ledger.py:548`, `toss_reconcile_orders_impl` `toss_live_ledger.py:806`)과
  execution ledger ingest(`app/services/execution_ledger/fill_ingest.py`)는 **변경하지 않는다.** 보호는 체결을
  기록·거부·수정하지 않는다(하드룰 5·16: 원장 커밋이 권한자).
- 필요한 것은 관측뿐: drift 판정(§4.3)이 원장의 순체결량을 **읽는다**
  (`execution_ledger/repository.py:241` `net_quantity_by_match_key_since` 류). 쓰기 0.
- 저널 FIFO 마감(`order_journal.py:215` `_close_journals_on_sell`)은 전술 매도가 장기 매수 저널을 먼저 닫을
  수 있다 — 회계 귀속 문제이지 보호 문제는 아니다. 범위 밖(§7 Q16).

### 4.3 분할·병합·종목코드 변경 — drift 판정

레포에는 corporate action 처리가 **없다**(`docs/runbooks/forecast-terminal-close.md:89-97` "unsupported",
ROB-1043 로 이연). 그 결과:
- **역분할·감자**: H 가 줄어 `shortfall` → 전술 매도 차단(fail-closed, 안전).
- **액면분할**: H 가 늘고 P 는 그대로 → **보호 비율이 조용히 줄어든다(fail-open)**. 1:5 분할이면 P=60/H=100
  (60%) 이 P=60/H=500(12%) 이 된다. 가장 위험한 구멍.
- **종목코드 변경·합병**: 옛 코드 행은 H=0 → `shortfall`, 새 코드는 **보호 없음(fail-open)**.

대책 — **drift 판정**(스케줄러 없이 읽기 시점 계산, 하드룰 6):
`expected_H = last_confirmed_broker_held + Σ(원장 순체결: 매수 − 매도, last_confirmed_at 이후, 이 scope·symbol)`.
G 가드와 보유 화면이 보호 종목을 읽을 때 `H ≠ expected_H`(수량 단위 허용오차 0, crypto 는 스텝 단위)이면
`unverified` → 전술 매도 차단 + 운영자 재확인 요청. 운영자가 화면에서 "재확인"(`action=reconfirm`, 새 H 기록,
필요 시 P 조정)하면 풀린다.
- 원장에 없는 체결(브로커 앱 수동 거래, 입출고)도 drift 로 잡힌다 — 의도된 동작이다. **누가 결정하나 = 운영자**.
- 종목코드 변경으로 새 코드가 생기는 경우는 drift 로도 못 잡는다(새 코드엔 행이 없다). 옛 코드 행이
  `shortfall`(H=0)로 뜨는 것을 "고아 보호" 경고로 표시하고, 런북 절차로 운영자가 새 코드에 재선언한다. 잔여 위험으로 명시.
- 원장이 모든 체결을 담는지(웹소켓 fill 이 브로커 앱 주문까지 받는지)는 브로커별 **미확인** — 안 담기면
  drift 가 더 자주 뜨는 쪽(fail-closed)으로만 틀린다(§7 Q4).

### 4.4 운영자 수동 매도 — 보호 수량을 줄이나?

| 경우 | 동작 (권고 기본값) |
|---|---|
| auto_trader 로 보호분까지 팔고 싶다 | **먼저 화면에서 P 를 낮추고(확인 포함) 그다음 평소 경로로 판다.** 호출 단위 우회 플래그(`consume_protected=True` 류)는 두지 않는다 — MCP consumer 가 부를 수 있는 우회는 전술 레인이 부를 수 있는 우회다 |
| 브로커 앱에서 직접 팔았다 | P 는 **자동으로 줄지 않는다.** H<P 면 `shortfall`, H 가 원장과 어긋나면 `unverified` → 전술 매도 차단 + 경고. 운영자가 P 를 H 이하로 낮추거나 재확인한다 |
| shortfall 상태에서 전술 매수로 다시 채워진다 | P 가 그대로이므로 **새 매수분이 먼저 보호를 메운다**(산술 귀결). 화면에 "보호 부족 N주 — 매수분이 먼저 보호로 들어감" 을 표시 |

P 를 H 로 자동 절삭하지 않는 이유: 브로커 조회가 일시적으로 빈 목록·0 을 돌려주는 순간(KIS `_filter_nonzero_holdings`
`account.py:534` 는 0 행을 지운다) 보호가 영구 삭제된다. 읽기 오류가 보호를 푸는 방향으로 작동하면 안 된다.
대가는 운영자 1회 확인이다.

---

## 5. 운영자 UI / CLI

### 5.1 설정 화면 `/invest/settings/protected-positions`

manual_cash 화면(`app/routers/invest_manual_cash.py`, `app/services/manual_cash_settings.py`,
`frontend/invest/src/pages/ManualCashSettingsRoute.tsx`) 패턴을 그대로 따른다.

- `GET /invest/api/settings/protected-positions` — 세션 인증, `can_edit` 반환. 행마다 scope·market·symbol·이름·
  P·H·S·headroom·state·마지막 개정(누가·언제·사유)·개정 이력 링크.
- `PUT …/{scope}/{market}/{symbol}` — `require_admin` + CSRF. 본문(`extra="forbid"`):
  `protected_quantity`(Decimal 문자열, bool/float/NaN 거부), `reason`(필수), `expected_revision`,
  `idempotency_key`, `confirm_protection_change: StrictBool`, 감소·해제일 때 `confirm_symbol`(종목코드 재입력 일치).
- 서버 강제(화면만이 아니라): 모든 변경은 `confirm_protection_change=true` 없이는 409 `confirm_required`
  (응답에 이전/이후 P, 신선한 H·S, 변경 후 headroom 을 담아 대화상자가 그대로 보여 준다). 감소·해제는
  `confirm_symbol` 불일치 시 409. 증가는 신선한 H 읽기 실패·`P_new > H` 시 422. `expected_revision` 불일치 409 `stale_form`.
- 대화상자: "전술 매도 가능 수량이 A → B 로 바뀝니다" 를 가장 크게. 초기 포커스는 '취소'(manual_cash 와 동일).
- 모드 배너: 그 행 스코프의 `PROTECTED_QUANTITY_MODE_*` 가 `off`/`shadow` 면 화면 상단에 **"보호 미강제 — 표시·기록만"** 을
  고정 표시한다. 운영자가 보호가 걸려 있다고 오인하지 않게.

### 5.2 포트폴리오 표시

- 통합 보유표 `frontend/invest/src/components/my/UnifiedHoldingsTable.tsx` `QuantityCell`(`:85`, 기존 "주문대기" 칩 `:97-99`
  옆)에 **"장기 보호 N"** 칩, 매도가능은 tactical 값. 계좌별 분해 `BreakdownLine`(`:111`)에 계좌별 보호.
  `encroached/shortfall/unverified` 는 경고색 칩 + 툴팁 사유.
- 포트폴리오 개요 계좌 컴포넌트(`app/services/portfolio_overview_service.py:731` `_group_components_by_position`,
  `account_key`)와 `MergedHolding`(`merged_portfolio_service.py:76`, 부착 `:342-374`)에 같은 필드.
- MCP `get_holdings`(C7)·`get_protected_positions`(읽기 전용). 세션 브리핑(`get_operating_briefing`)에 보호 종목
  수와 비정상 상태 목록 한 섹션(선택, §7 Q14).

### 5.3 CLI

`scripts/protected_positions.py` — `list` / `show <scope> <market> <symbol>` / `history …` **읽기 전용**.
쓰기 하위명령은 두지 않는 것을 권고(§7 Q7): 에이전트 세션도 셸을 가진다. 두게 된다면 서비스 레이어 경유,
TTY 필수(비대화형 거부), 종목코드·수량 재입력, `origin='operator_cli'`, 기본 dry-run.

---

## 6. 테스트 · 롤아웃 · 롤백

### 6.1 테스트 (test DB · 브로커 fake 만. 실 브로커·프로덕션 DB 0)

**단위**
- 헤드룸 산술 표 테스트: `(S, P, q, baseline)` 격자 — 경계(`q = S−P` 통과, `S−P+ε` 거부), `P=0`, `S<P`, `H<P`,
  Decimal 소수(crypto 8자리), None·NaN·음수·bool 거부.
- 상태 분류(`covered/encroached/shortfall/unverified`), drift 계산(원장 fake 로 체결 합).
- 서비스 쓰기: `P_new > H` 거부, 신선한 H 읽기 실패 시 증가 거부, `expected_revision`·`confirm_*`·`confirm_symbol`
  409, 멱등키 재전송 = 같은 revision, head 가 revision 재생으로 재구성됨.
- DB: append-only 트리거(UPDATE/DELETE/TRUNCATE 거부), CHECK(scope·market 조합, 음수 거부).

**소비자별 + 뮤턴트 (각 C·G 마다 1쌍)**
- C1–C7: 보호 종목에서 반환 수량이 tactical 로 줄었는지 / 비보호·mock 에서 원값 그대로인지.
- G1–G5: 헤드룸 초과 요청 → 거부 **그리고 브로커 송신 spy 호출 0회**. 뮤턴트 = 가드 호출 삭제·비교 부등호
  반전·`is_mock` 조건 반전 → 테스트가 **assertion 으로** RED(예외 종료는 유효 뮤턴트 아님 — 빌더 규약).
- G1 위치 테스트: 차단 시 `OrderSendIntentService.reserve` 미호출(멱등키 미소모), kis_mock 귀속 게이트가 여전히 첫 단계.
- G4 Upbit ①: 사전검사 차단 시 취소 요청 0회.
- G4 Upbit ②(**#693 r1 BLOCKER 회귀 테스트**): fake Upbit 에서 H=100·P=60·잔여 매도 40·S=60, 가격만 정정 요청,
  취소 처리 중 25주 체결을 주입 → 재발주 0회, 응답 "취소됨·재발주 보류", 최종 H ≥ P. 뮤턴트 = ② 삭제 → 재발주 40 이
  나가고 assertion RED.
- `amend_uncapped`: KIS US·Toss KR 에서 headroom 0 인 종목의 가격만 정정 → 거부(송신 0). `amend_broker_capped` 로
  분류된 경로만 가격 정정 통과.
- 이중 차감 방지: C1 치환 뒤 G1 이 `broker_sellable_quantity` 를 읽는지 — `S=100, P=60, q=40` 이 통과(뮤턴트 = G1 이
  `quantity` 를 읽게 바꾸면 40 이 거부되어 RED).
- 키 정규화: 스코프마다 가드 조회 키 = 그 스코프 live 원장이 저장하는 `(market, symbol)` 과 동일. `equity_kr` 를 그대로
  넘기는 뮤턴트 → 보호 종목이 통과해 RED. 미지의 market 값 → 거부.
- G2 `orderAmount`: 보호 종목 매도에 `order_amount` 동반 → 거부, 송신 0.
- G5(삭제하지 않는 경우): 분할 3건의 합이 headroom 초과 → 초과분 조각 거부.
- G6 정적 allowlist: 송신 함수 호출자 집합이 고정 목록과 다르면 RED.
- 동시성: 헤드룸 10 에 10주 두 건 동시 → 정확히 한 건만 송신.
- fail-closed: sellable 미관측·KIS 폴백·DB 조회 실패·drift → 거부 + 송신 0.
- shadow 모드: 판정은 `would_block` 기록, 송신은 발생.

**종단 fixture 하나** — fake KIS(KR)·Toss·Upbit 계좌 `H=100, S=100, P=60`:
1. 세션 경로: `get_holdings` 가 tactical 40 을 보인다.
2. `order_proposal_create`(sell 45) → 자동승인 켠 fake 설정 → `revalidate_and_submit` → G1 거부, 송신 0, 제안 rung 거부 상태.
3. sell 40 → 통과, 송신 1. 이어서 sell 1 → 거부(S=60, P=60).
4. 정정 증량 +1 → G4 거부. KIS KR(`amend_broker_capped` 확인 후) 가격만 정정 → 통과, 미확인 상태면 거부.
   Upbit 가격만 정정 중 부분체결 주입 → 재발주 보류.
5. fake 브로커 H 를 50 으로(앱 수동 매도 모사) → `shortfall`, 모든 전술 매도 거부, P 불변.
6. fake H 를 500 으로(분할 모사) → `unverified`, 거부 → 운영자 reconfirm API → 풀림.
7. 모드 shadow 로 재실행 → 같은 판정이 `would_block` 으로만 남는다.

### 6.2 롤아웃 (dark → enforce)

| 단계 | 내용 | 게이트 |
|---|---|---|
| R0 | 마이그레이션(additive) — 운영자가 `alembic upgrade head` | — |
| R1 | 서비스·읽기 표면(C5·C7 필드 추가, 단 **값 치환은 아직 안 함**)·설정 화면 쓰기. 운영자가 보호를 입력 | 모든 스코프 `off`(기본) |
| R2 | shadow: G1–G5 판정 + `would_block` 기록, L1 치환 없음. 최소 N 거래일(권고 10) 관측 — 오탐(`unverified` 빈도)·누락 확인 | 스코프별 `shadow` |
| R3 | enforce: L1 치환 + G 차단. **스코프 단위로** 올린다 — `toss_live` 는 Q13 확정 전 `enforce` 불가(설정 검증이 기동 실패로 거부), KIS KR 제자리 정정은 Q19 확정 전 `amend_uncapped` | 스코프별 `enforce` — 운영자 명시 전환 |

**모드는 스코프별이다.** `PROTECTED_QUANTITY_MODE_KIS_LIVE` · `PROTECTED_QUANTITY_MODE_TOSS_LIVE` ·
`PROTECTED_QUANTITY_MODE_UPBIT_LIVE` ∈ `{off, shadow, enforce}`, 전부 기본 `off`. 단일 전역 플래그는 두지 않는다 —
Q13(Toss sellable 의미 미확인)과 R3 를 동시에 지킬 수 없기 때문이다. `PROTECTED_QUANTITY_TOSS_SELLABLE_VERIFIED`
(기본 false)가 false 인 동안 `..._TOSS_LIVE=enforce` 는 설정 오류로 기동을 막는다.

- 스케줄러·자동 트리거 0(하드룰 6). drift 는 읽기 시점 계산.
- 모드 값 외의 입력은 `off` 가 아니라 **enforce 로 해석하지 않고 기동 실패**(설정 오타가 보호를 조용히 끄거나
  켜지 않게).

### 6.3 롤백

- 1차: 해당 스코프의 `PROTECTED_QUANTITY_MODE_*` 를 `shadow`/`off` 로 되돌리고 재시작. 데이터(선언·이력)는 남는다.
  🔴 enforce → shadow 는 **안전 게이트 완화**이므로 운영자 결정 사항이다(하드룰 4 정신).
- 2차: 코드 revert(PR 단위). 테이블은 남겨 두고, 드롭은 이력 export 후 별도 승인.
- 롤백 중 화면 배너가 "보호 미강제" 로 바뀌는 것이 운영자에게 보이는 유일한 신호가 되지 않도록, 모드 전환 시
  기존 Telegram notifier 로 1회 알림(구현 선택).

---

## 7. 운영자 결정이 필요한 질문 (권고 기본값 포함)

| # | 질문 | 권고 기본값 |
|---|---|---|
| Q1 | 브로커 앱에서 장기분을 직접 팔면 P 를 자동으로 줄이나? | **아니오.** `shortfall/unverified` 로 전술 매도를 막고 경고, 운영자가 P 를 낮춘다(§4.4) |
| Q2 | auto_trader 로 장기분을 팔 때 "보호 소진 매도" 모드를 둘까? | **두지 않는다.** 화면에서 P 를 먼저 낮추고 판다 |
| Q3 | mock/paper/demo 계좌도 보호 대상인가? | **아니오** — live 3 스코프만 |
| Q4 | 분할·합병 대응을 drift 판정(원장 대조, 불일치 시 차단)으로 할까? 원장이 브로커 앱 체결을 못 담으면 드리프트 경고가 잦아진다 | **예**, 오탐은 fail-closed 쪽으로만 틀린다. 빈도는 R2 shadow 에서 측정 후 결정 |
| Q5 | 보호 선언 시 이미 걸린 매도 주문이 보호를 침범(`encroached`)하면? | 선언은 허용 + 경고, **자동 취소 안 함** |
| Q6 | 세션의 직접 MCP 발주(`place_order` 등)도 막나, 운영자 수동 스크리너 주문(T10)도 막나? | **둘 다 막는다.** 가드는 경로를 구분하지 않는다. 운영자는 P 를 낮추면 된다 |
| Q7 | CLI 에 쓰기 명령을 둘까? | **읽기 전용**, 쓰기는 화면만 |
| Q8 | shortfall 뒤 새 매수분이 먼저 보호를 메우는 것(산술 귀결)을 받아들이나? | **예**, 화면에 명시. 싫다면 운영자가 P 를 낮춘다 |
| Q9 | 목적 어휘를 `long_term` 하나로 시작하나? | **예**, 닫힌 enum. 확장은 스키마 변경으로 |
| Q10 | 감소·해제 때 브로커 읽기 실패면? | **거부**(증거 없는 변경 금지). 브로커 복구 뒤 재시도 |
| Q11 | 미배선 레거시 KIS 자동매도 잡(C8·S-D)은? | **별도 PR 로 삭제**. 삭제 전이면 G5 가드 |
| Q12 | 배포당 브로커별 live 계좌 1개 가정을 유지하나? | **예**. 계좌가 늘면 키 확장 마이그레이션 선행 |
| Q13 | Toss `sellableQuantity` 가 미체결 매도를 빼는지 | 구현 PR 에서 실모양 fixture·문서로 확정. **확정 전 `toss_live` enforce 금지** — `PROTECTED_QUANTITY_TOSS_SELLABLE_VERIFIED=false` 동안 설정 검증이 거부(§6.2). 차감하지 않는다면 Toss 공식은 `q ≤ S − O_pending − P` |
| Q14 | 자문 코드(portfolio action·rotation·브리핑)에 보호 상태를 표시할까? | 브리핑·get_holdings 만. 자문 %는 변경 없음 |
| Q15 | protected_positions 조회 실패 시 모든 live 매도를 막나? | **예**(어느 종목이 보호인지 모르면 전부 보호로 간주) |
| Q16 | 저널 FIFO 가 전술 매도로 장기 매수 저널을 먼저 닫는 문제 | 범위 밖, 후속 이슈 |
| Q17 | 정정 수량 상한 부재(§2.3)·시장가 프리뷰 결함(§2.5)은 보호와 별개로 먼저 고칠까? | 정정 상한은 **G4 와 함께** 이 기능 PR 에서, 프리뷰 결함은 별도 이슈 |
| Q18 | shadow 관측 기간 | 10 거래일 |
| Q19 | KIS KR 정정(`QTY_ALL_ORD_YN="Y"`)이 브로커의 **실제 잔량**에만 적용되어 체결분을 재장전하지 못하는가? KIS US `ORD_QTY`·Toss KR 수량 정정은? | 확인 전에는 전부 `amend_uncapped`(가격만 정정도 `q ≤ S − P`). 확인은 KIS 문서/실모양 응답으로 하고, 라이브 실험으로 증명하지 않는다 |

## 부록 A. 이 문서가 바꾸지 않는 것

- 브로커 게이트·호스트 allowlist·confirm 이중 게이트·하드 인바리언트 상수(하드룰 4).
- 원장 쓰기 경로, 리컨실 커널, execution ledger ingest(하드룰 5·16).
- W5 콜백 코어·재시도 대수(하드룰 11) — 가드는 코어가 부르는 `_place_order_impl`/`toss_place_order` 안쪽에 있으므로
  콜백 쪽 변경 0. 가드 거부는 기존 sellable 부족 거부(`order_validation.py:1408`, Toss `:860`)와 같은 에러 반환 경로를 탄다 — 브로커에 닿기 전 반환이므로 재시도 대수의 "코어 진입 후 모호" 부류를 새로 만들지 않는다.
- kis_mock 귀속 게이트 위치(하드룰 7).
- 스케줄러 등록 0(하드룰 6).
