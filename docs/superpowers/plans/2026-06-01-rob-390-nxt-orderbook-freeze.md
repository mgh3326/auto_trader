# ROB-390 NXT orderbook evidence freeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `market_session="nxt"` 리포트 번들에 NXT live orderbook evidence(venue/session/spread/depth/as_of)를 freeze하고, KRX 정규장 미개장 index가 frozen임을 명시한다.

**Architecture:** Option B — 새 snapshot kind 없이 기존 `symbol` quote enrichment의 venue를 `market_session="nxt"`일 때 `nxt`로 전환. enabling으로 `market_session`을 `EnsureBundleRequest`→`CollectorRequest`로 threading. KIS 어댑터가 venue→KIS market code(`J`/`NX`)를 매핑해 기존 `inquire_orderbook(market=...)`만 사용(신규 HTTP surface 없음). market collector는 nxt 세션에서 index frozen 주석만 첨부.

**Tech Stack:** Python 3.13, pytest (`uv run pytest`), pydantic 요청 모델, 기존 `snapshot_backed/collectors/*` + `market_data` venue 매핑.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-390-nxt-orderbook-freeze-design.md`

---

## File Structure

- **Modify** `app/services/investment_snapshots/collectors.py` — `CollectorRequest`에 `market_session` 필드.
- **Modify** `app/schemas/investment_snapshots_mcp.py` — `EnsureBundleRequest`에 `market_session` 필드.
- **Modify** `app/services/action_report/common/snapshot_bundle.py` — `CollectorRequest` 생성 시 market_session 전달.
- **Modify** `app/services/action_report/snapshot_backed/generator.py` — `EnsureBundleRequest` 생성 시 market_session 전달.
- **Modify** `app/services/action_report/snapshot_backed/collectors/registry.py` — KIS 어댑터 venue 지원.
- **Modify** `app/services/action_report/snapshot_backed/collectors/symbol.py` — protocol venue 인자 + plan venue 전환 + enrich 호출.
- **Modify** `app/services/action_report/snapshot_backed/collectors/market.py` — nxt index frozen 주석.
- **Test** `tests/services/action_report/snapshot_backed/test_collectors.py` — 신규 + 기존 fake/assert 업데이트.

> 실행 시 모든 명령은 worktree `/Users/mgh3326/work/auto_trader.rob-390`에서 `uv run` 으로 수행.
> `MarketSessionLiteral = Literal["regular", "nxt", "pre", "post", "24x7"]` (`app/schemas/investment_reports.py:38`).

---

## Task 1: `market_session`을 요청 모델에 추가 (threading enabling)

**Files:**
- Modify: `app/services/investment_snapshots/collectors.py:77-95` (`CollectorRequest`)
- Modify: `app/schemas/investment_snapshots_mcp.py:35-45` (`EnsureBundleRequest`)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가:

```python
def test_collector_request_carries_market_session_default_none():
    from app.services.investment_snapshots.collectors import CollectorRequest

    req = CollectorRequest(market="kr", account_scope="kis_live", policy_snapshot={})
    assert req.market_session is None
    req2 = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        policy_snapshot={},
        market_session="nxt",
    )
    assert req2.market_session == "nxt"


def test_ensure_bundle_request_carries_market_session_default_none():
    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest

    req = EnsureBundleRequest(market="kr", account_scope="kis_live")
    assert req.market_session is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k market_session_default -v`
Expected: FAIL — `pydantic ValidationError` (extra="forbid") on `market_session`, or AttributeError.

- [ ] **Step 3: Add the field to `CollectorRequest`**

`app/services/investment_snapshots/collectors.py`, `CollectorRequest` 클래스에 `user_id` 필드 뒤에 추가:

```python
    market_session: str | None = None
    """ROB-390 — venue/session context ("regular"/"nxt"/...). ``None`` = unset.
    Collectors that switch venue by trading session (e.g. NXT orderbook) read
    this; left ``None`` for callers that do not distinguish sessions."""
