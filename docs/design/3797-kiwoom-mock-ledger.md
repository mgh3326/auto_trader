# #3797 — Kiwoom 모의 주문 원장 · execution_ledger 대사 · 제안 집행 (설계)

- 상태: **설계 문서 r1 — 코드 변경 0.** 구현은 아래 §6 티켓 분할대로 별도 PR에서 한다.
- 상위 작업: hk 3797 (auto_trader 새 전략) 의 step 1. hk task 4635.
- 입력:
  - 운영자 결정 `decision/2026-11-10/at-new-strategy-go` 수정 2(§"수정 2 — 키움·NH"):
    키움 모의를 최대한 쓰되 주문 원장·체결 대사·제안 집행을 **실전 전환 시 호스트·자격증명·게이트만
    바뀌도록** 만든다. 자문 사실 목록이 이름 붙인 결측은: 주문 원장 테이블, `execution_ledger`
    대사, `order_proposals` 계좌 모드/자동승인 허용 목록/승인 창 규칙, 보유·가용자금 합류.
  - `research/2026-11-10/broker-api-recheck` 행 1–4: KR 모의 KRX 전용(unchanged), 모의 공통 TR
    "국내주식 1초 1회, 미국주식 1초 1회"(공식 한도 보완), `kt00010` 모의 지원 미확인(could not
    verify), US 모의는 별도 앱키·주문번호 문서상 9자리(일부 could not verify).
  - 계약 파일 `docs/agent-contracts/broker-and-ledger-contracts.md` "Kiwoom Mock Account
    Lifecycle (ROB-97 / ROB-319)"(파일 내 831행 시작), "Execution Ledger HTTP Ingest /
    Quarantine / ROB-1379"(292–431행), "토스증권 Open API"(904행), KIS live 절들.
- 기준 코드: `origin/main` (`e1db462ed`). 모든 `path:line` 은 이 커밋에서 읽었다.
- 범위 밖: 코드·마이그레이션·NHPLUG·스케줄 등록. 실전 계좌 활성화도 범위 밖 — 실전 호스트
  주문 허용은 이 결정에 없고 그때 따로 승인받는다(수정 2 자문 메모).

## 0. 요약

모의 계좌의 주문을 **accepted-only 원장 + 증거 우선 대사 + 제안 집행 표면**으로 만든다.
패턴은 KIS live(`review.kis_live_order_ledger` + `kis_live_reconcile_orders_impl`)와
Toss(`review.toss_live_order_ledger` + `toss_reconcile_orders`) 선례를 그대로 따르되,
실전 전환을 염두에 두고 브로커 차이점(호스트·자격증명·TR 계약·게이트 이름)만 설정으로
남긴다. 브로커에 멱등키 필드가 없으므로 KIS/NH와 같은 "송신 전 예약 + 단일 소비" 구조를
`review.order_send_intents` 재사용으로 해결한다.

| 규칙 | 내용 |
|---|---|
| **K1 accepted-only at send** | 브로커 ACK(return_code=0 + `ord_no`) 없이 `accepted` 행을 쓰지 않는다(I1: accepted ⇒ broker_order_id). 사전 상태·거절은 `rejected`, 송신 후 결과 불명은 ACK 없이도 `uncertain` 행으로 반드시 남기며(I3 이 이 행에 의존), 둘 다 재송신을 허용하지 않는다. |
| **K2 증거 우선** | `filled` 마킹은 브로커 조회 증거(체결 수량·가격)가 있을 때만. 부재는 증거가 아니다 — 목록에 없는 주문은 `cancelled` 가 아니라 `unknown`/수동검토다. |
| **K3 단일 송신 보장** | `review.order_send_intents` 의 `(account_scope, idempotency_key)` UNIQUE 로 같은 키 재송신을 fail-closed. 키는 canonical 주문 + 거래일 소금 + rung 판별자로 결정적으로 만든다(Toss ROB-651 패턴). |
| **K4 실전 동형** | 원장 스키마·대사 커널·제안 경로는 모의/실전이 같은 코드를 쓰고, 다른 것은 호스트·자격증명·env 게이트·계좌 모드 값뿐이다(§5 표). |
| **K5 계약 우회 금지** | 기존 경계(KRX-only, default-disabled 게이트, dry_run+confirm, ROB-1340 권한 종료, 서비스 레이어 경유 쓰기)를 완화하지 않는다. 새 표면은 그 뒤에 추가된다. |

## 1. 배경 — 이미 있는 것과 없는 것

### 1.1 이미 있는 자산

| 자산 | 위치 | 역할 |
|---|---|---|
| KR 모의 MCP 8도구 | `app/mcp_server/tooling/orders_kiwoom_variants.py:63-71` (`KIWOOM_MOCK_TOOL_NAMES`), `account_mode="kiwoom_mock"` 고정 `:53` | preview/place/modify/cancel/order history·detail/positions/orderable cash |
| US 모의 MCP 변형 | `app/mcp_server/tooling/orders_kiwoom_us_variants.py` (account_mode `kiwoom_mock_us`) | 별도 앱키 4종 |
| 송신 경계 | `_kiwoom_mock_place_order_impl` `orders_kiwoom_variants.py:423` — dry_run+confirm 이중 게이트, preflight 직후 `place_buy_order`/`place_sell_order` `:503-511`, `KiwoomPreDispatchError`(미송신 확정) vs `_dispatch_unknown_response`(송신 불명) `:383-416` | 여기가 원장 쓰기 지점 |
| 주문/조회 TR | `app/services/brokers/kiwoom/constants.py:27-36` (kt10000~kt10003 주문, kt00007 주문체결상세, kt00009 주문체결현황, kt00001 예수금, kt00018 잔고), `:167-178` (US ust20000~20003, ust21050/21070/21510/21160) | 대사 입력 |
| KRX-only 강제 | `MOCK_REJECTED_EXCHANGES={"NXT","SOR"}` `constants.py:54`, 주문 경로 `dmst_stex_tp=KRX` 고정 `domestic_orders.py:88` (`_ensure_krx` `:39`), tooling 사전거부 `orders_kiwoom_variants.py:121-127` | 네트워크 호출 전 거부 |
| 게이트 | `validate_kiwoom_mock_config` `app/core/config.py:1465-1471` (`KIWOOM_MOCK_ENABLED`+APP_KEY/SECRET/ACCOUNT_NO), US `validate_kiwoom_mock_us_config` `:1499-1512`, `KiwoomMockClient` 가 mock host 외 거부 `client.py:135-140` (`MOCK_BASE_URL` `constants.py:10`) | fail-closed |
| 주문번호 형식 | KR `ord_no` 는 응답 문자열, US 는 문서상 9자리·수용 범위 1–18자리 `us_orders.py:15-21` (`DOCUMENTED_ORDER_ID_LENGTH = 9` `:21`, `validate_us_order_id` `:42-46`) | 멱등·대사 키 |
| 사전 점검 | `app/services/brokers/kiwoom/order_preflight.py` — fresh quote 필수 `:108-122`, KRX 틱 스냅 `:124-145`, 매도가능 `_check_sellable` `:206`, 매수가용 `_check_buy_cash` `:283` | 송신 직전 증거 |
| 송신 전 예약 | `review.order_send_intents` `(account_scope, idempotency_key)` UNIQUE `app/models/review.py:673-692` — Kiwoom B0x 경로도 같은 테이블을 claim 저장소로 씀 `app/models/kiwoom_coordination_lifecycle.py:3-4` | 로컬 이중송신 차단 |
| B0x 유한송신 수명주기 | `review.kiwoom_coordination_lifecycle` (claim_account_scope·idempotency_key·order_attempt_id UNIQUE, dispatch_kind `acknowledged`/`definitive_without_broker_id`/`lane_reported_uncertain`/`callback_failed`/`ack_attachment_failed`) `app/models/kiwoom_coordination_lifecycle.py:45-115`, `scripts/b0x/kr/kiwoom_ordering.py`, `app/services/mock_integration/coordination.py` | 불명 결과의 타입화된 증거(레인 한정) |
| 권한 종료 증거 | `kiwoom_authority_attempts` / `kiwoom_authority_cessation_receipts` `app/services/brokers/kiwoom/coordination_store.py:54-93`, ROB-1340 계약 `broker-and-ledger-contracts.md:832-850` | 배치 취소 권한 |
| 차선 한정자 | `mock_lane_registry.py:146` `kr.kiwoom.mock`, 호스트 허용 `:197` | 레인 정체성 |

