# Binance Demo 스캘핑 리뷰 LLM vs 규칙 비교 (Phase 3 D-PR2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일별 스캘핑 리뷰/벤치마크를 `session_tag`별로 분리 생성(혼합 집계 수정)하고, `/invest/scalping`에서 LLM(`"llm"`)과 규칙 baseline(`""`)을 별도 카드로 비교 가능하게 한다.

**Architecture:** ① `list_analytics`/`_rollup_for`/`build_draft`/benchmark_runner에 `COALESCE(session_tag,'')` 필터 추가 ② 신규 `list_session_tags` distinct ③ 일별 flow가 `{""} ∪ distinct tags` 순회 ④ 프런트가 전 tag 리뷰를 라벨된 비교 strip으로 렌더.

**Tech Stack:** Python 3.13, SQLAlchemy(async, `func.coalesce`), pytest-asyncio, React/TypeScript, vitest.

## Global Constraints

- 변경은 worktree `feature/binance-demo-scalping-review-comparison`에서. canonical repo는 main 고정.
- **무회귀 — `list_analytics` 기본값 `session_tag: str | None = None`(=필터 없음, 전체)**: 트레이드 테이블 등 기존 호출자는 session_tag를 안 넘기므로 전 행을 받아야 한다. `None`이면 필터 미적용. `_rollup_for`/benchmark는 review의 `session_tag`(str, `""` 포함)를 명시 전달해 필터.
- **NULL/"" 정합**: 필터·distinct 모두 `func.coalesce(ScalpTradeAnalytics.session_tag, "")` 사용. 스케줄러(규칙)=NULL, llm 도구=`"llm"`, 리뷰 baseline grain=`""`.
- **de-mixing(의도된 동작 변경)**: 기존 `""` 리뷰는 rule+llm 혼합이었으나 이제 rule(NULL/"")만 집계.
- **flow는 `{""}` 항상 포함**: 빈 날에도 규칙 baseline `""` 리뷰 생성(Phase 2 무회귀). per-(product, tag) `begin_nested()` SAVEPOINT 격리.
- 마이그레이션 없음(`session_tag`는 기존 컬럼: `scalp_trade_analytics.session_tag` nullable, `scalping_daily_reviews.session_tag` NOT NULL server_default="").
- 데모 전용 / 주문·브로커 mutation 없음(리뷰는 read+집계 persistence).
- 커밋 trailer:
  ```
  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
  ```

## File Structure

- `app/services/scalping_reviews/service.py` (수정) — `list_analytics` 필터 + `_rollup_for`/`build_draft` 스레딩 + `list_session_tags` 신규 + `func` import.
- `app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py` (수정) — `list_analytics` 호출에 session_tag.
- `app/jobs/binance_demo_scalping_review.py` (수정) — `_refresh_with_session` tag 순회.
- `frontend/invest/src/pages/desktop/DesktopScalpingPage.tsx` (수정) — SESSION_TAG_LABEL + 비교 strip + 카드 라벨.
- Test: `tests/services/scalping_reviews/test_service.py`, `tests/jobs/test_binance_demo_scalping_review.py`, `frontend/invest/src/__tests__/DesktopScalpingPage.test.tsx`.

---

### Task 1: 리뷰 rollup을 session_tag로 분리 (service.py)

**Files:**
- Modify: `app/services/scalping_reviews/service.py`
- Test: `tests/services/scalping_reviews/test_service.py`