```

> `CollectorRequest`는 lock된 enum 의존을 피하려고 `str | None`을 쓴다(소비 측에서 `== "nxt"` 비교만 함).

- [ ] **Step 4: Add the field to `EnsureBundleRequest`**

`app/schemas/investment_snapshots_mcp.py` 상단 import에 (이미 없으면) 추가:

```python
from app.schemas.investment_reports import MarketSessionLiteral
```

`EnsureBundleRequest` 클래스(`symbols` 필드 근처)에 추가:

```python
    market_session: MarketSessionLiteral | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k market_session_default -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_snapshots/collectors.py app/schemas/investment_snapshots_mcp.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-390): add market_session to CollectorRequest + EnsureBundleRequest

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `market_session` threading (generator → ensure → CollectorRequest)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py:335` (`EnsureBundleRequest(...)`)
- Modify: `app/services/action_report/common/snapshot_bundle.py:501` (`CollectorRequest(...)`)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가 (bundle 빌더가 CollectorRequest로 market_session을 전달하는지 fake collector로 검증):

```python
@pytest.mark.asyncio
async def test_snapshot_bundle_threads_market_session_into_collector_request():
    import datetime as dt

    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
    from app.services.action_report.common.snapshot_bundle import (
        SnapshotBundleEnsureService,
    )
    from app.services.investment_snapshots.collectors import (
        CollectorRequest,
        SnapshotCollectResult,
    )

    captured: dict = {}

    class _CapturingCollector:
        snapshot_kind = "market"

        async def collect(self, request: CollectorRequest):
            captured["market_session"] = request.market_session
            return [
                SnapshotCollectResult(
                    snapshot_kind="market",
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"ok": True},
                    origin="auto_trader_db",
                    as_of=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
                    freshness_status="fresh",
                    coverage={},
                )
            ]

    service = SnapshotBundleEnsureService.__new__(SnapshotBundleEnsureService)
    service._collectors = {"market": _CapturingCollector()}

    from app.services.action_report.snapshot_backed.collectors._base import (
        SnapshotKindPolicy,
    )

    # Use the same kind-policy plumbing the service expects; resolve a minimal
    # policy for the "market" kind via the service helper under test.
    results, warnings, attempted = await service._collect_for_kind(
        kind_policy=_market_kind_policy(),
        request=EnsureBundleRequest(
            market="kr",
            account_scope="kis_live",
            market_session="nxt",
        ),
        policy_snapshot={},
    )
    assert attempted is True
    assert captured["market_session"] == "nxt"
```

> 위 테스트의 `_collect_for_kind` / `_market_kind_policy` / `SnapshotKindPolicy` 경로 이름은 구현 시 `snapshot_bundle.py`의 실제 메서드명·헬퍼와 맞춘다(라인 ~478의 `_collect_for_kind` 시그니처 확인). 핵심 단언은 `captured["market_session"] == "nxt"` 하나다. 만약 kind-policy 구성이 과도하면, 더 단순히 `service` 인스턴스를 정식 생성하지 않고 `_collect_for_kind`에 필요한 최소 `kind_policy` 객체(`snapshot_kind="market"`, `collector_timeout=dt.timedelta(seconds=5)`)를 `types.SimpleNamespace`로 만들어 주입한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k threads_market_session -v`
Expected: FAIL — `captured["market_session"]` is `None` (snapshot_bundle still builds CollectorRequest without market_session).

- [ ] **Step 3: Thread market_session in `snapshot_bundle.py`**

`app/services/action_report/common/snapshot_bundle.py`, `CollectorRequest(...)` 생성(라인 ~501)에 한 줄 추가:

```python
        collect_request = CollectorRequest(
            market=request.market,
            account_scope=request.account_scope,
            symbols=request.symbols,
            candidate_limit=request.candidate_limit,
            policy_snapshot=policy_snapshot,
            user_id=request.user_id,
            market_session=request.market_session,
        )
```

- [ ] **Step 4: Thread market_session in `generator.py`**

`app/services/action_report/snapshot_backed/generator.py`, `EnsureBundleRequest(...)` 생성(라인 ~335)에 한 줄 추가:

```python
            EnsureBundleRequest(
                market=request.market,
                ...
                market_session=request.market_session,
            )
```