### 1.2 없는 것 (이 문서가 채울 것)

| 결측 | 증거 | 설계 위치 |
|---|---|---|
| 주문 원장 | `review.kiwoom_mock_order_ledger` 부재 — 송신 후 질의·대사할 영속 행이 없다 | §2.1 |
| 귀속(신호) 원장 | `review.kiwoom_mock_signal_ledger` 부재 — kis_mock 하드룰 7 등가물 없음. `TradeRetrospective` CHECK 와 `_KR_ACCOUNT_MODES` 에는 `kiwoom_mock` 어휘가 이미 있다 `app/models/review.py:1221`, `app/services/decision_table_validate/one_share_exception.py:120` | §2.4, §4.5 |
| execution_ledger 도달 | `review.execution_ledger.broker` CHECK 가 `('kis','upbit','toss')` `app/models/execution_ledger.py:58`, reconcile-runs broker CHECK `('kis','upbit')` `:188` — `'kiwoom'` 추가가 필요 | §2.3 |
| 대사 커널 | Kiwoom 용 `reconcile_orders` 없음 — kt00009/kt00007/ust21510 결과를 종결 분류하는 커널 부재 | §3 |
| 제안 집행 | `order_proposals` CHECK 어휘 `'kis_live','kis_mock','toss_live','upbit','db_simulated'` `app/models/order_proposals.py:40` 에 `kiwoom_mock` 없음; 제출 가능 집합 `_SUBMITTABLE_ACCOUNT_MODE_MARKETS` `app/services/order_proposals/service.py:354-362` 에도 없음 | §4 |
| 세션 규칙 | `approval_window.py` 세션 분기는 `upbit/crypto`·`toss_live`·`kis_live` 만 처리하고 나머지는 `unsupported` fail-closed `:1017-1026` | §4.2 |
| 보유·가용 합류 | `default_buying_power_reader` 는 `toss_live` 만 읽고 나머지는 None(fail-open) `app/services/order_proposals/buying_power.py:182-198` | §4.4 |

### 1.3 선례 (복사 대상, 느슨하게 하지 않는다)

| 선례 | 위치 | 차용할 것 |
|---|---|---|
| Toss accepted-only 레저 | `toss_live_order_ledger` UNIQUE `client_order_id`/`broker_order_id`, status CHECK `app/models/review.py:694-730`; `record_send` 멱등 재생 `app/services/toss_live_order_ledger_service.py:100-160` ("같은 client_order_id + 다른 broker_order_id → anomaly raise") | 테이블 형태, 멱등 replay |
| Toss 실행 원장 브리지 | `build_toss_execution_ledger_upsert` `app/services/toss_execution_ledger.py:57-107` — broker 고정, venue/instrument 매핑, `source="reconciler"`, 결정적 `fill_seq` 해시 | execution_ledger upsert 형태 |
| KIS 대사 커널 | `_reconcile_one_ledger_row` `app/mcp_server/tooling/kis_live_ledger.py:851-1050` — `classify_fill_evidence` → FILLED/PARTIAL/PENDING/NONE, `classify_day_order_expiry` → cancelled/expired/pending `app/services/brokers/kis/live_order_expiry.py:272-330`, delta-멱등 부킹 | 종결 분류 대수 |
| #2400 terminal-only | `scripts/kis_live_auto_reconcile.py:31-105`, `scripts/toss_live_auto_reconcile.py:11-70` — `--apply-terminal-only` 는 단일 게이트, full `--apply` 는 두 플래그 | CLI·게이트 형태 |
| kis_mock 귀속 | `review.kis_mock_signal_ledger` `alembic/versions/20260803_kis_mock_signal_ledger.py` + pre-submit 게이트 `app/mcp_server/tooling/order_execution.py:996-1070` (`attribution_required`/`signal_record_unavailable`) | 신호 원장 + 게이트 |
| 멱등 예약 | ROB-653 P6-B `review.order_send_intents` `app/models/review.py:665-692` | 송신 전 예약 |

## 2. 데이터 형태

### 2.1 `review.kiwoom_mock_order_ledger` (신규, 추가형 마이그레이션 1건)

하나의 테이블에 KR·US 모의를 둔다(Toss `market` 컬럼 선례 `app/models/review.py:715`).
계좌 자격이 다르므로 `account_mode` 로 구분하고 `market` 과 일치를 CHECK 한다.