**Interfaces:**
- Produces: `list_analytics(*, review_date, product, session_tag: str | None = None)`; `_rollup_for(review_date, product, session_tag: str = "")`; `list_session_tags(*, review_date, product) -> list[str]`; `build_draft`가 자기 `session_tag`로 집계.

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/scalping_reviews/test_service.py`에 추가(기존 `_instrument`/`_analytics`/`_NOW`/`_DATE` 헬퍼 재사용; `_analytics`는 `**kw`로 `session_tag` 전달 가능, `tag=`는 open_client_order_id 구분용). `ScalpingReviewService`는 파일 상단에서 이미 import됨:

```python
@pytest.mark.asyncio
async def test_rollup_separates_by_session_tag(db_session) -> None:
    iid = await _instrument(db_session, "SEPXRPUSDT")
    # 규칙(NULL) 1건 + llm 2건, 같은 날/product
    await _analytics(db_session, iid, tag="rule1", symbol="SEPXRPUSDT",
                     entry_price=Decimal("100"), exit_price=Decimal("101"),
                     entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
                     gross_pnl_usdt=Decimal("1.0"), net_return_bps=Decimal("90"),
                     exit_reason="take_profit")
    for i in range(2):
        await _analytics(db_session, iid, tag=f"llm{i}", symbol="SEPXRPUSDT",
                         session_tag="llm", entry_price=Decimal("100"),
                         exit_price=Decimal("102"), entry_notional_usdt=Decimal("100"),
                         net_pnl_usdt=Decimal("1.5"), gross_pnl_usdt=Decimal("1.6"),
                         net_return_bps=Decimal("150"), exit_reason="take_profit")
    svc = ScalpingReviewService(db_session)
    rule_review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW, session_tag="")
    llm_review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW, session_tag="llm")
    assert rule_review.trade_count == 1
    assert llm_review.trade_count == 2
    # list_analytics: None=전체(무회귀), 값=필터
    assert len(await svc.list_analytics(review_date=_DATE, product="usdm_futures")) == 3
    assert len(await svc.list_analytics(review_date=_DATE, product="usdm_futures", session_tag="")) == 1
    assert len(await svc.list_analytics(review_date=_DATE, product="usdm_futures", session_tag="llm")) == 2


@pytest.mark.asyncio
async def test_list_session_tags_distinct_coalesced(db_session) -> None:
    iid = await _instrument(db_session, "TAGXRPUSDT")
    await _analytics(db_session, iid, tag="n", symbol="TAGXRPUSDT")  # NULL → ""
    await _analytics(db_session, iid, tag="l1", symbol="TAGXRPUSDT", session_tag="llm")
    await _analytics(db_session, iid, tag="l2", symbol="TAGXRPUSDT", session_tag="llm")
    svc = ScalpingReviewService(db_session)
    tags = await svc.list_session_tags(review_date=_DATE, product="usdm_futures")
    assert tags == ["", "llm"]
```

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.phase3-dpr2-review-comparison && uv run pytest tests/services/scalping_reviews/test_service.py::test_rollup_separates_by_session_tag tests/services/scalping_reviews/test_service.py::test_list_session_tags_distinct_coalesced -v`
Expected: FAIL — `list_analytics() got an unexpected keyword argument 'session_tag'` / `AttributeError: list_session_tags`. (test deps 없으면 `uv sync --all-groups` 먼저.)

- [ ] **Step 3: `func` import 추가**

`service.py` 상단 `from sqlalchemy import select`를 교체:

```python
from sqlalchemy import func, select
```

- [ ] **Step 4: `list_analytics`에 session_tag 필터(None=전체)**

`list_analytics` 메서드 전체를 교체:

```python
    async def list_analytics(
        self, *, review_date: dt.date, product: str, session_tag: str | None = None
    ) -> list[ScalpTradeAnalytics]:
        """Raw scalp_trade_analytics round-trip rows for a day/product, oldest
        first. ``session_tag=None`` returns all rows (the per-trade table);
        a value filters to ``COALESCE(session_tag,'') == session_tag`` so the
        per-tag review rolls up only its own trades. Read-only."""
        start = dt.datetime.combine(review_date, dt.time.min, tzinfo=dt.UTC)
        end = start + dt.timedelta(days=1)
        stmt = select(ScalpTradeAnalytics).where(
            ScalpTradeAnalytics.product == product,
            ScalpTradeAnalytics.created_at >= start,
            ScalpTradeAnalytics.created_at < end,
        )
        if session_tag is not None:
            stmt = stmt.where(
                func.coalesce(ScalpTradeAnalytics.session_tag, "") == session_tag
            )
        stmt = stmt.order_by(ScalpTradeAnalytics.created_at)
        return list((await self._session.scalars(stmt)).all())
```

- [ ] **Step 5: `_rollup_for` + `build_draft` 스레딩 + `list_session_tags` 신규**

