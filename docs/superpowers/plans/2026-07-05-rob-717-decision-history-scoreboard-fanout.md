# ROB-717 decision_history→scoreboard 팬아웃 완화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** decision_history 주입 경로가 3s timeout 안에 안정 수렴하도록 scoreboard 팬아웃에서 OHLCV/브로커 호출을 제거하고, ROB-713 검증 minor 5건을 정리한다.

**Architecture:** `build_trading_scoreboard`에 `include_excursions` 플래그를 추가해 decision_history 경로(`_realized_r_by_tag`)가 `False`로 호출 → per-trade `compute_excursions`(→`get_ohlcv`) 호출을 완전히 스킵. scoreboard MCP·웹은 기본 `True`로 MAE 정확도를 유지한다. 나머지는 소유 파일(`decision_history.py`/`aggregates.py`) 내 국소 수정.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest(asyncio), uv.

## Global Constraints

- 백엔드 전용, **migration 0** (스키마 변경 없음).
- 소유 파일: `app/services/trade_journal/aggregates.py`, `app/services/decision_history.py` (+ 각 테스트), `tests/test_mcp_trading_scoreboard.py`. ROB-715는 소비만 — 웹/insights 컬럼 확장은 이 플랜 밖.
- read-path(=decision_history)에서 브로커 네트워크 호출 **0**.
- `compute_excursions`는 3-tuple `(mae, mfe, degraded)` 시그니처 유지 (기존 `test_excursions_from_stubbed_ohlcv` 의존).
- `TradeMetrics.degraded`는 **기본값 `False`** — 기존 `_tm` 헬퍼가 degraded 없이 생성하므로 필수 필드로 만들면 안 됨.
- 테스트 실행: `uv run pytest <path> -v`. 커밋 메시지 말미:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: `include_excursions` 플래그 — scoreboard 팬아웃 스킵 + cache key

**Files:**
- Modify: `app/services/trade_journal/aggregates.py:482-542` (`build_trading_scoreboard` 시그니처·cache key·per-trade 루프)
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- Produces: `build_trading_scoreboard(db, *, market=None, account_mode=None, date_from=None, date_to=None, setup_tag=None, min_sample=1, include_excursions=True, use_cache=True, now=None) -> dict`. `include_excursions=False`이면 어떤 trade에 대해서도 `compute_excursions`/`get_ohlcv`를 호출하지 않으며 그룹의 MAE/MFE 필드는 `None`. cache key에 `include_excursions`가 포함되어 True/False 결과가 분리 캐시된다.

- [ ] **Step 1: 실패 테스트 작성** — `tests/services/test_trade_journal_aggregates_scoreboard.py` 끝에 추가

```python
@pytest.mark.asyncio
async def test_include_excursions_false_skips_ohlcv(db_session, monkeypatch):
    called = False

    async def spy_get_ohlcv(*a, **k):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(agg, "get_ohlcv", spy_get_ohlcv)
    # market=None, empty CI-owned rows are fine; the assertion is on the call, not counts
    await agg.build_trading_scoreboard(
        db_session, use_cache=False, include_excursions=False
    )
    assert called is False


@pytest.mark.asyncio
async def test_include_excursions_in_cache_key(db_session, monkeypatch):
    from datetime import UTC, datetime

    calls = {"n": 0}

    async def counting_load_fills(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(agg, "load_fills", counting_load_fills)
    stamp = datetime(2026, 7, 5, tzinfo=UTC)
    await agg.build_trading_scoreboard(
        db_session, include_excursions=True, now=stamp
    )
    await agg.build_trading_scoreboard(
        db_session, include_excursions=False, now=stamp
    )
    # distinct cache keys → load_fills ran twice, not served from one cache slot
    assert calls["n"] == 2
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py::test_include_excursions_false_skips_ohlcv tests/services/test_trade_journal_aggregates_scoreboard.py::test_include_excursions_in_cache_key -v`
Expected: FAIL — `build_trading_scoreboard() got an unexpected keyword argument 'include_excursions'`

- [ ] **Step 3: 구현** — `aggregates.py`의 `build_trading_scoreboard`

시그니처에 `include_excursions: bool = True`를 `min_sample` 다음에 추가:

```python
async def build_trading_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
    include_excursions: bool = True,
    use_cache: bool = True,
    now: datetime | None = None,
) -> dict:
```

cache key에 `include_excursions` 추가:

```python
    key = (market, account_mode, date_from, date_to, setup_tag, min_sample, include_excursions)
```

per-trade 루프의 excursions 블록을 조건부로 변경 (기존 라인 524-528):

