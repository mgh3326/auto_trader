# ROB-554 — /invest 결정로그에 주문 provenance(근거) + 체결현황 노출

**Status:** Design approved (2026-06-14) · **Migrations:** 0 · **Base:** `915db6ef` (origin/main)

## 1. 운영자 목표

Claude(운영자 대행)가 auto_trader MCP로 코인 분석 → 매수/매도 → 결정기록을 수행한다(Upbit 실계좌). `trader.robinco.dev/invest` **결정로그(리포트) 화면에서 각 주문이 "어떤 근거(rationale)로 났는지 + 체결현황(fill status)"을 함께 보고 싶다.** 현재 불가능: DB에는 다 있으나 read-back이 어디에도 연결되지 않음.

## 2. 검증된 현황 (코드 대조 완료, base `915db6ef`)

### ✅ DB는 이미 완성 — 신규 적재/마이그레이션 불요

- `LiveOrderLedger`(US/crypto, `app/models/review.py:348`, table `review.live_order_ledger`):
  - 근거: `reason`(:402), `thesis`(:403), `strategy`(:404), `target_price`(:405), `stop_loss`(:406), `min_hold_days`(:407), `notes`(:408), `exit_reason`(:409), `indicators_snapshot`(:410)
  - 체결: `status`(:395), `filled_qty`(:424), `avg_fill_price`(:425), `trade_id`(:426), `journal_id`(:427), `reconciled_at`(:428)
  - 링크: `report_item_uuid`(:414, ROB-473), indexed(:364)
  - 브로커 주문 id: `order_no`(:390, `# KIS odno / Upbit uuid`) — **`broker_order_id` 컬럼 없음**
- `KISLiveOrderLedger`(KR, `review.py:267`): 형제 모델, `report_item_uuid`(:328). **컬럼명 차이 주의**: `broker`(default `"kis"`) + `account_mode`(default `"kis_live"`)를 가짐 — `LiveOrderLedger`의 `account_scope`와 **이름이 다름**. `market` 컬럼은 **없음**. `order_time`은 양쪽 다 `Text`(broker 타임스탬프 문자열).
- 송신 시점 적재: crypto는 `_record_live_order(broker="upbit", order_no=uuid)`(`order_execution.py:805-859`), 근거 전 필드 전달(:841-849), `report_item_uuid`(:858)까지 row(:`live_order_ledger.py:109`)에 durable.
- 체결 write-back: `live_reconcile_orders`가 같은 row를 `_update_live_ledger_outcome`(`live_order_ledger.py:165-190`)로 갱신(status/filled_qty/avg_fill_price/trade_id/journal_id/reconciled_at).

### 🔴 갭 (전부 사실)

1. **링크가 NULL로 남음**: `place_order`가 `report_item_uuid` param을 받지만(`orders_registration.py:230,308`) **generic 설명(:179-206)에 한 줄도 없음** → 운영자(LLM)가 못 채움. (KIS variant 도구는 이미 문서화됨 — `orders_kis_variants.py:469`.)
2. **read-back 미연결**: `list_live_orders_by_report_item_uuid`(`live_order_ledger.py:477`) + KIS 형제(`kis_live_ledger.py:769`) — 둘 다 호출자 0건(유닛테스트만). 게다가 **fill 필드를 projection 안 함**(order_no/symbol/side/status만).
3. **웹이 근거를 안 보여줌**: 번들 화면은 `item.rationale`만 렌더(`InvestmentReportBundleContent.tsx:272-274`), 주문/체결 링크 없음. (`/invest/fills`는 근거 없는 `execution_ledger`만 읽지만, 본 이슈는 결정로그 화면이 surface이므로 fills 페이지는 범위 밖.)

### 핵심 아키텍처 사실 (설계 근거)

- 웹 번들 endpoint(`investment_reports.py:204-222`, `get_investment_report`)와 MCP `investment_report_get`(`investment_reports_handlers.py:690`)이 **둘 다** 공유 서비스 `InvestmentReportQueryService.get_bundle()`(`query_service.py:140`)와 공유 스키마 `InvestmentReportBundle`/`InvestmentReportItemResponse`를 사용. 각자 `_serialise_bundle`(router:56 / handler:224)을 가짐.
  - → **공유 서비스+스키마 계층에 `linked_orders[]`를 추가하면 웹 화면 + MCP 도구가 동시에 얻는다.** 별도 MCP 도구 불요(갭 "MCP ②"가 자동 충족).
- `InvestmentReportItemResponse`(`schemas:735`)는 `extra="forbid"` 아님 + frozen 아님(`:774`) → optional 필드 추가 + post-`model_validate` set 가능.

## 3. 결정 (사용자 확정)

