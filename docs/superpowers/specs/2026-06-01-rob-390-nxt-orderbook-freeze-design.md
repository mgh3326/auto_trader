# ROB-390 — NXT live orderbook evidence를 report bundle에 freeze 설계

- **이슈:** ROB-390 (오케스트레이션 ROB-394의 3번)
- **날짜:** 2026-06-01
- **상태:** 설계 승인됨 → 구현 계획 대상
- **base:** `origin/main` `861d149e` (ROB-389 머지 직후)

## 목표

NXT 세션 리포트가 KRX 정규장 미개장 지수 `+0.00%`만 보고 방향성을 오독하지 않도록, `market_session="nxt"`
report bundle에 **NXT live orderbook evidence**(top-of-book / spread / depth / venue / session / as_of)를
freeze하고, KRX 정규장 미개장으로 index가 frozen이면 이를 명시한다. read-only, order mutation 무접촉.

## 설계 결정 (이슈 요구: 선택지 먼저 기록)

**Option B 채택** — 새 snapshot kind를 만들지 않고, `market_session="nxt"`일 때 기존 `symbol` quote
enrichment의 venue를 `nxt`로 전환한다.

근거: `symbol` collector는 **이미** quote payload로 `venue`/`session`/`best_bid`/`best_ask`/`spread`/
`spread_bps`/`bid_depth`/`ask_depth`/`as_of`를 내보낸다 — ROB-390 acceptance가 요구하는 evidence 형태와
동일. 따라서 venue만 전환하면 NXT top-of-book이 그대로 freeze된다.

**Option A 기각** — 별도 `nxt_orderbook` snapshot kind + 전용 collector + registry + freshness + schema는
symbol enrichment와 중복되고 스키마 표면이 과다하다.

## 코드 현황 (근거)

* `app/services/action_report/snapshot_backed/collectors/symbol.py` — `_quote_enrichment_plan`이
  KR을 `(kis_client, requires_user_id=True, default_venue="krx", "kis_live")`로 고정 반환. 갭은
  **venue가 항상 `krx`**. `_maybe_enrich_quote`가 `client.fetch_quote_orderbook(symbol)` 결과를 quote
  payload(venue/session/spread/depth/as_of 포함)로 직렬화 — evidence 형태는 이미 충분.
* `app/services/action_report/snapshot_backed/collectors/registry.py:73` —
  `_KISDomesticQuoteOrderbookAdapter.fetch_quote_orderbook(symbol)`이 `inquire_orderbook(symbol)`
  (기본 `market="J"`=KRX) 호출 후 `"venue": "krx"` 하드코딩.
* `app/services/brokers/kis/domestic_market_data.py:228` —
  `inquire_orderbook(code, market="J")`의 **`market` 파라미터가 venue 셀렉터**. NXT는 KIS market code
  `"NX"`.
* `app/services/market_data/service.py:200-204` — `_KR_VENUE_MAP`: `krx→KrOrderbookVenue("krx","J")`,
  `nxt→("nxt","NX")`, `unified→("unified","UN")`. (이미 `get_orderbook(venue="nxt")` tool이 사용.)
* `app/services/investment_snapshots/collectors.py:77` `CollectorRequest` — `market`/`account_scope`/
  `symbols`/`candidate_limit`/`policy_snapshot`/`user_id`만 보유. **`market_session` 없음.**
* `app/schemas/investment_snapshots_mcp.py:35` `EnsureBundleRequest` — 동일하게 `market_session` 없음.
* `app/services/action_report/common/snapshot_bundle.py:501` — `EnsureBundleRequest`로부터
  `CollectorRequest`를 구성(이 지점에 market_session 전달 필요).
* `app/services/action_report/snapshot_backed/generator.py:335` — `EnsureBundleRequest`를 구성하며
  `request.market_session`을 이미 보유(라인 854에서 다른 경로로 사용 중).
* `app/services/action_report/snapshot_backed/collectors/market.py:145` `_collect_indices` — index는
  세션 비인식. NXT 프리마켓에 KRX 정규장 종가를 그대로 노출(→ `+0.00%` 오독 원인).

## 설계

### 변경 1 — `market_session` threading (enabling)

* `CollectorRequest`(`collectors.py`)와 `EnsureBundleRequest`(`investment_snapshots_mcp.py`)에
  `market_session: MarketSessionLiteral | None = None` 추가. additive·None 기본이라 기존 호출 무영향.
  `MarketSessionLiteral`는 `app/services/action_report/snapshot_backed/request.py`에서 import(이미 정의됨).
* `generator.py:335` `EnsureBundleRequest(...)`에 `market_session=request.market_session` 추가.
* `snapshot_bundle.py:501` `CollectorRequest(...)`에 `market_session=request.market_session` 추가.
* 다른 `EnsureBundleRequest` 생성 사이트(7곳)는 None 기본값 사용 — 변경 없음.