```python
        mae, mfe = None, None
        if include_excursions:
            try:
                mae, mfe, _degraded = await compute_excursions(t)
            except Exception:
                mae, mfe = None, None
        rows.append(TradeMetrics(t, tag, compute_r_multiple(t, stop), mae, mfe))
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -v`
Expected: PASS (신규 2건 + 기존 전부)

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "feat(ROB-717): include_excursions flag skips OHLCV fanout in scoreboard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: decision_history read-path 스킵 + minor (a) untagged-before-slice

**Files:**
- Modify: `app/services/decision_history.py:138-158` (`_realized_r_by_tag`)
- Test: `tests/services/test_decision_history_realized_r.py`

**Interfaces:**
- Consumes: `build_trading_scoreboard(..., include_excursions=False)` from Task 1.
- Produces: `_realized_r_by_tag`가 `untagged`를 top-3 슬라이스 **전에** 제거하고, `include_excursions=False`로 scoreboard를 호출한다.

- [ ] **Step 1: 실패 테스트 작성** — `tests/services/test_decision_history_realized_r.py` 끝에 추가

```python
@pytest.mark.asyncio
async def test_untagged_dominant_still_returns_real_tags(db_session, monkeypatch):
    import app.services.decision_history as dh

    async def fake_scoreboard(db, *, market=None, include_excursions=True, **kw):
        assert include_excursions is False  # read-path must skip excursions
        return {
            "groups": [
                {"tag": "untagged", "n": 99, "expectancy_r": 0.0, "win_rate": 0.0,
                 "profit_factor": None, "avg_mae": None, "insufficient_sample": False},
                {"tag": "pullback_long", "n": 5, "expectancy_r": 1.2, "win_rate": 0.6,
                 "profit_factor": 2.0, "avg_mae": -0.02, "insufficient_sample": True},
            ],
            "overall": None, "as_of": "x", "count": 104,
        }

    monkeypatch.setattr(
        "app.services.trade_journal.aggregates.build_trading_scoreboard",
        fake_scoreboard,
    )
    out = await dh._realized_r_by_tag(db_session, "kr", None)
    assert "untagged" not in out
    assert "pullback_long" in out
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_decision_history_realized_r.py::test_untagged_dominant_still_returns_real_tags -v`
Expected: FAIL — `assert include_excursions is False` 또는 untagged가 슬라이스에 먹혀 pullback_long 누락

- [ ] **Step 3: 구현** — `_realized_r_by_tag` 본문 교체 (라인 148-158)

```python
    from app.services.trade_journal.aggregates import build_trading_scoreboard

    board = await build_trading_scoreboard(
        db, market=market, include_excursions=False
    )
    groups = [g for g in board.get("groups", []) if g["tag"] != "untagged"]
    ordered = sorted(groups, key=lambda g: (g["tag"] != setup_tag, -int(g["n"])))
    return {g["tag"]: {k: g.get(k) for k in _R_KEYS} for g in ordered[:_MAX_TAGS]}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_decision_history_realized_r.py -v`
Expected: PASS (신규 + 기존 `test_realized_r_by_tag_present_and_bounded` — fake는 `**kw`로 include_excursions 흡수)

- [ ] **Step 5: 커밋**

```bash
git add app/services/decision_history.py tests/services/test_decision_history_realized_r.py
git commit -m "fix(ROB-717): decision_history skips excursions + filter untagged before top-3 slice

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: minor (b) — `load_fills` smoke 필터 확장

**Files:**
- Modify: `app/services/trade_journal/aggregates.py:152-155` (`load_fills`의 `_is_smoke` 호출)
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- Produces: `load_fills`가 `reason`/`thesis`/`strategy`/`notes` 중 하나라도 smoke 토큰을 담으면 해당 fill을 제외한다 (기존 `correlation_id`/`status`에 더해).

- [ ] **Step 1: 실패 테스트 작성** — `tests/services/test_trade_journal_aggregates_scoreboard.py` 끝에 추가

```python
@pytest.mark.asyncio
async def test_load_fills_excludes_smoke_marked_reason(db_session):
    from datetime import UTC, datetime

    from app.models.review import KISLiveOrderLedger

    db_session.add(
        KISLiveOrderLedger(
            symbol="005930",
            side="buy",
            status="filled",
            filled_qty=10,
            avg_fill_price=100.0,
            trade_date=datetime(2026, 6, 1, tzinfo=UTC),
            reason="smoke-only probe do not journal",
        )
    )
    await db_session.flush()
    fills = await agg.load_fills(db_session, market="kr")
    assert all("005930" not in f.symbol or f.price != 100.0 for f in fills)
```

Note: KISLiveOrderLedger의 다른 NOT NULL 컬럼이 있으면 최소값을 채운다 — 실패 시 에러 메시지의 컬럼명을 보고 `account_mode="kis_live"` 등 최소 필드를 추가.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py::test_load_fills_excludes_smoke_marked_reason -v`
Expected: FAIL — smoke row가 필터되지 않아 fill로 포함됨