| 항목 | 결정 |
|---|---|
| 주력 화면 | **결정로그(리포트) 번들 화면** (fills 페이지 아님) |
| 마켓 범위 | **전 라이브 마켓** — US/crypto(`LiveOrderLedger`) + KR(`KISLiveOrderLedger`) + Toss KR/US(`TossLiveOrderLedger`) 통합. **정정(2026-06-14)**: 초안은 "toss는 report_item_uuid 없어 범위 밖"이라 했으나 ROB-545가 Toss 레저에 `report_item_uuid`를 추가했으므로 "전 라이브 마켓"에 포함됨(리뷰 발견, 사용자 승인 후 구현). mock/paper/alpaca/binance-demo는 링크 컬럼 없음 → 범위 밖 |
| 체결 상세도 | **장부행 롤업** (ledger row의 status/filled_qty/avg_fill_price. `execution_ledger` join 불요, fan-out 없음) |
| 과거 링크 | **forward-only** (S1 이후 신규 주문만. 과거 NULL 백필 안 함) |
| PR 패키징 | **단일 PR, 커밋 3개**(S1/S2/S3) |
| 주문-레벨 근거 | `exit_reason`/`thesis`를 `LinkedOrderView`에 포함, 카드 보조줄에 노출(아이템 rationale은 위에 이미 표시) |

## 4. 접근법

**채택 — 공유 서비스 역조회 → 번들 item `linked_orders[]` 임베드.**

기각:
- **fills 페이지 uuid-JOIN**: surface 결정과 불일치 + `report_item_uuid` 링크 미사용 + fan-out.
- **execution_ledger 컬럼 추가 + reconcile 전파**: migration>0 + `live_order_ledger`에 이미 있는 데이터 중복.

## 5. 데이터 흐름

```
report item(item_uuid)
 └─[report_item_uuid 역조회: 테이블당 1쿼리(배치) WHERE report_item_uuid IN (item_uuids)]
   → LiveOrderLedger(US/crypto) ∪ KISLiveOrderLedger(KR)
   → report_item_uuid 별 그룹 → LinkedOrderView[] 정규화(롤업 fill 포함)
 get_bundle() 반환 dict에 linked_orders_by_item_uuid 추가
 → 웹 _serialise_bundle + MCP _serialise_bundle 양쪽이 item.linked_orders set
 → InvestmentReportItemResponse.linked_orders (신규 optional)
 → 프런트 ItemRow 주문 카드 렌더
```

N+1 없음: 번들의 모든 item_uuid에 대해 ledger 테이블당 1쿼리.

## 6. `LinkedOrderView` (신규 read-side 스키마)

`app/schemas/investment_reports.py`에 추가. 기존 dead 헬퍼 projection을 확장(특히 fill 필드).

| 필드 | 타입 | US/crypto (`LiveOrderLedger`) | KR (`KISLiveOrderLedger`) |
|---|---|---|---|
| `broker` | `str \| None` | `row.broker` | `row.broker`(="kis") |
| `account_scope` | `str \| None` | `row.account_scope` | **`row.account_mode`**(이름 다름) |
| `market` | `str \| None` | `row.market` | 상수 `"kr"`(컬럼 없음) |
| `order_no` | `str \| None` | `row.order_no`(Upbit uuid) | `row.order_no`(KIS odno) |
| `ledger_id` | `int` | `row.id` | `row.id` |
| `symbol` | `str \| None` | `row.symbol`(정규화 BTC) | `row.symbol` |
| `side` | `str \| None` | `row.side` | `row.side` |
| `status` | `str \| None` | `row.status` | `row.status` |
| `filled_qty` | `Decimal \| None` | `row.filled_qty` | `row.filled_qty` |
| `avg_fill_price` | `Decimal \| None` | `row.avg_fill_price` | `row.avg_fill_price` |
| `order_time` | `str \| None` | `row.order_time`(Text) | `row.order_time`(Text) |
| `reconciled_at` | `datetime \| None` | `row.reconciled_at` | `row.reconciled_at` |
| `exit_reason` | `str \| None` | `row.exit_reason` | `row.exit_reason` |
| `thesis` | `str \| None` | `row.thesis` | `row.thesis` |
| `report_item_uuid` | `UUID \| None` | `row.report_item_uuid` | `row.report_item_uuid` |

`model_config = ConfigDict(extra="forbid")` (read-side 명시 계약). 두 모델 공통 필드는 동일 이름, 차이나는 둘(`account_scope`↔`account_mode`, `market` 부재)만 projection 함수에서 정규화.

## 7. 슬라이스 (전부 migration 0)