`_rollup_for` 교체:

```python
    async def _rollup_for(
        self, review_date: dt.date, product: str, session_tag: str = ""
    ) -> RollupResult:
        rows = await self.list_analytics(
            review_date=review_date, product=product, session_tag=session_tag
        )
        return build_rollup(rows)
```

`build_draft` 내 `rollup = await self._rollup_for(review_date, product)`를 교체:

```python
        rollup = await self._rollup_for(review_date, product, session_tag=session_tag)
```

`list_analytics` 메서드 **다음에** `list_session_tags` 추가:

```python
    async def list_session_tags(
        self, *, review_date: dt.date, product: str
    ) -> list[str]:
        """Distinct ``COALESCE(session_tag,'')`` for that day/product, sorted.
        Empty list when no analytics rows exist."""
        start = dt.datetime.combine(review_date, dt.time.min, tzinfo=dt.UTC)
        end = start + dt.timedelta(days=1)
        tag_col = func.coalesce(ScalpTradeAnalytics.session_tag, "")
        rows = await self._session.scalars(
            select(tag_col)
            .where(
                ScalpTradeAnalytics.product == product,
                ScalpTradeAnalytics.created_at >= start,
                ScalpTradeAnalytics.created_at < end,
            )
            .distinct()
            .order_by(tag_col)
        )
        return list(rows)
```

- [ ] **Step 6: 통과 + 회귀**

Run: `uv run pytest tests/services/scalping_reviews/test_service.py -v`
Expected: PASS (신규 2 + 기존 전부 green — 기존 호출자는 session_tag 미전달 → None → 전체, build_draft는 기본 ""로 NULL/"" 집계 무회귀).

- [ ] **Step 7: 커밋**

```bash
git add app/services/scalping_reviews/service.py tests/services/scalping_reviews/test_service.py
git commit -F - <<'EOF'
feat: scope scalping review rollup by session_tag

list_analytics에 COALESCE(session_tag,'') 필터(None=전체, 무회귀) + _rollup_for/
build_draft가 자기 session_tag로 집계 분리(rule NULL/"" vs llm de-mixing) +
list_session_tags distinct 신규.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 2: benchmark 분리 + flow tag 순회

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py`
- Modify: `app/jobs/binance_demo_scalping_review.py`
- Test: `tests/jobs/test_binance_demo_scalping_review.py`

**Interfaces:**
- Consumes: Task 1의 `list_analytics(..., session_tag=...)`, `list_session_tags(...)`, `build_draft(..., session_tag=...)`.
- Produces: flow가 (product, tag)별 리뷰+벤치마크 생성; summary 항목에 `sessionTag`.

- [ ] **Step 1: 실패 테스트 작성**

`tests/jobs/test_binance_demo_scalping_review.py`에 추가(기존 `_instrument`/`_analytics`/`_FakeMD`/`_DATE`/`_NOW`/`settings`/`run_demo_scalping_review_refresh`/`ScalpingReviewService` 재사용):

```python
@pytest.mark.asyncio
async def test_flow_builds_per_session_tag(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    iid = await _instrument(db_session, "XRPUSDT")
    # 규칙(NULL) 1 + llm 1
    await _analytics(db_session, iid, tag="r", symbol="XRPUSDT",
                     entry_price=Decimal("100"), exit_price=Decimal("101"),
                     entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
                     gross_pnl_usdt=Decimal("1.0"), net_return_bps=Decimal("90"),
                     exit_reason="take_profit")
    await _analytics(db_session, iid, tag="l", symbol="XRPUSDT", session_tag="llm",
                     entry_price=Decimal("100"), exit_price=Decimal("102"),
                     entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("1.5"),
                     gross_pnl_usdt=Decimal("1.6"), net_return_bps=Decimal("150"),
                     exit_reason="take_profit")
    md = _FakeMD({"XRPUSDT": (100.0, 101.0)})
    result = await run_demo_scalping_review_refresh(
        session=db_session, market_data=md, review_date=_DATE,
        products=("usdm_futures",), now=_NOW,
    )
    assert result["status"] == "ran"
    tags = {s["sessionTag"] for s in result["products"]}
    assert tags == {"", "llm"}
    svc = ScalpingReviewService(db_session)
    rule = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    llm = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "llm")
    assert rule.trade_count == 1 and llm.trade_count == 1
    assert llm.benchmark_return_bps is not None  # benchmark per tag stored


@pytest.mark.asyncio
async def test_flow_empty_day_still_builds_rule_baseline(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    md = _FakeMD({})
    result = await run_demo_scalping_review_refresh(
        session=db_session, market_data=md, review_date=_DATE,
        products=("usdm_futures",), now=_NOW,
    )
    assert [s["sessionTag"] for s in result["products"]] == [""]  # {""} always
    svc = ScalpingReviewService(db_session)
    assert await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "") is not None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/jobs/test_binance_demo_scalping_review.py::test_flow_builds_per_session_tag tests/jobs/test_binance_demo_scalping_review.py::test_flow_empty_day_still_builds_rule_baseline -v`