- [ ] **Step 3: 구현** — `load_fills`의 `_is_smoke` 호출 확장 (라인 152-155)

```python
            if _is_smoke(
                getattr(r, "correlation_id", None),
                getattr(r, "status", None),
                getattr(r, "reason", None),
                getattr(r, "thesis", None),
                getattr(r, "strategy", None),
                getattr(r, "notes", None),
            ):
                continue
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "fix(ROB-717): load_fills smoke filter also checks reason/thesis/strategy/notes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: minor (c) — `excursions_degraded` surface

**Files:**
- Modify: `app/services/trade_journal/aggregates.py:431-470` (`TradeMetrics`, `_agg_one`), `:515-528` (per-trade 루프)
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- Consumes: Task 1의 조건부 excursions 루프.
- Produces: `TradeMetrics(..., degraded: bool = False)`; scoreboard 그룹(및 `overall`) dict에 `excursions_degraded: int` (해당 tag에서 degraded=True인 trade 수).

- [ ] **Step 1: 실패 테스트 작성** — `tests/services/test_trade_journal_aggregates_scoreboard.py` 끝에 추가

```python
def test_excursions_degraded_surfaced_in_group():
    r1 = _tm(0.10, 2.0)
    r2 = _tm(-0.05, -1.0)
    r1.degraded = True  # TradeMetrics is @dataclass (not frozen) → mutable
    [g] = aggregate_by_tag([r1, r2])
    assert g["excursions_degraded"] == 1
```

(`TradeMetrics`는 `@dataclass`(frozen 아님)이므로 `r1.degraded = True` 가능.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py::test_excursions_degraded_surfaced_in_group -v`
Expected: FAIL — `TradeMetrics`에 `degraded` 없음 / 그룹에 `excursions_degraded` 키 없음

- [ ] **Step 3: 구현**

`TradeMetrics`에 필드 추가 (라인 431-437):

```python
@dataclass
class TradeMetrics:
    trade: ClosedTrade
    tag: TagInfo
    r_multiple: float | None
    mae: float | None
    mfe: float | None
    degraded: bool = False
```

`_agg_one`의 반환 dict에 카운트 추가 (`insufficient_sample` 앞):

```python
        "excursions_degraded": sum(1 for r in rows if r.degraded),
        "insufficient_sample": n < _INSUFFICIENT_SAMPLE_N,
```

per-trade 루프에서 degraded를 잡아 저장 (Task 1이 만든 블록 교체):

```python
        mae, mfe = None, None
        degraded = False
        if include_excursions:
            try:
                mae, mfe, degraded = await compute_excursions(t)
            except Exception:
                mae, mfe, degraded = None, None, False
        rows.append(
            TradeMetrics(t, tag, compute_r_multiple(t, stop), mae, mfe, degraded)
        )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py tests/services/test_trade_journal_aggregates_metrics.py -v`
Expected: PASS (기존 `test_excursions_from_stubbed_ohlcv`도 3-tuple 유지되어 green)

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "feat(ROB-717): surface excursions_degraded count per scoreboard group

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: minor (d) — 캐시 반환 격리(deep copy)

**Files:**
- Modify: `app/services/trade_journal/aggregates.py:1-30` (import), `:499-542` (cache hit/store)
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- Produces: `build_trading_scoreboard`의 반환 dict를 호출자가 mutate해도 다음 호출(캐시 hit) 결과가 오염되지 않는다.

- [ ] **Step 1: 실패 테스트 작성** — 파일 끝에 추가

```python
@pytest.mark.asyncio
async def test_cache_returns_isolated_copies(db_session, monkeypatch):
    from datetime import UTC, datetime

    async def empty_load_fills(*a, **k):
        return []

    monkeypatch.setattr(agg, "load_fills", empty_load_fills)
    stamp = datetime(2026, 7, 5, tzinfo=UTC)
    first = await agg.build_trading_scoreboard(db_session, now=stamp)
    first["groups"].append({"tag": "MUTANT"})
    first["count"] = 999
    second = await agg.build_trading_scoreboard(db_session, now=stamp)  # cache hit
    assert second["groups"] == []
    assert second["count"] == 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py::test_cache_returns_isolated_copies -v`
Expected: FAIL — 캐시가 동일 dict를 반환해 `second`에 MUTANT/999가 새어나옴

- [ ] **Step 3: 구현**

파일 상단 import에 `copy` 추가 (기존 `import uuid` 부근):

```python
import copy
import uuid
```

cache hit 반환을 copy로 (라인 502-504):

```python
    if use_cache:
        cached = _scoreboard_cache.get(key)
        if cached and stamp - cached[0] < _SCOREBOARD_TTL_SECONDS:
            return copy.deepcopy(cached[1])
```

store + 최종 반환도 격리 (라인 540-542 교체):