### 변경 2 — symbol collector NXT venue 전환

`_quote_enrichment_plan(request)`:

* `market=="kr" & account_scope=="kis_live"`:
  * `request.market_session == "nxt"` → `default_venue="nxt"`
  * else → `default_venue="krx"` (기존 동작)
* crypto/upbit_live는 불변.

enrichment 루프(`collect` → `_maybe_enrich_quote`)가 plan의 venue를 KIS 어댑터 호출에 전달한다
(아래 변경 3의 확장된 protocol 사용).

### 변경 3 — KIS 어댑터 venue 지원

* `_QuoteOrderbookClient` protocol을 `fetch_quote_orderbook(self, symbol: str, venue: str = "krx")`로 확장
  (기본값으로 Upbit/back-compat 유지).
* `symbol.py::_maybe_enrich_quote`가 `client.fetch_quote_orderbook(symbol, venue=default_venue)` 호출.
* `_KISDomesticQuoteOrderbookAdapter.fetch_quote_orderbook(symbol, venue="krx")`:
  * venue → KIS market code 매핑: `{"krx": "J", "nxt": "NX"}` (미지원 venue → `"J"` fallback).
  * `inquire_orderbook(symbol, market=code)` 호출(**신규 HTTP surface 없음**, 기존 메서드의 market 파라미터만).
  * 반환 payload의 `"venue"`를 실제 venue(`"krx"`/`"nxt"`)로 세팅. `session`/`nxt_eligible`/depth 등은 기존
    로직 유지.
* `_UpbitQuoteOrderbookAdapter.fetch_quote_orderbook(symbol, venue="krx")`는 venue 인자를 받되 무시.

### 변경 4 — market collector index frozen 주석 (acceptance #2)

`MarketEventsSnapshotCollector.collect`에서 `request.market=="kr" & request.market_session=="nxt"`이고
`indices`가 존재하면, payload에 다음을 첨부:

```python
payload["index_session"] = "regular_closed"
payload["index_session_note"] = "KRX 정규장 미개장, 전일 종가 기준(frozen)"
```

새 데이터 fetch 없음. MarketStage가 `+0.00%`를 실제 flat으로 오독하지 않도록 명시만 한다. (market collector도
변경 1로 `request.market_session`을 받게 됨.)

## 테스트 (fake / unit, read-only)

* **T1 (threading):** `CollectorRequest`/`EnsureBundleRequest`가 `market_session`을 보유하고 기본 None.
* **T2 (symbol plan):** `_quote_enrichment_plan`이 `market_session="nxt"` → `default_venue="nxt"`;
  미지정/`"regular"` → `"krx"`.
* **T3 (KIS 어댑터 venue):** fake KIS client(`inquire_orderbook`의 `market` 인자 캡처)로
  `fetch_quote_orderbook(symbol, venue="nxt")` → `market="NX"` 호출 + payload `venue="nxt"`;
  `venue="krx"`(기본) → `market="J"` + `venue="krx"`.
* **T4 (symbol collect NXT):** fake KIS client를 주입한 symbol collector가
  `market_session="nxt"` 요청에서 각 심볼 quote에 `venue="nxt"`를 남긴다.
* **T5 (market index frozen):** kr + `market_session="nxt"` → market payload에
  `index_session="regular_closed"` + note; 다른 세션/마켓엔 미첨부.
* **T6 (mutation guard 회귀):** 기존 ROB-278 import-guard 테스트가 여전히 통과(symbol/registry가 order
  placement/cancel/modify surface를 import 안 함).

## 안전 경계

* read-only. broker/order/watch/order-intent mutation 없음.
* collector는 order mutation surface를 import/호출하지 않음 — 기존 import-guard 테스트 유지.
* **신규 KIS HTTP surface 없음** — 기존 `inquire_orderbook(market=...)`의 파라미터만 사용.
* **DB 마이그레이션 없음** — payload JSON 필드 + 요청 모델 필드(additive)만.
* scheduler/Prefect 활성화 없음. `recommend_stocks` 무관.

## 산출물 / 핸드오프

* 독립 PR (base `origin/main` `861d149e`, worktree `auto_trader.rob-390`/branch `rob-390`).
* 검증 명령/결과 → PR + ROB-394 handoff 코멘트.
* 다음 순서 ROB-392로 인계.

## 비목표 (Out of scope)

* 새 `nxt_orderbook` snapshot kind (Option A 기각).
* NXT 전용 지수 소스 도입 — index는 frozen 주석만, 데이터 교체 없음.
* NXT 세션 시간대 판정 로직 — `market_session`은 요청이 명시(이 PR은 그것을 소비만).
* MarketStage(Hermes 합성 측)의 index frozen 해석 로직 — 본 PR은 evidence/주석만 freeze하고 합성은 Hermes.