Expected: FAIL — summary 항목에 `sessionTag` 없음 / llm 리뷰 행 없음(현재 "" 한 행만, 혼합 집계).

- [ ] **Step 3: benchmark_runner에 session_tag 필터 전달**

`benchmark_runner.py`의 `rows = await service.list_analytics(review_date=review_date, product=product)`를 교체:

```python
    rows = await service.list_analytics(
        review_date=review_date, product=product, session_tag=session_tag
    )
```

- [ ] **Step 4: `_refresh_with_session` tag 순회**

`_refresh_with_session`의 `for product in products:` 블록 전체를 교체:

```python
    for product in products:
        try:
            tags = sorted(
                {""} | set(
                    await service.list_session_tags(
                        review_date=review_date, product=product
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 — isolate product on enumeration failure
            logger.exception(
                "demo scalping review tag enumeration failed for product=%s", product
            )
            errors.append({"product": product, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for session_tag in tags:
            try:
                async with session.begin_nested():  # SAVEPOINT: isolate per (product, tag)
                    review = await service.build_draft(
                        review_date=review_date,
                        product=product,
                        now=now,
                        session_tag=session_tag,
                    )
                    benchmark = await compute_and_store_daily_benchmark(
                        session=session,
                        market_data=market_data,
                        review_date=review_date,
                        product=product,
                        now=now,
                        session_tag=session_tag,
                    )
                    summaries.append(
                        {
                            "product": product,
                            "sessionTag": session_tag,
                            "tradeCount": review.trade_count,
                            "netReturnBps": _num(review.net_return_bps),
                            "benchmarkReturnBps": _num(benchmark),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 — isolate; savepoint rolled back
                logger.exception(
                    "demo scalping review refresh failed for product=%s tag=%s",
                    product,
                    session_tag,
                )
                errors.append(
                    {
                        "product": product,
                        "sessionTag": session_tag,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
```

- [ ] **Step 5: 통과 + 회귀**

Run: `uv run pytest tests/jobs/test_binance_demo_scalping_review.py -v`
Expected: PASS (신규 2 + 기존 — 기존 테스트는 NULL 트레이드만이라 tags={""}, "" 리뷰 동일 생성).

- [ ] **Step 6: 커밋**

```bash
git add app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py app/jobs/binance_demo_scalping_review.py tests/jobs/test_binance_demo_scalping_review.py
git commit -F - <<'EOF'
feat: per-session_tag review+benchmark in daily refresh flow

benchmark_runner가 session_tag로 analytics 필터(tag별 buy&hold) + flow가
{""}∪distinct tag 순회하여 (product,tag)별 리뷰+벤치마크 생성(규칙 항상 포함,
per-(product,tag) SAVEPOINT 격리). LLM vs 규칙 baseline 분리 측정.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 3: 프런트 — tag 라벨 + 비교 strip

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopScalpingPage.tsx`
- Test: `frontend/invest/src/__tests__/DesktopScalpingPage.test.tsx`

**Interfaces:**
- Consumes: `fetchScalpingReviews(...)` → `{items: ScalpingReview[]}` (tag별 행); `ScalpingReview.sessionTag`, `.metrics.{tradeCount,winCount,lossCount,netReturnBps,netPnlUsdt}`.