### S1 — `place_order` 설명 보강 (enabler)
- `orders_registration.py:179-206` 설명에 `report_item_uuid` 사용 안내 1~2줄 추가. `orders_kis_variants.py:469` 문구 미러링("report item에서 비롯된 주문이면 investment_report_get/create의 item_uuid를 report_item_uuid로 넘겨 감사 링크(ROB-473)").
- 코드 동작 변경 없음(이미 배선됨). 순수 문서.

### S2 — 백엔드 역조회 배선
- 신규 모듈 `app/services/investment_reports/linked_orders.py`:
  - `LinkedOrderView` projection 함수(US/crypto row → view, KR row → view; KR은 `account_mode`→`account_scope` 매핑 + `market="kr"` 상수, 나머지 동일 이름).
  - `async def list_linked_orders_for_item_uuids(db, item_uuids: list[UUID]) -> dict[str, list[LinkedOrderView]]` — 두 ledger 배치 쿼리 + report_item_uuid 별 그룹.
- `get_bundle()`(`query_service.py:140`): 위 함수 호출, 반환 dict에 `"linked_orders_by_item_uuid"` 추가.
- 스키마: `InvestmentReportItemResponse`에 `linked_orders: list[LinkedOrderView] | None = None` 추가.
- 직렬화: 웹 `_serialise_bundle`(router:56) + MCP `_serialise_bundle`(handler:224) 양쪽에서 `resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))` set.
- **dead 헬퍼 정직 처리**: 두 MCP 헬퍼(`list_*_by_report_item_uuid`)를 공유 projection에 위임하도록 리팩터 — 필드 drift 방지 + read-back이 실제 소비됨(get_bundle 경유). projection 단일 출처.
- 레이어링: 신규 모듈은 `app/services/` 내(모델만 import). MCP tooling이 services를 import(방향 OK). `app/services` → `app/mcp_server` 역방향 import 없음.

### S3 — 프런트 렌더
- 타입: `frontend/invest/src/types/investmentReports.ts:226` `InvestmentReportItem`에 `linkedOrders?: LinkedOrder[] | null` 추가 + `LinkedOrder` 인터페이스.
- 렌더: `InvestmentReportBundleContent.tsx` ItemRow의 rationale(:272-274)과 결정이력(:298) 사이에 주문 카드 sub-block.
- 카드: `[측 심볼] · [filled_qty @ avg_fill_price] · [order_time] · order [order_no 7자]` + 체결 뱃지.
  - 뱃지: `filled`→체결 / `accepted`·`submitted`·`partial`→미체결/부분 / `cancelled`→취소.
  - 보조줄(선택): `exit_reason`/`thesis`가 있으면 작게.
- `watch-item-${itemUuid}` 앵커 재사용(ROB-500 딥링크 패턴).
- `ActionPacketView`(intraday)는 범위 밖(별도 surface).

## 8. 테스트 (TDD)

- **S2 단위** (`tests/`): `list_linked_orders_for_item_uuids` — (a) US/crypto+KR 혼합 그룹핑, (b) 미링크 item → 빈/없음, (c) fill 롤업 값 정확(filled/미체결/부분/취소 status별), (d) KR 상수(market="kr", account_scope="kis_live") 채움.
- **S2 통합**: `get_bundle`이 `linked_orders_by_item_uuid` 포함; 웹·MCP `_serialise_bundle` 동치(같은 item.linked_orders).
- **S2 헬퍼 회귀**: 기존 `test_rob473_report_item_link_ledger.py`가 위임 후에도 통과(projection 확장으로 fill 필드 추가됨 — assert 갱신).
- **S3 프런트** (vitest): ItemRow가 linkedOrders 유/무, status별 뱃지 렌더.
- **S1**: 설명 문자열에 `report_item_uuid` 포함 assert(있으면). 없으면 생략.

## 9. 안전 경계 / 비범위

- 브로커/주문/감시 mutation 도달 없음 — 전부 read 경로.
- migration 0 — 모든 컬럼 존재.
- forward-only — 과거 NULL 링크 백필 없음(설계상 read-back 비어있음 = 허용).
- fills 페이지(SellHistoryPanel), intraday ActionPacketView, mock/paper/alpaca/binance-demo ledger는 범위 밖. (Toss는 정정 후 **포함**.)

## 10. 열린 항목 (구현 중 결정 가능, 블로커 아님)

- dead MCP 헬퍼: 위임 리팩터 vs 삭제(+테스트 삭제). 기본=위임(보수적, 향후 MCP 노출 여지).
- `linked_orders` 정렬: `id desc`(최신 먼저, 기존 헬퍼와 동일) 유지.
- 카드에서 `exit_reason`/`thesis` 항상 노출 vs 토글: 기본=있으면 작게 항상 노출.