| 컬럼 | 타입 | 규칙 | 선례 |
|---|---|---|---|
| `id` | BigInteger PK | autoincrement | 공통 |
| `trade_date` | timestamptz NOT NULL | 송신(ACK) 시각 | kis_live:25 |
| `trading_day` | Text NOT NULL | KR=KST `YYYYMMDD`, US=미국 거래일(ROB-1379 `us_trading_day_window`) | ROB-1379 거래일 전제 |
| `symbol` / `raw_symbol` | Text NOT NULL | 현재 writer는 두 컬럼에 같은 입력값을 쓴다(브로커 원문 별도 보존은 없다) | `app/services/kiwoom_mock_order_ledger_service.py:903-904` |
| `instrument_type` | Text NOT NULL | `equity_kr`/`equity_us` | kis_live:27 |
| `market` | Text NOT NULL CHECK `IN ('kr','us')` | account_mode 와 정합: `kiwoom_mock↔kr`, `kiwoom_mock_us↔us` | toss CHECK `app/models/review.py:715` |
| `side` / `order_type` | Text | side `buy|sell`; order_type KR `limit` 고정(`trde_tp='0'` `domestic_orders.py:91`), US `limit|market`(`US_SUPPORTED_TRDE_TP` `constants.py:183-185`) | — |
| `operation_kind` | Text CHECK `IN ('place','modify','cancel')` | 스키마는 정정·취소도 같은 원장 행으로 표현할 수 있게 한다 — 현재 송신 경로는 `place` 만 쓴다 | toss `:706-710` |
| `quantity` / `price` / `amount` | Numeric | 지정가 필수(KR), 시장가는 price NULL 허용(US) | kis_live:30-32 |
| `currency` | Text CHECK `IN ('KRW','USD')` | market 과 정합 | — |
| `client_order_id` | Text UNIQUE NOT NULL | **우리가 발행하는 멱등키**(아래 §2.2). 브로커 필드가 없어 wire에는 안 나간다 | toss `clientOrderId` |
| `idempotency_key` | Text NOT NULL | `order_send_intents` 예약과 같은 값 | ROB-653 |
| `approval_hash` | Text NULL | kis_live 동형 자리지만 현재 모의 place 경로는 채우지 않는다(NULL) | `app/services/kiwoom_mock_order_ledger_service.py:916`, `app/services/kiwoom_mock_place.py:950-958,1464-1473` |
| `correlation_id` / `report_item_uuid` | Text / UUID NULL | 스키마는 두지만 현재 place 경로는 채우지 않는다 — 귀속 `correlation_id` 는 `kiwoom_mock_signal_ledger` 가 소유 | `app/services/kiwoom_mock_order_ledger_service.py:917-918`, `app/services/kiwoom_mock_place.py:950-958,1464-1473` |
| `broker_order_id` | Text NULL | `ord_no`(KR)·US ord_no(문서상 9자리, 수용 1–18 `us_orders.py:20-21`) | — |
| `original_order_id` | Text NULL | modify/cancel 이 가리키는 원주문 | toss |
| `account_mode` | Text CHECK `IN ('kiwoom_mock','kiwoom_mock_us')` | tooling 고정값과 동일 `orders_kiwoom_variants.py:53` | — |
| `broker` | Text CHECK `= 'kiwoom'` | | toss `:704` |
| `status` | Text CHECK (아래) | `accepted`/`rejected`/`pending`/`partial`/`filled`/`cancelled`/`expired`/`replaced`/`uncertain`/`anomaly`/`requires_manual_review` | toss `:716-724` + `uncertain` 추가 |
| `lifecycle_state` | Text | kis_mock 상태 어휘 재사용 `d4f1c5a3e2b1:20-30` (`planned`~`anomaly`) | — |
| `response_code`/`response_message`/`raw_response` | Text/JSONB | 브로커 `return_code`/`return_msg` + 원문 | kis_live:42-44 |
| `filled_qty`/`avg_fill_price`/`fee` | Numeric | 대사가 `filled_qty`·`avg_fill_price` 를 채우고 `fee` 는 모의 응답에 없어 NULL 유지 | `app/services/kiwoom_mock_order_ledger_service.py:771-776`, `app/services/kiwoom_execution_ledger.py:138-142` |
| `trade_id`/`journal_id` | BigInt NULL | 현재 full 대사는 채우지 않는다 — journal/trade 부킹은 hk 4802 공백 | `app/services/kiwoom_mock_reconcile.py:22-25,673-679` |
| `reconcile_attempts`/`last_reconcile_detail`/`reconciled_at` | Int/JSONB/timestamptz | 대사 이력 | `app/services/kiwoom_mock_order_ledger_service.py:777-792` |
| `strategy`/`thesis`/`reason`/`notes`/`exit_intent`/`exit_reason`/`indicators_snapshot` | Text/JSONB | kis_live 동형 맥락 컬럼 — 현재 place 경로는 `strategy`/`reason` 도 넘기지 않아 NULL이고, 귀속·주문 맥락은 신호/제안 원장이 소유한다 | `app/models/review.py:987-993`, `app/services/kiwoom_mock_order_ledger_service.py:926-927` |
| `created_at`/`updated_at` | timestamptz | | 공통 |

인덱스·유일성:

- `UNIQUE(client_order_id)` — 멱등 replay (toss `record_send` 와 같은 규약).
- `UNIQUE(account_mode, trading_day, broker_order_id)` — 브로커 주문번호의 거래일 재사용
  가능성을 KIS 와 동일하게 가정(ROB-1379 전제 `broker-and-ledger-contracts.md:399-401`).
  **미검증 전제**: Kiwoom `ord_no` 의 재사용 여부는 공식 문서에서 확인되지 않았다
  (research 행 4 could not verify) — 날짜 포함 유일키는 재사용이 없어도 안전하다.
- 부분 유일 인덱스 `uq_kiwoom_mock_order_ledger_open_operation` on
  `(account_mode, trading_day, original_order_id)` — 술어는
  `operation_kind IN ('modify','cancel') AND status NOT IN ('rejected','filled','cancelled','expired','replaced')`.
  같은 원주문에 대한 동시 정정·취소 경쟁을 막는다(`app/models/review.py:932-939`,
  `alembic/versions/20261115_hk4703_kiwoom_ledger.py:277-284`).
- `ix (status)`, `ix (market, symbol)`, `ix (report_item_uuid)` — 대사·리포트 스캔.
- CHECK `acknowledged 계열은 broker_order_id NOT NULL` (`status='accepted'` 이면
  `broker_order_id IS NOT NULL`) — NH r4-Q3 에서 입증된 형태.

### 2.2 정체성·멱등키

브로커 주문 본문(`kt10000`~`kt10003` body: `dmst_stex_tp`/`stk_cd`/`ord_qty`/`ord_uv`/`trde_tp`
`domestic_orders.py:87-95`)에 **클라이언트 멱등 필드가 없다** — NH 와 같은 구조적 상황이므로
송신 전 예약이 유일한 이중송신 방어다.