> `...` 부분은 기존 인자를 그대로 두고 `market_session=request.market_session`만 추가한다. `request`(SnapshotBacked report request)는 이미 `market_session`을 보유(라인 854에서 사용 중).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k threads_market_session -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/common/snapshot_bundle.py app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-390): thread market_session generator->ensure->CollectorRequest

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: KIS 어댑터 venue 지원 (protocol + adapter)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/symbol.py:71` (`_QuoteOrderbookClient` protocol)
- Modify: `app/services/action_report/snapshot_backed/collectors/registry.py:73-131` (`_KISDomesticQuoteOrderbookAdapter`); `:133-164` (`_UpbitQuoteOrderbookAdapter`)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_kis_adapter_maps_nxt_venue_to_market_code_nx():
    import pandas as pd
    from unittest.mock import AsyncMock

    from app.services.action_report.snapshot_backed.collectors.registry import (
        _KISDomesticQuoteOrderbookAdapter,
    )

    captured: dict = {}

    kis_client = MagicMock()
    kis_client.inquire_price = AsyncMock(
        return_value=pd.DataFrame({"close": [70_000.0]})
    )

    async def _inquire_orderbook(code, market="J"):
        captured["market"] = market
        return {"askp1": "70100", "bidp1": "69900", "askp_rsqn1": "10", "bidp_rsqn1": "12"}

    kis_client.inquire_orderbook = AsyncMock(side_effect=_inquire_orderbook)

    adapter = _KISDomesticQuoteOrderbookAdapter(kis_client)
    raw = await adapter.fetch_quote_orderbook("005930", venue="nxt")
    assert captured["market"] == "NX"
    assert raw["venue"] == "nxt"

    raw_krx = await adapter.fetch_quote_orderbook("005930")  # default venue
    assert captured["market"] == "J"
    assert raw_krx["venue"] == "krx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k maps_nxt_venue -v`
Expected: FAIL — `fetch_quote_orderbook()` got an unexpected keyword argument `venue`.

- [ ] **Step 3: Extend the protocol signature**

`app/services/action_report/snapshot_backed/collectors/symbol.py`, `_QuoteOrderbookClient` protocol의 메서드 시그니처를 교체:

```python
    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]: ...
```

- [ ] **Step 4: Add venue mapping to the KIS adapter**

`app/services/action_report/snapshot_backed/collectors/registry.py`, `_KISDomesticQuoteOrderbookAdapter` 클래스 위에 상수 추가:

```python
# ROB-390 — venue -> KIS domestic market-division code. "J"=KRX, "NX"=NXT.
_VENUE_TO_KIS_MARKET_CODE = {"krx": "J", "nxt": "NX"}
```

`fetch_quote_orderbook` 시그니처와 orderbook 호출, 반환 venue를 교체:

```python
    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("kis client unavailable")
        market_code = _VENUE_TO_KIS_MARKET_CODE.get(venue, "J")
        price_df = await self._client.inquire_price(symbol)
        orderbook = await self._client.inquire_orderbook(symbol, market=market_code)
        ...
```

그리고 반환 dict의 `"venue": "krx"`를 `"venue": venue if venue in _VENUE_TO_KIS_MARKET_CODE else "krx",`로 교체. 나머지 로직(`_num`, depth, session, nxt_eligible)은 그대로 유지.

- [ ] **Step 5: Make the Upbit adapter accept (and ignore) venue**

`app/services/action_report/snapshot_backed/collectors/registry.py`, `_UpbitQuoteOrderbookAdapter.fetch_quote_orderbook` 시그니처를 교체(venue 인자만 추가, 본문 불변):

```python
    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]:
        _ = venue  # Upbit has a single venue; argument kept for protocol parity.
```

> 기존 본문 첫 줄 앞에 `_ = venue`만 넣고 나머지는 유지.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k maps_nxt_venue -v`
Expected: PASS (1 passed).

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/symbol.py app/services/action_report/snapshot_backed/collectors/registry.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-390): KIS adapter maps venue->market code (krx=J, nxt=NX)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: symbol collector venue 전환 (nxt 세션) + 기존 테스트 갱신

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/symbol.py:115-128` (`_quote_enrichment_plan`); `:317` (`_maybe_enrich_quote` 호출)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py` (신규 + 기존 fake/assert 갱신)

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_symbol_collector_switches_to_nxt_venue_when_nxt_session():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])

    captured: dict = {}

    async def fetch_quote(symbol: str, venue: str = "krx") -> dict[str, Any]:
        captured["venue"] = venue
        return {
            "last_price": 70_000.0,
            "best_bid": 69_900.0,
            "best_ask": 70_100.0,
            "bid_depth": 100.0,
            "ask_depth": 120.0,
            "venue": venue,
            "as_of": "2026-06-01T08:30:00+09:00",
            "session": "nxt",
            "nxt_eligible": True,
        }

    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(side_effect=fetch_quote)
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        policy_snapshot={},
        user_id=42,
        market_session="nxt",
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert captured["venue"] == "nxt"
    assert payload["quote"]["venue"] == "nxt"
    assert payload["quote"]["session"] == "nxt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k switches_to_nxt_venue -v`
Expected: FAIL — `captured["venue"] == "krx"` (plan still hardcodes krx and call passes no venue).

- [ ] **Step 3: Make the enrichment plan venue session-aware**

`app/services/action_report/snapshot_backed/collectors/symbol.py`, `_quote_enrichment_plan`의 KR 분기를 교체:

```python
    def _quote_enrichment_plan(
        self, request: CollectorRequest
    ) -> tuple[_QuoteOrderbookClient | None, bool, str, str] | None:
        if request.market == "kr" and request.account_scope == "kis_live":
            venue = "nxt" if request.market_session == "nxt" else "krx"
            return (self._kis_quote_client, True, venue, "kis_live")
        if request.market == "crypto" and request.account_scope == "upbit_live":
            return (self._upbit_quote_client, False, "upbit", "upbit_live")
        return None
```

- [ ] **Step 4: Pass the plan venue into the client call**

`app/services/action_report/snapshot_backed/collectors/symbol.py`, `_maybe_enrich_quote` 내부의 client 호출(라인 ~317)을 교체:

```python
        try:
            raw = await client.fetch_quote_orderbook(symbol, venue=default_venue)
```

> 단, crypto 경로의 `default_venue`는 `"upbit"`이고 Upbit 어댑터는 venue를 무시하므로 안전하다.

- [ ] **Step 5: Update existing symbol-collector test fakes for the new signature**

기존 fake quote client들이 `fetch_quote_orderbook(symbol, venue=...)` 호출로 깨지지 않도록 시그니처에 `venue` 인자를 추가한다. 다음 위치를 모두 수정:

`_fake_quote_client_ok` (라인 ~1567):
```python
    async def fetch_quote(symbol: str, venue: str = "krx") -> dict[str, Any]:
```

`test_symbol_collector_quote_exception_marks_unavailable`의 `fetch`(라인 ~1700 부근):
```python
    async def fetch(symbol: str, venue: str = "krx"):
```

`test_symbol_collector_quote_empty_book_marks_no_data_reason`의 `fetch`(라인 ~1740 부근):
```python
    async def fetch(symbol: str, venue: str = "krx"):
```

그리고 기존 단언(라인 ~1622):
```python
    quote_client.fetch_quote_orderbook.assert_awaited_once_with("005930")
```
을 다음으로 교체:
```python
    quote_client.fetch_quote_orderbook.assert_awaited_once_with("005930", venue="krx")
```

> `assert_not_called()`를 쓰는 테스트(no-kis-live / no-user-id)는 호출되지 않으므로 시그니처 수정 불필요. 단, `quote_enrichment_cap` 테스트가 `_fake_quote_client_ok`를 재사용하면 위 시그니처 수정으로 자동 호환된다.

- [ ] **Step 6: Run the full symbol-collector suite**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "symbol_collector or switches_to_nxt" -v`
Expected: 모두 PASS (신규 nxt 테스트 + 갱신된 기존 테스트).

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/symbol.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-390): symbol collector switches to nxt venue on nxt session

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: market collector index frozen 주석 (nxt 세션)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/market.py:114-126` (`collect`, payload 구성)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_market_collector_kr_nxt_marks_index_frozen():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.0},
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    req = _request(market="kr")
    req = req.model_copy(update={"market_session": "nxt"})
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["index_session"] == "regular_closed"
    assert "frozen" in payload["index_session_note"]


@pytest.mark.asyncio
async def test_market_collector_kr_regular_session_has_no_frozen_note():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.5},
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))
    payload = results[0].payload_json
    assert "index_session" not in payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "index_frozen or regular_session_has_no_frozen" -v`
Expected: FAIL — `payload["index_session"]` KeyError (nxt 주석 미구현).

- [ ] **Step 3: Add the frozen annotation in `market.py`**

`app/services/action_report/snapshot_backed/collectors/market.py`, `collect` 메서드의 `indices` 첨부 직후(라인 ~123, `payload["indices"] = indices` 다음)에 추가:

```python
        if (
            request.market == "kr"
            and request.market_session == "nxt"
            and indices
        ):
            payload["index_session"] = "regular_closed"
            payload["index_session_note"] = (
                "KRX 정규장 미개장, 전일 종가 기준(frozen)"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "index_frozen or regular_session_has_no_frozen" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/market.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-390): annotate KR index as frozen during nxt session

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 전체 검증 + mutation guard + lint + PR + 핸드오프

**Files:** 없음 (검증 전용)

- [ ] **Step 1: Run the collectors + threading suites**

Run:
```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -v
```
Expected: 모두 PASS (신규 + 기존 회귀 없음).

- [ ] **Step 2: Run the mutation/import-guard regression (ROB-278)**