- [ ] **Step 1: 실패 테스트 작성**

`frontend/invest/src/__tests__/DesktopScalpingPage.test.tsx`에 추가(기존 `REVIEW`/`ACTION`/`TRADE_OK`/`wrap`/`ScalpingRoute`/`scalpingApi` 재사용). 두 tag 리뷰를 반환하도록 mock:

```typescript
test("renders per-session_tag comparison strip with labels", async () => {
  const ruleReview = { ...REVIEW, id: 1, sessionTag: "", metrics: { ...REVIEW.metrics, netReturnBps: "-10" } };
  const llmReview = { ...REVIEW, id: 2, sessionTag: "llm", metrics: { ...REVIEW.metrics, netReturnBps: "150" } };
  vi.spyOn(scalpingApi, "fetchScalpingReviews").mockResolvedValue({ items: [ruleReview, llmReview] });
  vi.spyOn(scalpingApi, "fetchScalpingReview").mockResolvedValue({ review: ruleReview, actions: [ACTION] });
  vi.spyOn(scalpingApi, "fetchScalpingTrades").mockResolvedValue({ items: [TRADE_OK] });

  render(wrap(<ScalpingRoute />));

  await waitFor(() => expect(screen.getByTestId("scalping-session-comparison")).toBeInTheDocument());
  const strip = screen.getByTestId("scalping-session-comparison");
  expect(within(strip).getByText("규칙")).toBeInTheDocument();
  expect(within(strip).getByText("LLM")).toBeInTheDocument();
});
```

`within` import 확인: 파일 상단 `import { render, screen, waitFor } from "@testing-library/react";`에 `within` 추가.

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.phase3-dpr2-review-comparison/frontend/invest && npm test -- DesktopScalpingPage 2>&1 | tail -20`
Expected: FAIL — `scalping-session-comparison` testid 없음.

- [ ] **Step 3: SESSION_TAG_LABEL 추가**

`DesktopScalpingPage.tsx`의 `STATUS_LABEL` 정의 근처(파일 상단 라벨 상수들 옆)에 추가:

```typescript
const SESSION_TAG_LABEL: Record<string, string> = { "": "규칙", llm: "LLM" };
const sessionTagLabel = (tag: string): string => SESSION_TAG_LABEL[tag] ?? tag ?? "규칙";
```

- [ ] **Step 4: 전체 items 보관**

`load` 콜백에서 `const found = reviews.items[0] ?? null;` 위에 전체 리스트 상태 저장 추가. 컴포넌트 상단 state 선언부(`const [building, setBuilding] = useState(false);` 다음)에:

```typescript
  const [reviewList, setReviewList] = useState<ScalpingReview[]>([]);
```

(`ScalpingReview` 타입 import가 없으면 `import type { ScalpingReview, ScalpingProduct } from "../../types/scalping";`에 추가.)

`load` 콜백 내 `const found = reviews.items[0] ?? null;` **앞에** 추가:

```typescript
      setReviewList(reviews.items);
```

- [ ] **Step 5: 비교 strip 렌더**

`{/* 2. Daily loop card */}` 블록 **앞에**(summary 다음) 추가:

```typescript
              {/* 1.5 Per-session_tag comparison (LLM vs rule baseline) */}
              {reviewList.length > 1 && (
                <Card>
                  <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>세션별 비교 (LLM vs 규칙)</h2>
                  <div data-testid="scalping-session-comparison" style={{ display: "grid", gap: 6 }}>
                    {reviewList.map((r) => (
                      <div key={r.id} style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 13 }}>
                        <strong style={{ minWidth: 48 }}>{sessionTagLabel(r.sessionTag)}</strong>
                        <span style={{ color: "var(--fg-3)" }}>{r.metrics.tradeCount}건</span>
                        <span style={{ color: "var(--fg-3)" }}>승/패 {r.metrics.winCount}/{r.metrics.lossCount}</span>
                        <span>net {na(r.metrics.netReturnBps)} bps</span>
                        <span>순손익 {na(r.metrics.netPnlUsdt)}</span>
                      </div>
                    ))}
                  </div>
                </Card>
              )}