```python
    if use_cache:
        _scoreboard_cache[key] = (stamp, copy.deepcopy(result))
    return result
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "fix(ROB-717): deep-copy scoreboard cache returns to isolate callers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: minor (e) — 빈-DB 스코어보드 tool 테스트 hermetic화

**Files:**
- Modify: `tests/test_mcp_trading_scoreboard.py`

**Interfaces:**
- Consumes: `get_trading_scoreboard` (MCP tool, 시그니처 불변).
- Produces: 테스트가 공유 CI DB 상태·네트워크(`get_ohlcv`)에 의존하지 않고 tool 응답 shape만 검증.

- [ ] **Step 1: 테스트 재작성 (실패 상태로)** — `tests/test_mcp_trading_scoreboard.py` 전체 교체

```python
import pytest

import app.mcp_server.tooling.trading_scoreboard_tools as tool


@pytest.mark.asyncio
async def test_scoreboard_tool_shape_hermetic(monkeypatch):
    async def fake_board(db, **kw):
        return {"groups": [], "overall": None, "as_of": "x", "count": 0}

    monkeypatch.setattr(tool, "build_trading_scoreboard", fake_board)
    result = await tool.get_trading_scoreboard()
    assert set(result) >= {"groups", "overall", "as_of", "count"}
    assert result["groups"] == []
    assert result["count"] == 0
```

- [ ] **Step 2: 실패→통과 확인 (구현은 mock 배선뿐)**

Run: `uv run pytest tests/test_mcp_trading_scoreboard.py -v`
Expected: PASS (mock이 실 DB·네트워크를 차단; `build_trading_scoreboard`가 tool 모듈 네임스페이스에 import되어 있어 patch 가능)

만약 `AttributeError: build_trading_scoreboard`가 나면 `trading_scoreboard_tools.py`가 이미 `from ... import build_trading_scoreboard`(라인 12)로 심볼을 모듈에 바인딩하므로 patch 대상은 `tool.build_trading_scoreboard`가 맞다 — 경로 확인.

- [ ] **Step 3: 커밋**

```bash
git add tests/test_mcp_trading_scoreboard.py
git commit -m "test(ROB-717): make empty-db scoreboard tool test hermetic (no shared DB / no network)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 통합 검증 — 전체 스위트 + 안전 가드 + read-path 무네트워크 실측

**Files:** (검증 전용, 코드 변경 없음)

- [ ] **Step 1: 소유 파일 관련 스위트 전체**

Run:
```bash
uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py \
  tests/services/test_trade_journal_aggregates_metrics.py \
  tests/services/test_decision_history_realized_r.py \
  tests/test_mcp_trading_scoreboard.py -v
```
Expected: 전부 PASS

- [ ] **Step 2: import-경계 가드 (ROB-716 안전 테스트) 회귀 확인**

Run: `uv run pytest tests/test_invest_view_model_safety.py -v`
Expected: PASS — `decision_history` import가 여전히 broker/market-data 체인을 끌어오지 않음 (lazy import 유지)

- [ ] **Step 3: lint / typecheck**

Run: `make lint`
Expected: clean (Ruff + ty). 실패 시 해당 파일만 교정 후 재실행.

- [ ] **Step 4: read-path 무네트워크 실측 (성공 기준)**

decision_history가 `include_excursions=False`로 호출하는지 + `get_ohlcv`가 호출되지 않는지를 스위트로 이미 검증(Task 1·2). 추가로 `_realized_r_by_tag`가 실제로 excursions를 스킵함을 로컬에서 재확인:

Run:
```bash
uv run pytest tests/services/test_decision_history_realized_r.py::test_untagged_dominant_still_returns_real_tags -v
```
Expected: PASS (fake_scoreboard의 `assert include_excursions is False`가 read-path 계약을 강제)

- [ ] **Step 5: 최종 커밋 (필요 시)** — Step 3에서 교정이 있었다면만

```bash
git add -A && git commit -m "chore(ROB-717): lint/typecheck fixups

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

- **Spec coverage:** Part 1a/1b→Task 1, 1c→Task 2, 1d(MCP 기본 True 유지)→Task 1(불변)+Task 6. minor (a)→Task 2, (b)→Task 3, (c)→Task 4, (d)→Task 5, (e)→Task 6. 성공 기준(3s 수렴·네트워크 0)→Task 1·2 테스트 + Task 7 Step 4. 전부 매핑됨.
- **Placeholder scan:** 모든 code step에 실제 코드 포함. "적절한 처리" 류 없음.
- **Type consistency:** `include_excursions=True` 기본값이 Task 1·2·6에서 일관. `TradeMetrics.degraded: bool = False`가 Task 4에서 정의되고 기존 `_tm`(degraded 미지정)과 호환. `excursions_degraded: int` 키 이름이 Task 4 내에서 일관. `compute_excursions` 3-tuple 유지 — Global Constraints·Task 1·4에서 일관.