- `client_order_id`/`idempotency_key` = `kwoomm-<sha16>(canonical|거래일소금|rung)` (KR),
  `kwoomus-<sha16>(canonical|거래일소금|rung)` (US — market/order_type 도 canonical 에 들어간다)
  (Toss `tossp6-` 패턴 `broker-and-ledger-contracts.md:919`). 같은 거래일 동일 주문은 dedupe,
  익일은 신규, 같은 날 진짜 동일 두 번째는 rung 판별자로 분리.
- 송신 직전 `review.order_send_intents` 에 `(account_scope='kiwoom_mock'|'kiwoom_mock_us',
  idempotency_key)` INSERT — UNIQUE 충돌은 브로커 호출 **전**에 fail-closed.
- 송신 후 결과 세 갈래(구현은 `app/services/kiwoom_mock_place.py` 의 공통 꼬리
  `_execute_reserved_send` 와 시장별 classifier):
  1. `KiwoomPreDispatchError` — 미송신 확정 → 원장 0행, 예약 해제(같은 키 재시도 가능).
  2. 정상 응답 — `return_code==0` 이면 `accepted`(KR 은 `ord_no`/`order_no` 단일 안전값,
     US 는 `validate_us_order_id` 통과 1–18자리 단일값이 필요하고 없으면 `uncertain`),
     명시적 비-0 `return_code` 면 `rejected`(예약은 소비된 채 종결 — 브로커가 거절했으므로 재송신 의미 없음).
  3. `_dispatch_unknown_response` — 송신 여부 불명 → `uncertain` 행 + 예약 **유지**.
     대사가 양성 증거로 해소할 때까지 같은 키 재송신 금지(NH S3 와 동일 논리).

### 2.3 `execution_ledger` 로의 도달

T-P4(hk 4734)가 구현한 현재 형태는 **KR full 대사만** `execution_ledger` 에 쓴다.
US(`kiwoom_mock_us`)는 terminal-only 범위이고, `market='us'` full mode 는 쓰기 전에 거부된다
(`app/services/kiwoom_mock_reconcile.py:370-377`).

| 항목 | 현재 구현 | 근거 |
|---|---|---|
| `broker` | `'kiwoom'` — `execution_ledger` CHECK 는 `('kis','upbit','toss','kiwoom')`, `execution_ledger_reconcile_runs` CHECK 는 `('kis','upbit','kiwoom')` | `alembic/versions/20261115_hk4734_kiwoom_broker.py:50-85`, `alembic/versions/20261115_hk4734_kiwoom_validate.py:31-42` |
| `account_mode` | `'mock'` | `app/services/kiwoom_execution_ledger.py:33-35` |
| `venue`/`instrument_type`/`currency` | `'krx'` / `'equity_kr'` / `'KRW'` (KR only) | `app/services/kiwoom_execution_ledger.py:35-37` |
| `broker_order_id` | 원장 `broker_order_id` — `record_fill` 은 broker order id 없는 행을 거부 | `app/services/kiwoom_mock_order_ledger_service.py:683-688` |
| `fill_seq` | `_stable_int32_hash(f"{broker_order_id}:{previous}:{cumulative}")` 결정적 int32 | `app/services/kiwoom_execution_ledger.py:42-44,122-124` |
| `source` | `'reconciler'`(대사 부킹) | `app/services/kiwoom_execution_ledger.py:146` |
| `filled_at`/`filled_at_basis` | `observed_at` / `'observed_at'` — 브로커 체결 시각을 주지 않으므로 관측 시각을 명시 | `app/services/kiwoom_execution_ledger.py:11-13,142-143` |
| `fee_amount`/`fee_basis`/`tax_amount` | `fee_amount=None`, `fee_basis='not_provided'`, `tax_amount=None` — 모의 응답의 미제공 수수료를 0 으로 채우지 않는다 | `app/services/kiwoom_execution_ledger.py:11-13,138-142` |

- 부킹 단위는 **델타**다: 브로커의 누적 체결수량·평균가와 원장의 이전 누적값의 차이를
  `ExecutionLedgerRepository.upsert_fill` 로 쓴다. 이전 수량이 있으면 이전 누적평균가가
  필요하고, 증분가격은 `(cum_qty*cum_avg - prev_qty*prev_avg) / delta_qty` 로 유도한다
  (`app/services/kiwoom_execution_ledger.py:53-74,105-121`). 같은 누적수량에 다른 평균가가
  오면 `average_price_changed_without_fill` 로 거부해 부킹된 평균을 바꾸지 않는다
  (`app/services/kiwoom_mock_order_ledger_service.py:721-736`).
- full run 은 `execution_ledger` 델타와 별도로 `broker='kiwoom'`, `notes='kiwoom_mock_full'`,
  `committed_insert=booked_count`, `unchanged=already_booked_count` 인 reconcile-run 행을 한 번 쓴다
  (`app/services/kiwoom_mock_reconcile.py:836-857`).
- **저널·trade row·실현손익은 아직 쓰지 않는다.** 그 공백은 hk 4802 가 소유한다
  (`app/services/kiwoom_mock_reconcile.py:22-25,673-679`).