```

(`na(...)` 헬퍼는 이 파일에서 이미 사용 중 — 라인 199 `na(metrics.netPnlUsdt)`.)

`{/* 2. Daily loop card */}`의 카드 헤더 `<span>`에 sessionTag 라벨 추가 — 라인 214-217의 `<span>`을 교체:

```typescript
                    <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
                      [{sessionTagLabel(review.sessionTag)}] {STATUS_LABEL[review.status] ?? review.status}
                      {exitReasonSummary ? ` · 종료사유: ${exitReasonSummary}` : ""}
                    </span>
```

- [ ] **Step 6: 통과 + 회귀**

Run: `cd /Users/mgh3326/work/auto_trader.phase3-dpr2-review-comparison/frontend/invest && npm test -- DesktopScalpingPage 2>&1 | tail -20 && npx tsc --noEmit 2>&1 | tail -5`
Expected: 신규 + 기존 DesktopScalpingPage 테스트 PASS, tsc clean.

- [ ] **Step 7: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.phase3-dpr2-review-comparison
git add frontend/invest/src/pages/desktop/DesktopScalpingPage.tsx frontend/invest/src/__tests__/DesktopScalpingPage.test.tsx
git commit -F - <<'EOF'
feat(invest): per-session_tag scalping review comparison strip + labels

/invest/scalping이 전 session_tag 리뷰를 라벨된 비교 strip으로 렌더(규칙 vs LLM
net bps·순손익 나란히) + 일별 루프 카드에 tag 라벨. 기존 items[0] 단일 렌더 유지.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### 최종 검증

- [ ] **Step: 백엔드 스코프 + 프런트 + lint/type**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.phase3-dpr2-review-comparison
uv run pytest tests/services/scalping_reviews/ tests/jobs/test_binance_demo_scalping_review.py tests/services/brokers/binance/ -q -p no:cacheprovider
uv run ruff format --check app/services/scalping_reviews/service.py app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py app/jobs/binance_demo_scalping_review.py
uv run ruff check app/services/scalping_reviews/service.py app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py app/jobs/binance_demo_scalping_review.py
uv run ty check app/
cd frontend/invest && npm test -- DesktopScalpingPage 2>&1 | tail -5
```
Expected: 백엔드 신규+회귀 PASS(binance audit 포함 — 새 binance-참조 파일 없으므로 allowlist 변경 불필요), ruff/ty clean, 프런트 PASS.

---

## Self-Review (spec 대비)

- **§2.1 rollup session_tag 필터** → Task 1(list_analytics None-default 필터 + _rollup_for/build_draft 스레딩). ✓
- **§2.1 benchmark 필터** → Task 2 Step 3. ✓
- **§2.1 tag 열거** → Task 1 list_session_tags. ✓
- **§2.1 flow tag 순회** → Task 2 Step 4(`{""}∪distinct`, per-(product,tag) SAVEPOINT). ✓
- **§2.1 UI 라벨** → Task 3(SESSION_TAG_LABEL + 비교 strip + 카드 라벨). ✓
- **§5 NULL/"" 정합** → `func.coalesce(...,"")` 필터·distinct 일관(Task 1). ✓
- **§6 에러** → tag enumeration 실패 시 product skip, (product,tag) SAVEPOINT 격리, 빈 날 `{""}`만(무회귀). ✓
- **§7 테스트** → rollup 분리/distinct(T1), flow per-tag/빈 날(T2), UI 라벨 strip(T3), 무회귀(None default + 기존 테스트). ✓
- **§8 무회귀** → list_analytics 기본 None(전체); build_draft 기본 ""; 기존 호출자 불변. ✓
- 마이그레이션 없음. 전용 비교 컴포넌트(델타/승패 표)·benchmark 프런트 surfacing은 후속(범위 밖). ✓
- **타입 일관**: `list_analytics(session_tag: str|None=None)` ↔ `_rollup_for(session_tag: str="")` ↔ `build_draft(session_tag="")` ↔ benchmark/flow가 str 전달; `list_session_tags -> list[str]`. ✓