Run:
```bash
uv run pytest tests/services/action_report/snapshot_backed/test_generator_safety.py tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v
```
Expected: 모두 PASS — symbol/registry/market 변경 후에도 order placement/cancel/modify surface import 없음.

- [ ] **Step 3: Lint (CLAUDE.md 게이트)**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 둘 다 통과. (format 위반이면 `uv run ruff format app/ tests/` 후 재확인·커밋 — `ruff check`만으로는 lint job이 떨어진다.)

- [ ] **Step 4: Push branch and open PR (base: main)**

Run:
```bash
git push -u origin rob-390
gh pr create --base main --title "fix(ROB-390): NXT live orderbook evidence를 report bundle에 freeze" --body "$(cat <<'EOF'
## 요약
ROB-390: `market_session="nxt"` 리포트 번들에 NXT live orderbook evidence를 freeze (Option B — 새 snapshot kind 없이 기존 symbol quote enrichment의 venue 전환).

1. `market_session`을 `EnsureBundleRequest`→`CollectorRequest`로 threading (additive, None 기본).
2. symbol collector: `market_session="nxt"` & kr/kis_live → enrichment venue를 `nxt`로 전환.
3. KIS 어댑터: venue→KIS market code 매핑(`krx=J`, `nxt=NX`) 후 기존 `inquire_orderbook(market=...)` 호출 (신규 HTTP surface 없음). quote payload의 venue/session/spread/depth/as_of가 NXT 기준으로 freeze.
4. market collector: kr + nxt 세션 → indices payload에 `index_session="regular_closed"` + frozen note (KRX 정규장 미개장 `+0.00%` 방향 오독 방지).

## 테스트
- `tests/services/action_report/snapshot_backed/test_collectors.py` — market_session threading / KIS venue→NX / symbol nxt 전환 / market index frozen + 기존 fake·assert 갱신
- mutation/import-guard 회귀(ROB-278) 유지

## 안전 경계
read-only. broker/order/watch mutation 없음. 신규 KIS HTTP surface 없음(기존 inquire_orderbook market 파라미터만). DB 마이그레이션 없음(payload + 요청 모델 additive 필드만). scheduler 활성화 없음.

## 잔여 (handoff)
- index는 frozen 주석만 첨부(데이터 교체 없음). MarketStage의 frozen 해석/합성은 Hermes 측. NXT 전용 지수 소스 도입은 비목표.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL 출력. (출력된 URL 확인 후에만 PR 번호 인용.)

- [ ] **Step 5: ROB-394 handoff 코멘트 작성**

ROB-394에 ROB-390 결과(PR 링크 + 검증 결과 + 비목표: NXT 지수 소스/Hermes frozen 해석)를 남기고, 다음 순서가 ROB-392임을 명시한다. (Linear `save_comment`.)

---

## Self-Review

**Spec coverage:**
- 변경1 market_session threading → Task 1(필드) + Task 2(generator/bundle). ✅
- 변경2 symbol venue 전환 → Task 4. ✅
- 변경3 KIS 어댑터 venue → Task 3. ✅
- 변경4 market index frozen → Task 5. ✅
- 테스트 T1/T2/T3/T4/T5/T6 → Task1/Task4/Task3/Task4/Task5/Task6에 매핑. ✅
- 안전 경계(read-only, no new HTTP surface, no migration, mutation guard) → Task 3는 기존 inquire_orderbook만, Task 6 guard 회귀. ✅
- 비목표(새 kind, NXT 지수 소스, Hermes 해석) → 미구현, Task6 handoff 명시. ✅

**Placeholder scan:** 모든 step에 실제 코드/명령. Task 2 Step 1은 헬퍼명 확인 주의 포함하되 핵심 단언은 구체적. ✅

**Type consistency:** `market_session: str | None`(CollectorRequest) / `MarketSessionLiteral | None`(EnsureBundleRequest) — 소비 측은 `== "nxt"` 문자열 비교만(타입 호환). `fetch_quote_orderbook(symbol, venue="krx")`(Task3 protocol+adapter ↔ Task4 호출 ↔ 기존 fake 갱신 ↔ assert venue 인자). `_VENUE_TO_KIS_MARKET_CODE={"krx":"J","nxt":"NX"}`(Task3). payload 키 `index_session`/`index_session_note`(Task5 ↔ T5). ✅

**Commit-별 CI green:** Task3은 protocol/adapter에 venue 기본값 추가(symbol.py는 아직 venue 미전달)라 기존 테스트 무손상. Task4에서 호출 변경과 동시에 기존 fake/assert를 같은 커밋으로 갱신 → 각 커밋 green 유지. ✅