- 격리 리더 규약: 원장을 fill 로 읽는 새 리더는 `execution_ledger_in_effect()`
  (`quarantined_at IS NULL`)를 AND 한다(`app/models/execution_ledger.py:142-152`).
  격리 대상 CHECK 는 `source='websocket' AND broker='kis'` 라 Kiwoom 행은 격리 대상이
  아니지만(#1175 경계), 리더 계약은 동일하게 지킨다.

### 2.4 `review.kiwoom_mock_signal_ledger` (귀속, §4.5)

`kis_mock_signal_ledger` 스키마 그대로 복제하되 CHECK 를
`account_mode IN ('kiwoom_mock','kiwoom_mock_us')` 로. `correlation_id` UNIQUE,
`strategy`·`signal_source` NOT NULL nonblank, `decision IN ('order','no_order')`,
`outcome_state` 동일 어휘 (`20260803_kis_mock_signal_ledger.py:33-100`).

## 3. 대사 커널 (reconcile)

### 3.1 입력 — 조회 TR 과 한도

| 역할 | KR | US | 근거 |
|---|---|---|---|
| 상태 목록(`role='status'`) | `kt00009` (`acnt_ord_cntr_prst_array`) | `ust21050` (미체결/오픈 주문) | `constants.py:33-34`, `:172`; `app/services/kiwoom_mock_reconcile.py:570-603` |
| 주문 상세(`role='detail'`) | `kt00007` `qry_tp="1"` (주문순 — 그 거래일의 주문 행 전부; `qry_tp` 어휘는 1주문순/2역순/3미체결/4체결내역만 `constants.py:109-121`) | `ust21510` (당일 주문·체결) | `constants.py:33`, `:174`; `app/services/brokers/kiwoom/mock_reconcile.py:76-81,334-344`; `app/services/kiwoom_mock_reconcile.py:188-201,587-603` |

- US 는 과거 ET 거래일을 덮는 목록이 없다. 과거 US 행은 `us_past_day_evidence()` 로
  항상 불완전 증거(`us_listing_unavailable_for_past_trading_day`)가 되어 `unknown` 이다
  (`app/services/brokers/kiwoom/mock_reconcile.py:342-344,453-455`;
  `app/services/kiwoom_mock_reconcile.py:724-729`).

- **Rate limit**: 모의 공통 TR 은 "국내주식 1초 1회, 미국주식 1초 1회"(research 행 2,
  공식 mockInvestInfo; 실측 0.5s 성공/0.2s 실패 `broker-and-ledger-contracts.md:860` 근방).
  커널은 시장별 직렬 + 최소 1.0s 간격을 기본으로 하고 조정은 설정값으로 둔다.
- 목록 불완전(페이지 절단·오류·빈 응답 모호)은 "부재 증거"로 쓰지 않는다 — 해당 스캔은
  `unknown` 으로 끝내고 다음 실행에 맡긴다(NH S6·키움 contract 의 목록-불완전 규칙과 동형).

### 3.2 종결 분류표 (T-P3/T-P4 구현)

판정은 순수 함수 `classify_verdict`(`app/services/brokers/kiwoom/mock_reconcile.py:552-731`)가
하고, 커널이 verdict 를 원장 서비스로 적용한다. `unknown` 은 전이 없이 open 을 유지하고
`requires_manual_review` 로 보고한다.

| verdict | 증거(전부 양성) | 원장 전이 | 부수 동작 |
|---|---|---|---|
| `filled` | detail 행의 동일 `ord_no`, 누적 체결수량 ≥ 주문수량. KR 은 평균가 파싱 가능해야 한다(`require_fill_price=True`); US 는 평균가가 없어도 verdict 는 나지만 full mode 가 KR only 라 부킹하지 않는다 | full mode 에서만 `filled` | `execution_ledger` 델타 부킹; terminal_only·US 는 `deferred_fill_booking` |
| `partial` | detail 행의 동일 `ord_no`, 0 < 누적 체결수량 < 주문수량이고 아직 미체결 잔량이 남은 상태 | `partial`(open 유지) | full mode 에서 델타만큼 `execution_ledger` 부킹 |
| `cancelled` | 우리 자신의 acknowledged cancel 요청 **+** 원주문에 대한 양성 취소 marker(`취소`/`cancel` 또는 US `ord_cntr_tp='12'`) — 둘 다 필요. 누적 체결이 있으면 그 체결을 싣고 종결 | `cancelled` | 양성 체결이 있으면 full mode 가 델타를 먼저 부킹하고, 부킹 누적이 verdict 수량과 정확히 같을 때만 `close_filled_as_terminal` |
| `expired` | detail/status 행의 단일 `expiry` marker(`만료`/`expire`) — **세션 종료·목록 부재만으로는 만료시키지 않음** | `expired` | 양성 체결이 있으면 위와 같이 델타 부킹 후 종결 |
| `rejected` | detail/status 행의 단일 `reject` marker(`거부`/`reject`) | `rejected` | 양성 체결이 있으면 위와 같이 델타 부킹 후 종결 |
| `pending` | detail 행이 살아 있고 잔량이 남은 브로커 행 | 전이 없음 | `noop_pending` |
| `unknown` | `ord_no` 정확 일치 행 없음 / 목록 불완전·파싱 불가 / symbol·수량 불일치 / marker 모호·충돌 / 기록된 체결과 브로커 체결 모순 | **전이 없음** — `noop_no_evidence` + `requires_manual_review` | 행은 open 유지 |
| `uncertain 해소` | `uncertain` 행: 완전한 목록에서 동일 `ord_no`+symbol+수량 일치, 종결·모호·불명확 change text 없음, detail 증거와 읽을 수 있는 체결수량이 있고, 체결>0 또는 잔량>0 이면 `accepted` 로만 승격. 종결 marker 가 보이면 `unknown` 유지 | `uncertain→accepted` 만 허용 | 재송신 없음; 승격 후 다음 run 이 filled/partial 을 분류 |

- marker 는 닫힌 어휘(`cancel`/`reject`/`expiry`)만 양성 증거다. 두 listing 의 marker class 가
  다르면(`conflicting_terminal_markers`) 어느 쪽도 선택하지 않고 `unknown` 이다
  (`app/services/brokers/kiwoom/mock_reconcile.py:462-475,651-681,704-731`).
- `filled`/`partial` 이 아닌 종결 verdict 에 양성 누적 체결이 실려 오면 full mode 는 먼저
  `_book_fill` 로 델타를 부킹하고, `close_filled_as_terminal` 은 행의 부킹 누적이 verdict 의
  `filled_quantity` 와 정확히 같을 때만 종결한다. 아니면 `fill_not_booked` 로 거부해 행을
  open 에 남긴다(`app/services/kiwoom_mock_reconcile.py:762-789`;
  `app/services/kiwoom_mock_order_ledger_service.py:554-610`).
- "늦은 종결은 무해, 이른 종결은 유해" 원칙을 그대로 둔다: 전이 거부는
  `KiwoomMockTransitionRefused` 로 표면화하고 보고된 close 가 실제로 일어나지 않았음을 숨기지 않는다
  (`app/services/kiwoom_mock_reconcile.py:615-668`).

### 3.3 실행 모드 — #2400 형태

| 모드 | CLI | 게이트(기본 false) | 동작 |
|---|---|---|---|
| dry-run | `scripts/kiwoom_mock_auto_reconcile.py`(기본) | 없음 | verdict 예고만, 쓰기 0, `would_*` 액션 |
| terminal-only | `--apply-terminal-only --market kr\|us` | KR=`KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED`, US=`KIWOOM_MOCK_US_TERMINAL_AUTO_RECONCILE_ENABLED` — 각각 정확히 `true` | 만료·취소·거절·uncertain→accepted 만 반영, 체결 부킹은 `deferred_fill_booking` |
| full | `--apply` (**KR only**) | `KIWOOM_MOCK_AUTO_RECONCILE_ENABLED` **AND** `KIWOOM_MOCK_AUTO_RECONCILE_SAFETY_REVIEW_PASSED` — 각각 정확히 `true` | `execution_ledger` 델타 부킹 + reconcile-run 행; 저널·실현손익은 hk 4802 공백 |

- 커널은 `kiwoom_mock_reconcile_orders_impl` 이고 CLI 는 `--apply`/`--apply-terminal-only` 를
  mutually exclusive 로 받고, 미해결·deferred·잘린 scan·dry-run pending work 는 exit 3 으로
  보고한다(`scripts/kiwoom_mock_auto_reconcile.py:54-85,134-187`;
  `app/services/kiwoom_mock_reconcile.py:334-434`).
- 게이트는 write 조건 자체를 쓰기 전·원장/브로커 읽기 전에 검사한다. terminal-only 는 시장별
  게이트 하나, full mode 는 두 스위치가 모두 필요하고 US full mode 는 거부된다
  (`app/services/kiwoom_mock_reconcile.py:361-400`).
- **스케줄러 등록 없음**(하드룰 6) — CLI/수동 lever 만 출고.
- 모의라도 full 부킹은 두 플래그를 유지한다 — 실전 전환 시 게이트 이름만 바꾸고
  구조는 동일해야 하므로(§5).

### 3.4 부킹 범위 (full 모드)

- `execution_ledger` upsert: §2.3 형태, `source='reconciler'`. 원장 행은 `partial`/`filled`
  로 전이하고 `filled_qty`·`avg_fill_price`·`reconcile_attempts`·`last_reconcile_detail`·
  `reconciled_at` 을 갱신한다(`app/services/kiwoom_mock_order_ledger_service.py:648-806`).
- full run 은 `execution_ledger_reconcile_runs` 에 `broker='kiwoom'`, `notes='kiwoom_mock_full'`,
  `committed_insert=booked_count`, `unchanged=already_booked_count` 인 행을 한 번 쓴다
  (`app/services/kiwoom_mock_reconcile.py:836-857`).
- **저널·trade row·실현손익은 아직 쓰지 않는다.** hk 4734 는 `execution_ledger` 델타만
  부킹하고, 저널 프로젝션은 hk 4802 가 소유한다
  (`app/services/kiwoom_mock_reconcile.py:22-25,670-679`).
- 기존 trade/journal 테이블의 account 어휘를 Kiwoom mock 에 맞출지는 hk 4802 의
  선행 조건이지, 현재 reconcile 이 쓰는 범위가 아니다.

## 4. 제안 집행 통합 (order_proposals)

### 4.1 계좌 모드

| 항목 | 결정 | 근거 |
|---|---|---|
| 신규 account_mode | `'kiwoom_mock'`(KR, market `equity_kr`) 1개부터. `'kiwoom_mock_us'`(US)는 별도 후속 티켓 — 현 도구는 KRX 가 KR 경로뿐이고 US 는 별도 앱키 표면 | `orders_kiwoom_variants.py:53`, decision 수정 2 |
| CHECK 확장 | `order_proposals.account_mode` CHECK 에 추가 — 현재 `'kis_live','kis_mock','toss_live','upbit','db_simulated'` | `app/models/order_proposals.py:40` |
| 제출 가능 집합 | `_SUBMITTABLE_ACCOUNT_MODE_MARKETS` 에 `('kiwoom_mock','equity_kr')` 추가 | `service.py:354-362` |

### 4.2 승인 창 / 세션 규칙

- `resolve_submission_session` 에 `('kiwoom_mock','equity_kr')` 분기 추가 — KRX-only 라
  **정규장(09:00–15:30 KST)만 허용**. NXT/pre-market 세션은 모의가 지원하지 않으므로
  허용 세션에서 제외한다(KIS 의 `nxt_after` 연장 논리를 적용하지 않음 —
  `approval_window.py:1000-1026` 의 fail-closed 기본값을 명시 분기로 대체).
- 승인 창(valid_until)·묶음 승인 정책은 계좌 모드와 무관한 기존 규칙을 그대로 쓴다.

### 4.3 자동승인 허용 목록 — **기본 미등록**

- `_VETO_CAPABLE_ACCOUNT_MARKETS`(auto_approve.py:192-205)와 자동승인 어휘
  (`auto_approve.py:784`, `auto_approve_audit.py:117`)에 `kiwoom_mock` 을 넣지 않는다.
- 이유: 자동승인은 "승인 없이 제출 + 거부(veto) 버튼으로 즉시 취소 가능"이 전제인데
  veto 는 실제 취소 경로(`cancel_auto_submitted_rungs` → `cancel_target_order`)가
  필요하다. 운영자가 위임장 결속 자동승인을 켜고 싶으면 그때 별도 결정 + 취소 경로
  검증 후 등록한다 — brief 의 기본 입장과 동일.
- `order_proposals` 경유 주문은 **전부 수동 승인 카드**로 시작한다.

### 4.4 보유·가용자금 합류

- 매수 가용: `default_buying_power_reader`/`claimer` 에 `kiwoom_mock` 분기 —
  `kt00001`(예수금상세) 로 가용현금을 읽는다(`kt00010` 은 모의 미지원 ROB-904
  `domestic_account.py:51-60`, 코드상 이미 kt00001 폴백이 확립 경로).
- 매도 가능 수량: `kt00018` 잔고 조회 — order_preflight 의 `_check_sellable`
  (`order_preflight.py:206`)이 이미 이 경로를 쓴다.
- reader 실패는 fail-open("unknown") 유지 — Toss 와 같은 계약(`buying_power.py:182-198`).
  다만 제안 create-time advisory 게이트(`order_proposal_tools.py:385`) 의
  `(account_mode, market)` 허용 집합에 `kiwoom_mock` 추가는 별도 판단(Q5).

### 4.5 귀속(attribution) 게이트 — kis_mock 룰 7 등가

- **권고: 동일하게 둔다.** `place_order(is_mock=True)`/`account_mode='kis_mock'` 는
  브로커 전송 전 `review.kis_mock_signal_ledger` 커밋을 요구하고 귀속 불가는
  `attribution_required`/`signal_record_unavailable` 에러(`order_execution.py:996-1070`,
  하드룰 7). 모의 주문의 목적이 전략 검증인 이상 귀속 없는 주문은 데이터로서도 가치가
  없다 — Kiwoom 모의에도 같은 게이트를 둔다.
- 구현: `kiwoom_mock_*` place 경로의 송신 전에 §2.4 신호 원장 커밋을 요구.
  `resolve_attribution` 과 동일한 실패 계약(같은 error_code 어휘) 재사용.
- 하드룰 7 본문의 대상이 `kis_mock` 한정이므로, 동형 규칙의 `kiwoom_mock` 확장은
  계약 문서 갱신(§6 T-PX)과 함께 운영자 결정 항목으로 올린다(Q1).

### 4.6 제안 → 브로커 바인딩

- 기존 발행 바인딩·nonce·approval-hash 기구는 그대로 상속된다 —
  `DispatchBinding`/`CallbackEnvelope`(`dispatch_contract.py:60-115`), nonce 소비
  (`service.py:2105`), W5 재시도 대수(코어 진입 후 실패는 재시도 없음).
- `revalidate_and_submit` 의 `_default_place_order_fn` 에 kiwoom 어댑터 추가 —
  Toss 전용 어댑터와 같은 위치(`revalidation.py` 의 dispatch 분기). 어댑터는
  MCP impl 이 아니라 **서비스 레이어 함수**를 호출해 원장 기록을 같은 경계에서
  보장한다(§6 T-P2).

## 5. 실전 전환 준비 — 무엇이 달라지고 달라지면 안 되는가

| 계층 | 모의 → 실전에서 **바뀌는 것** | **바뀌면 안 되는 것** |
|---|---|---|
| 호스트 | `mockapi.kiwoom.com` → `api.kiwoom.com` (`constants.py:10` 대 `live_market_data` 의 api host) | host allowlist+전송 직전 재검증 구조 |
| 자격증명 | `KIWOOM_MOCK_*` 4종 → 실전 앱키/계좌 env(새 이름, 모의와 독립 — 공식상 mock/live 앱키 별도 관리, research 행 4) | env 이름만 보고하고 값 출력 금지 규약 |
| 게이트 | `KIWOOM_MOCK_ENABLED` → `KIWOOM_LIVE_*` 계열 + 실전 주문 게이트(신설, default off) | default-disabled + confirm 이중 게이트 구조 |
| account_mode | `kiwoom_mock`/`kiwoom_mock_us` → `kiwoom_live`/`kiwoom_live_us`(가칭) | 제안·원장·대사 코드 경로 — 모드 값만 다른 분기 |
| 원장 테이블 | 새 `review.kiwoom_live_order_ledger`(또는 동일 테이블에 모드 확장 — 구현 시 결정, 권고는 §7-Q2) | 스키마 형태·UNIQUE/CHECK·서비스 레이어 |
| TR 계약 | 동일 TR(kt10000 계열/kt00007/kt00009) — 실전 NXT/SOR 허용 여부는 그때 별도 검증 | 종결 분류 대수·증거 규칙 |
| execution_ledger | `account_mode='live'` 값만 변경 — `broker='kiwoom'` 은 그대로 | upsert 형태·ROB-1379 정체성 |
| 세션 규칙 | 실전은 NXT 허용 가능성(미검증) — 승인 창 분기만 조정 | evidence-first·fail-closed 판정 |
| 대사 게이트 | 모의용 플래그 → 실전용 플래그로 이름 변경 | terminal_only/full 두 모드 + 두 플래그 구조 |

- 🔴 **실전 주문 경로는 현재 존재하지 않는다.** 레포의 유일한 live 호스트 클라이언트는
  차트 읽기 전용(`KiwoomLiveReadOnlyClient`, 주문 TR·계좌번호 3중 부재 방어
  `broker-and-ledger-contracts.md:870-889`). 이 설계는 실전 주문을 열지 않고,
  "열 때 바꿀 것"을 위 표로 고정할 뿐이다. 실전 활성화는 별도 운영자 승인(수정 2 자문 메모).
- 🔴 실전 전환 시 **게이트 완화·하드 인바리언트 변경은 금지**(하드룰 4) — 위 표의
  "바뀌는 것" 외의 변경은 새 결정이다.

## 6. 인바리언트 표와 티켓 분할

### 6.1 인바리언트 (wake §T3)

| # | 인바리언트 | 강제 장치 | 위반 시 |
|---|---|---|---|
| I1 | 브로커 ACK 없는 주문은 원장에 `accepted` 로 존재하지 않는다 | record_send 가 ACK 후에만 쓰기; `accepted ⇒ broker_order_id NOT NULL` CHECK | — |
| I2 | 같은 `(account_scope, idempotency_key)` 송신은 최대 1회 | `order_send_intents` UNIQUE | IntegrityError → 송신 전 차단 |
| I3 | 송신 후 불명 결과는 재송신하지 않는다 | `uncertain` 행 + 예약 유지, 해소는 양성 증거만 | — |
| I4 | `filled` 마킹은 브로커 증거가 있는 델타만큼만 | 대사 분류표 §3.2, delta-멱등 | — |
| I5 | 부재는 종결 증거가 아니다 | verdict `unknown` → 전이 없음 | — |
| I6 | KR 모의 주문은 KRX 외로 나가지 않는다 | `_ensure_krx` + `MOCK_REJECTED_EXCHANGES` + tooling 사전거부 (기존, 변경 없음) | — |
| I7 | 모든 mutation 도구는 default-disabled 게이트 + confirm | 기존 게이트 유지, 신규 게이트도 기본 false | — |
| I8 | 원장 쓰기는 서비스 레이어 경유만 | 하드룰 5, 신규 서비스 모듈만 writer | — |
| I9 | execution_ledger 의 Kiwoom 행은 `broker='kiwoom' AND account_mode='mock'` | CHECK 확장 + upsert 빌더 고정값 | — |
| I10 | 제안 경유 kiwoom 주문도 신호 귀속 없이 송신되지 않는다 | §4.5 게이트(운영자 결정 조건부) | `attribution_required` |
| I11 | 스케줄러·자동 트리거 없음 | 하드룰 6 — CLI 만 | — |
| I12 | 자동승인 허용 목록에 `kiwoom_mock` 이 없다 | 허용 목록 미변경 + 테스트 | — |

### 6.2 티켓 분할

각 티켓은 독립 머지 가능 단위다. Tier 는 PR 위험도(T1 문서/계약성, T2 추가형 서비스,
T3 송신·차단 경로 접촉). **차단 경로**(주문 송신·재송신·체결 부킹에 직접 걸리는 것)는
★로 표시한다.

| 티켓 | Tier | 의존 | 한 줄 acceptance |
|---|---|---|---|
| T-P1: 마이그레이션+모델 — `review.kiwoom_mock_order_ledger`, `review.kiwoom_mock_signal_ledger`, `order_proposals.account_mode` CHECK 확장 | T2 | 없음 | upgrade/downgrade 왕복 성공, CHECK·UNIQUE 가 설계대로 거부 |
| T-P2: 원장 서비스 + 송신 배선 — record_send/uncertain 기록을 `kiwoom_mock_*` place 경로에 연결 ★ | T3 | T-P1 | mock 클라이언트 스텁으로 place→행 1건, 같은 client_order_id 재송신 차단, dispatch-unknown → uncertain |
| T-P3: 대사 커널 + CLI — 분류 순수함수, `scripts/kiwoom_mock_auto_reconcile.py`, 두 모드 | T2 | T-P2 | 고정 목록 입력에 verdict 표 §3.2 전부 재현, dry-run 쓰기 0, terminal-only 는 부킹 보류 |
| T-P4: execution_ledger 연결 — broker CHECK 확장, upsert 빌더, full 모드 부킹 ★ | T3 | T-P3 | filled verdict → in-effect 행 1건, 재대사 시 delta 중복 없음 |
| T-P5: 제안 통합 — `_SUBMITTABLE`·세션 분기·`_default_place_order_fn` 어댑터 ★ | T3 | T-P2, T-P3 | `order_proposal` place→승인→제출→원장·대사까지 통과, 자동승인 목록 불변 |
| T-P6: 귀속 게이트 — 신호 원장 pre-submit 강제(Q1 결정 후) ★ | T3 | T-P1 | strategy 없는 place → `attribution_required` 로 송신 0 |
| T-P7: 보유·가용 reader — kt00001/kt00018 buying-power 분기 | T2 | T-P2 | reader 가 kiwoom 응답을 반환, 실패 시 unknown fail-open |
| T-P8: US 모의 확장 — `kiwoom_mock_us` 제안·대사(주문번호 9자리, ET 거래일) | T2 | T-P5 | US place→대사 왕복, KR 경로 회귀 없음 |
| T-PX: 계약 문서 갱신 — broker/approval 계약에 kiwoom 절 추가 | T1 | T-P2~T-P5 머지 후 | 해당 절이 코드와 일치, AGENTS 하드룰 변경 없음 |

## 7. 운영자 질문 (번호 순, 권고안 첨부)

1. **귀속 게이트를 Kiwoom 모의에도 둘까?** — 권고: **예.** kis_mock 룰 7 과 같은
   이유(모의 주문의 목적이 전략 검증)이고, 어휘(`kiwoom_mock`)는 이미 DB CHECK 에
   있다. `review.kiwoom_mock_signal_ledger` 신설 + 같은 error_code 재사용.
2. **실전 원장은 별도 테이블인가, 같은 테이블의 account_mode 확장인가?** — 권고:
   **같은 테이블에 CHECK 확장**(`IN ('kiwoom_mock','kiwoom_mock_us','kiwoom_live','kiwoom_live_us')`).
   Toss 단일 테이블 선례가 조회·대사 코드 공유에 유리하고, 모드 CHECK 만 실전 전환
   마이그레이션 1건으로 해결된다. 물리 분리가 운영상 나으면 별도 테이블도 가능 —
   둘 다 서비스 레이어 뒤라 호환 비용은 같다.
3. **모의 체결을 저널·실현손익까지 부킹할까?** — 권고: **부킹하되 `account='kiwoom_mock'`
   라벨로**. 검증 목적의 mock 이라도 회고·성과표가 execution_ledger/journal 을 읽는
   경로와 같아야 지표가 비교 가능하다. 다만 기존 trade/journal 테이블의 `account`
   어휘 CHECK 확인이 T-P4 선행 조건.
4. **모의 DAY 주문의 만료 의미론을 추론으로 닫을까?** — 권고: **아니오(초기).**
   브로커 양성 증거만으로 종결하고, 목록-없음은 `unknown` 유지. 추론 종결(#1112
   형태)는 모의 서버의 실제 만료 동작을 관측한 뒤 별도 결정.
5. **create-time buying-power advisory 허용 집합에 `kiwoom_mock` 을 넣을까?** —
   권고: **예(T-P7).** reader 자체는 fail-open 이라 안전 비용이 작고, 제안 생성 시
   가용 부족 경고가 조기에 잡힌다.
6. **US 모의는 이번 범위에 포함할까?** — 권고: **KR 먼저(T-P1~T-P7), US 는 T-P8 후속.**
   별도 앱키·9자리 주문번호·ET 거래일이라 동형이지만 검증 단위를 줄인다.
   테이블·커널 설계는 이미 US 를 수용한다.
7. **`kt00010` 모의 미지원을 공식 문서로 재확인할까?** — 권고: **구현 전 스모크에서
   한 번 재확인**(research 행 3 could not verify). 지원이 확인되면 가용현금 reader
   를 kt00010 으로 바꾸는 것은 설정 수준 변경이다.

---

### 부록 A — 참조한 사실의 핀 모음 (주요)

- MCP 도구·모드: `app/mcp_server/tooling/orders_kiwoom_variants.py:53,63-71`
- 송신 지점·3갈래 결과: `orders_kiwoom_variants.py:423,503-524,383-416`
- TR 상수: `app/services/brokers/kiwoom/constants.py:27-36,115-122,167-178`
- KRX-only: `constants.py:54`, `domestic_orders.py:39,88`, `orders_kiwoom_variants.py:121-127`
- 게이트: `app/core/config.py:1465-1471,1499-1512`; `client.py:135-140`; `constants.py:10`
- preflight: `app/services/brokers/kiwoom/order_preflight.py:85-145,206,283`
- order_send_intents: `app/models/review.py:673-692`
- B0x 수명주기: `app/models/kiwoom_coordination_lifecycle.py:3-115`,
  `app/services/brokers/kiwoom/coordination_store.py:54-93`
- Toss 레저: `app/models/review.py:694-730`, `app/services/toss_live_order_ledger_service.py:100-160`
- Toss execution 브리지: `app/services/toss_execution_ledger.py:17-19,45-107`
- KIS 커널: `app/mcp_server/tooling/kis_live_ledger.py:66-79,851-1050`,
  `app/services/brokers/kis/mock_scalping_exec/fill_evidence.py:22-30,118-190`,
  `app/services/brokers/kis/live_order_expiry.py:272-330`
- #2400 CLI: `scripts/kis_live_auto_reconcile.py:31-105`,
  `scripts/toss_live_auto_reconcile.py:11-70`, `scripts/kis_us_auto_reconcile.py:11-70`
- execution_ledger: `alembic/versions/2026_05_13_rob211_add_execution_ledger.py:35-100`,
  `app/models/execution_ledger.py:58,75,142-152,188`,
  `docs/agent-contracts/broker-and-ledger-contracts.md:292-431`
- 제안: `app/models/order_proposals.py:40`, `app/services/order_proposals/service.py:354-362`,
  `approval_window.py:1000-1026`, `auto_approve.py:192-205`, `buying_power.py:182-231`,
  `order_execution.py:996-1070`, `revalidation.py`, `dispatch_contract.py:60-115`
- kis_mock 신호 원장: `alembic/versions/20260803_kis_mock_signal_ledger.py:33-100`
- 키움 계약 절: `docs/agent-contracts/broker-and-ledger-contracts.md:831-889`
- 결정·리서치: hk `decision/2026-11-10/at-new-strategy-go` 수정 2,
  `research/2026-11-10/broker-api-recheck` 행 1-4
