# ROB-414 US Symbol Resolution Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `prepare_bundle` symbol 스테이지가 `market="us"`에서 미보유 후보 티커를 `us_symbol_universe`로 폴백 해소하고, 해소 실패 시 티커별 reason_code를 반환하도록 한다.

**Architecture:** `SymbolSnapshotCollector._resolve_symbol_payloads`에 US 분기를 추가해 stock_info(우선) → us_symbol_universe(폴백) 2단 해소를 수행하고, 잔여 미해소 티커는 per-ticker `reason_code`를 산출해 partial 스냅샷 payload의 `unresolved` 필드에 담는다. `SymbolStage`는 `unresolved`가 있으면 reason 포함으로 렌더한다. US-only, KR/crypto 무변경, migration 0, read-only.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest (asyncio), MagicMock/AsyncMock fixtures.

---

## File Structure

- Modify: `app/services/action_report/snapshot_backed/collectors/symbol.py` — US 폴백 해소 + reason_code 산출 (Unit 1, 2)
- Modify: `app/services/investment_stages/stages/symbol.py` — `unresolved` reason 렌더 (Unit 3)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py` — collector 동작
- Test: `tests/services/investment_stages/stages/test_symbol.py` — stage 렌더

설계의 reason_code 분류(`not_registered` / `inactive` / `universe_empty` / `universe_lookup_error`)는 collector에 모듈 상수/헬퍼로 둔다.

---

## Task 1: US 폴백 해소 — stock_info miss → us_symbol_universe hit

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/symbol.py`
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

배경: 현재 `_resolve_symbol_payloads`는 crypto 외 모든 market을 `stock_info`만으로 해소한다(`symbol.py:159`).
US는 stock_info에 없는 후보를 `us_symbol_universe`에서 폴백 해소해야 한다.

테스트는 `session.execute`가 **두 번** 호출됨(1: stock_info, 2: us_symbol_universe)을 가정해 `side_effect`로
서로 다른 scalars 결과를 돌려준다. 기존 `_stock_info_session` 헬퍼(단일 결과)는 KR/crypto용이므로, US용
2단 결과 헬퍼를 테스트에 추가한다.

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 의 Symbol collector 섹션(파일 내 `# Symbol collector` 주석 부근, 약 1468 라인 이후)에 추가:

```python
def _us_universe_row(
    symbol: str,
    *,
    name_kr: str = "",
    name_en: str = "",
    exchange: str = "NASD",
    is_active: bool = True,
):
    class _Row:
        def __init__(self) -> None:
            self.symbol = symbol
            self.name_kr = name_kr
            self.name_en = name_en
            self.exchange = exchange
            self.is_active = is_active

    return _Row()


def _two_stage_session(stock_rows: list[Any], universe_rows: list[Any]) -> MagicMock:
    """Session whose 1st execute() returns stock_info rows, 2nd returns
    us_symbol_universe rows (US fallback path issues two queries)."""
    session = MagicMock()

    def _result(rows: list[Any]) -> MagicMock:
        scalars = MagicMock(all=MagicMock(return_value=rows))
        return MagicMock(scalars=MagicMock(return_value=scalars))

    session.execute = AsyncMock(
        side_effect=[_result(stock_rows), _result(universe_rows)]
    )
    return session


@pytest.mark.asyncio
async def test_symbol_collector_us_falls_back_to_universe_for_unheld():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # stock_info has the held name; the candidate is only in us_symbol_universe.
    session = _two_stage_session(
        stock_rows=[_stock_info_row("AAPL", "애플")],
        universe_rows=[_us_universe_row("HCA", name_en="HCA Healthcare")],
    )
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        symbols=["AAPL", "HCA"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    resolved = {r.symbol for r in results if r.symbol}
    assert resolved == {"AAPL", "HCA"}
    hca = next(r for r in results if r.symbol == "HCA")
    assert hca.payload_json["instrument_type"] == "equity_us"
    assert hca.payload_json["name"] == "HCA Healthcare"
    assert hca.payload_json["exchange"] == "NASD"
    # No partial/missing row when everything resolved.
    assert all(r.freshness_status != "partial" for r in results)
```

(`_stock_info_row` 헬퍼는 같은 파일에 이미 존재함.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_symbol_collector_us_falls_back_to_universe_for_unheld -v`
Expected: FAIL — HCA가 resolved에 없음(`unresolved`/missing에 빠짐), `resolved == {"AAPL"}`.

- [ ] **Step 3: Write minimal implementation**

`app/services/action_report/snapshot_backed/collectors/symbol.py`:

(a) import 추가 (파일 상단 import 블록, `UpbitSymbolUniverse` 옆):

```python
from app.models.us_symbol_universe import USSymbolUniverse
```

(b) `_resolve_symbol_payloads`를 US 분기 추가하도록 교체. 현재 메서드(약 133–172 라인)의 마지막 stock_info 블록을 다음으로 바꾼다. crypto 분기는 그대로 두고, stock_info 조회 후 US는 폴백을 수행:

```python
        if market == "crypto":
            stmt = select(UpbitSymbolUniverse).where(
                UpbitSymbolUniverse.market.in_(symbols)
            )
            rows = (await self._session.execute(stmt)).scalars().all()
            return [
                {
                    "symbol": row.market,
                    "name": row.korean_name,
                    "instrument_type": "crypto",
                    "exchange": "upbit",
                    "sector": None,
                    "market_cap": None,
                    "is_active": row.is_active,
                }
                for row in rows
            ]
        stmt = select(StockInfo).where(StockInfo.symbol.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        payloads = [
            {
                "symbol": row.symbol,
                "name": row.name,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "sector": row.sector,
                "market_cap": row.market_cap,
                "is_active": row.is_active,
            }
            for row in rows
        ]
        if market == "us":
            resolved_syms = {p["symbol"] for p in payloads}
            remaining = [s for s in symbols if s not in resolved_syms]
            if remaining:
                payloads.extend(
                    await self._resolve_us_universe_payloads(remaining)
                )
        return payloads
```

(c) US universe 폴백 헬퍼를 클래스에 추가(`_resolve_symbol_payloads` 바로 아래):

```python
    async def _resolve_us_universe_payloads(
        self, symbols: list[str]
    ) -> list[dict[str, Any]]:
        """Resolve US symbols absent from ``stock_info`` against the
        ``us_symbol_universe`` master (active rows only)."""
        stmt = select(USSymbolUniverse).where(
            USSymbolUniverse.symbol.in_(symbols),
            USSymbolUniverse.is_active.is_(True),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "symbol": row.symbol,
                "name": row.name_kr or row.name_en or row.symbol,
                "instrument_type": "equity_us",
                "exchange": row.exchange,
                "sector": None,
                "market_cap": None,
                "is_active": row.is_active,
            }
            for row in rows
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_symbol_collector_us_falls_back_to_universe_for_unheld -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add app/services/action_report/snapshot_backed/collectors/symbol.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-414): US symbol 미해소 후보 us_symbol_universe 폴백 해소

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 보유(stock_info) 메타 우선 + 중복 없음

**Files:**
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

stock_info에 있는 종목은 universe 폴백 대상에서 제외되어야 하며(중복 row 미생성), sector/market_cap 등
풍부한 메타가 보존되어야 한다. Task 1 구현이 `remaining`에서 stock_info 해소분을 제외하므로 통과해야 한다(회귀 가드).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_symbol_collector_us_prefers_stock_info_meta_no_dup():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # AAPL is in BOTH stock_info and the universe; stock_info must win and the
    # universe row must NOT produce a duplicate.
    session = _two_stage_session(
        stock_rows=[_stock_info_row("AAPL", "애플")],
        universe_rows=[_us_universe_row("AAPL", name_en="Apple Inc")],
    )
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        symbols=["AAPL"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    aapl_rows = [r for r in results if r.symbol == "AAPL"]
    assert len(aapl_rows) == 1
    # stock_info meta preserved (sector/market_cap come only from stock_info).
    assert aapl_rows[0].payload_json["sector"] == "Tech"
    assert aapl_rows[0].payload_json["market_cap"] == 1_000_000.0
    assert aapl_rows[0].payload_json["name"] == "애플"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_symbol_collector_us_prefers_stock_info_meta_no_dup -v`
Expected: PASS (Task 1 구현이 `remaining`에서 AAPL 제외 → universe 미조회분). 만약 FAIL이면 Task 1 폴백 필터 로직 점검.

> 참고: AAPL이 `remaining`에 없으므로 2번째 execute(universe)는 호출되지 않는다. `_two_stage_session`의 side_effect 2개 중 첫 번째만 소비되어도 무방하다.

- [ ] **Step 3: (구현 변경 없음 — Task 1로 충족)**

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "test(ROB-414): US 보유종목 stock_info 메타 우선·중복 없음 회귀 가드

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 미해소 US 티커 per-ticker reason_code (`unresolved` payload)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/symbol.py`
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

두 소스 모두 miss → `not_registered`; universe row 존재하나 inactive → `inactive`; universe 0행 →
`universe_empty`. partial 스냅샷 payload에 `missing_symbols`(back-compat) + `unresolved`(구조화)를 담는다.

reason 산출은 universe를 active 제한 없이 한 번 더 조회하지 않도록, 폴백 헬퍼가 **(해소 payload, reason_map)**
둘 다 반환하게 바꾼다. 즉 universe를 `is_active` 제한 없이 조회하고, active면 payload로, inactive면
reason `inactive`로, 아예 없으면 `not_registered`(0행이면 `universe_empty`)로 분류한다.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_symbol_collector_us_unresolved_reason_codes():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # NOPE: absent everywhere → not_registered.
    # DEAD: present in universe but inactive → inactive.
    session = _two_stage_session(
        stock_rows=[],
        universe_rows=[_us_universe_row("DEAD", name_en="Dead Co", is_active=False)],
    )
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        symbols=["NOPE", "DEAD"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    unresolved = {
        u["symbol"]: u["reason_code"] for u in partial.payload_json["unresolved"]
    }
    assert unresolved == {"NOPE": "not_registered", "DEAD": "inactive"}
    # back-compat bulk list still present.
    assert set(partial.payload_json["missing_symbols"]) == {"NOPE", "DEAD"}


@pytest.mark.asyncio
async def test_symbol_collector_us_universe_empty_reason():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _two_stage_session(stock_rows=[], universe_rows=[])
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        symbols=["NVDA"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    unresolved = {
        u["symbol"]: u["reason_code"] for u in partial.payload_json["unresolved"]
    }
    assert unresolved == {"NVDA": "universe_empty"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "us_unresolved_reason_codes or us_universe_empty_reason" -v`
Expected: FAIL — `unresolved` 키 없음(partial payload엔 `missing_symbols`만 존재).

- [ ] **Step 3: Write minimal implementation**

`app/services/action_report/snapshot_backed/collectors/symbol.py`:

(a) 모듈 상수(파일 상단, `_DEFAULT_QUOTE_ENRICHMENT_LIMIT` 부근) 추가:

```python
_US_REASON_NOT_REGISTERED = "not_registered"
_US_REASON_INACTIVE = "inactive"
_US_REASON_UNIVERSE_EMPTY = "universe_empty"
_US_REASON_UNIVERSE_LOOKUP_ERROR = "universe_lookup_error"
```

(b) `_resolve_us_universe_payloads`를 **(payloads, reason_map)** 반환 구조로 교체. is_active 제한을 제거하고
inactive/누락을 분류한다:

```python
    async def _resolve_us_universe_payloads(
        self, symbols: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Resolve US symbols absent from ``stock_info`` against the
        ``us_symbol_universe`` master.

        Returns ``(resolved_payloads, reason_by_symbol)``: active universe rows
        become payloads; inactive rows / absent symbols become per-symbol reason
        codes. An empty universe maps every requested symbol to
        ``universe_empty``.
        """
        stmt = select(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        by_symbol = {row.symbol: row for row in rows}

        # Empty universe → every requested symbol is unresolvable for the same
        # reason (operator must run the sync). Distinguish from not_registered.
        any_universe_rows = await has_any_rows(
            self._session, USSymbolUniverse.symbol
        )

        payloads: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}
        for symbol in symbols:
            row = by_symbol.get(symbol)
            if row is None:
                reasons[symbol] = (
                    _US_REASON_NOT_REGISTERED
                    if any_universe_rows
                    else _US_REASON_UNIVERSE_EMPTY
                )
                continue
            if not row.is_active:
                reasons[symbol] = _US_REASON_INACTIVE
                continue
            payloads.append(
                {
                    "symbol": row.symbol,
                    "name": row.name_kr or row.name_en or row.symbol,
                    "instrument_type": "equity_us",
                    "exchange": row.exchange,
                    "sector": None,
                    "market_cap": None,
                    "is_active": row.is_active,
                }
            )
        return payloads, reasons
```

(c) import 추가 (상단):

```python
from app.services.symbol_universe_common import has_any_rows
```

(d) `_resolve_symbol_payloads`의 US 블록을 reason_map까지 반환하도록 변경. 메서드 시그니처를
**(payloads, us_reasons)** 튜플 반환으로 바꾼다. crypto/KR은 빈 reason map을 반환:

```python
    async def _resolve_symbol_payloads(
        self, market: str, symbols: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if market == "crypto":
            stmt = select(UpbitSymbolUniverse).where(
                UpbitSymbolUniverse.market.in_(symbols)
            )
            rows = (await self._session.execute(stmt)).scalars().all()
            return (
                [
                    {
                        "symbol": row.market,
                        "name": row.korean_name,
                        "instrument_type": "crypto",
                        "exchange": "upbit",
                        "sector": None,
                        "market_cap": None,
                        "is_active": row.is_active,
                    }
                    for row in rows
                ],
                {},
            )
        stmt = select(StockInfo).where(StockInfo.symbol.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        payloads = [
            {
                "symbol": row.symbol,
                "name": row.name,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "sector": row.sector,
                "market_cap": row.market_cap,
                "is_active": row.is_active,
            }
            for row in rows
        ]
        if market != "us":
            return payloads, {}
        resolved_syms = {p["symbol"] for p in payloads}
        remaining = [s for s in symbols if s not in resolved_syms]
        us_reasons: dict[str, str] = {}
        if remaining:
            try:
                extra, us_reasons = await self._resolve_us_universe_payloads(
                    remaining
                )
            except Exception as exc:  # noqa: BLE001 — fail-open, preserve stock_info
                us_reasons = {
                    s: _US_REASON_UNIVERSE_LOOKUP_ERROR for s in remaining
                }
                _ = exc
            else:
                payloads.extend(extra)
        return payloads, us_reasons
```

(e) `collect`에서 호출부 및 partial payload 구성을 변경. 현재 `collect`의
`base_payloads = await self._resolve_symbol_payloads(...)` 호출(약 190 라인)과
missing 처리(약 255–268 라인)를 다음과 같이 바꾼다:

호출부:

```python
        try:
            base_payloads, us_reasons = await self._resolve_symbol_payloads(
                request.market, symbols
            )
```

(`except` 블록은 그대로.)

missing 블록(`missing = [s for s in symbols if s not in seen_symbols]` 이후)을 교체:

```python
        missing = [s for s in symbols if s not in seen_symbols]
        if missing:
            missing_payload: dict[str, Any] = {"missing_symbols": missing}
            if us_reasons:
                missing_payload["unresolved"] = [
                    {
                        "symbol": s,
                        "reason_code": us_reasons.get(s, _US_REASON_NOT_REGISTERED),
                    }
                    for s in missing
                ]
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=missing_payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(missing)},
                )
            )
```

마지막 "every symbol missed" fallback 블록(약 270–284 라인)도 동일하게 `unresolved`를 포함하도록 교체:

```python
        if not results:
            empty_payload: dict[str, Any] = {"missing_symbols": symbols}
            if us_reasons:
                empty_payload["unresolved"] = [
                    {
                        "symbol": s,
                        "reason_code": us_reasons.get(s, _US_REASON_NOT_REGISTERED),
                    }
                    for s in symbols
                ]
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=empty_payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(symbols)},
                )
            )
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "us_unresolved_reason_codes or us_universe_empty_reason" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add app/services/action_report/snapshot_backed/collectors/symbol.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-414): 미해소 US 티커 per-ticker reason_code(unresolved payload)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: KR/crypto 무변경 회귀 가드

**Files:**
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

KR/crypto는 2차 universe 쿼리를 하지 않고 `unresolved`도 첨부하지 않아야 한다(`missing_symbols`-only).
기존 `test_symbol_collector_returns_results_for_each_symbol`(market=kr, 단일 execute)이 깨지지 않아야 한다.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_symbol_collector_kr_missing_has_no_unresolved_field():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # KR uses the single-query stock_info path; missing rows stay bulk-only.
    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    assert partial.payload_json["missing_symbols"] == ["000660"]
    assert "unresolved" not in partial.payload_json
    # KR path issues exactly one query (no universe fallback).
    assert session.execute.await_count == 1
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_symbol_collector_kr_missing_has_no_unresolved_field -v`
Expected: PASS (Task 3 구현이 KR에서 `us_reasons={}` → `unresolved` 미첨부, execute 1회).

- [ ] **Step 3: (구현 변경 없음 — Task 3로 충족)**

- [ ] **Step 4: Run full collector + stage regression**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -v`
Expected: PASS (기존 symbol collector 테스트 전부 green).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "test(ROB-414): KR/crypto missing은 unresolved 미첨부·단일쿼리 회귀 가드

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: SymbolStage — `unresolved` reason 렌더

**Files:**
- Modify: `app/services/investment_stages/stages/symbol.py`
- Test: `tests/services/investment_stages/stages/test_symbol.py`

`unresolved`(list of `{symbol, reason_code}`)가 있으면 `missing_data`를
`unresolved_symbols: AZO (not_registered), DEAD (inactive)` 형태로 렌더. 없으면 기존 bulk 렌더 유지.

- [ ] **Step 1: Write the failing test**

`tests/services/investment_stages/stages/test_symbol.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_symbol_stage_renders_unresolved_reason_codes():
    ctx = _ctx(
        [
            {"symbol": "AAPL", "name": "애플"},
            {
                "missing_symbols": ["AZO", "DEAD"],
                "unresolved": [
                    {"symbol": "AZO", "reason_code": "not_registered"},
                    {"symbol": "DEAD", "reason_code": "inactive"},
                ],
            },
        ]
    )
    payload = await SymbolStage().run(ctx)
    line = next(m for m in payload.missing_data if "unresolved_symbols" in m)
    assert "AZO (not_registered)" in line
    assert "DEAD (inactive)" in line


@pytest.mark.asyncio
async def test_symbol_stage_bulk_render_when_no_reason_codes():
    # back-compat: missing_symbols without `unresolved` keeps bulk rendering.
    ctx = _ctx(
        [
            {"symbol": "KRW-BTC", "name": "비트코인"},
            {"missing_symbols": ["DOGECOIN", "FOO"]},
        ]
    )
    payload = await SymbolStage().run(ctx)
    line = next(m for m in payload.missing_data if "unresolved_symbols" in m)
    assert "DOGECOIN" in line and "FOO" in line
    assert "(" not in line  # no per-ticker reason parens
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/investment_stages/stages/test_symbol.py -k "renders_unresolved_reason_codes or bulk_render_when_no_reason" -v`
Expected: FAIL — 첫 테스트가 reason paren 미렌더로 실패.

- [ ] **Step 3: Write minimal implementation**

`app/services/investment_stages/stages/symbol.py`의 `run` 메서드에서 collect 루프와 missing 구성 변경.

(a) missing 수집 루프(약 66–73 라인)에서 `unresolved`도 수집하도록 변경:

```python
        snaps = context.snapshots_for("symbol")
        resolved: list[tuple[InvestmentSnapshot, dict[str, Any]]] = []
        missing: list[str] = []
        unresolved_reasons: dict[str, str] = {}
        for snap in snaps:
            payload = snap.payload_json or {}
            if payload.get("symbol"):
                resolved.append((snap, payload))
                continue
            unresolved = payload.get("unresolved")
            if isinstance(unresolved, list):
                for item in unresolved:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("symbol"), str)
                        and isinstance(item.get("reason_code"), str)
                    ):
                        unresolved_reasons[item["symbol"]] = item["reason_code"]
            if isinstance(payload.get("missing_symbols"), list):
                missing.extend(
                    s for s in payload["missing_symbols"] if isinstance(s, str)
                )
```

(b) `missing_data` 구성(약 124–128 라인)을 reason 포함 렌더로 변경:

```python
        missing_data: list[str] = []
        if missing:
            uniq = sorted(set(missing))
            rendered = [
                f"{s} ({unresolved_reasons[s]})" if s in unresolved_reasons else s
                for s in uniq
            ]
            missing_data.append(f"unresolved_symbols: {', '.join(rendered)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/investment_stages/stages/test_symbol.py -v`
Expected: PASS (신규 2건 + 기존 전부 green).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add app/services/investment_stages/stages/symbol.py tests/services/investment_stages/stages/test_symbol.py
git commit -m "feat(ROB-414): SymbolStage가 unresolved reason_code를 per-ticker 렌더

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 스테이지 docstring known-limitation 갱신 + 전체 검증

**Files:**
- Modify: `app/services/investment_stages/stages/symbol.py` (docstring)

`symbol.py` 스테이지 docstring의 "Known limitation (ROB-369)"은 US도 stock_info에서만 해소된다는 옛 서술을
담고 있다. US가 us_symbol_universe로 폴백됨을 반영해 갱신한다(crypto는 여전히 thin).

- [ ] **Step 1: docstring 갱신**

`app/services/investment_stages/stages/symbol.py` 상단 docstring의 "Known limitation" 문단을 교체:

```python
"""
...
Known limitation (ROB-369 / ROB-414): ``SymbolSnapshotCollector`` resolves
KR metadata from ``stock_info`` and US metadata from ``stock_info`` with a
``us_symbol_universe`` fallback for unheld candidates (ROB-414); quotes are
enriched only for KR + ``kis_live``. Crypto reads ``upbit_symbol_universe``
for metadata but has no quote adapter yet, so crypto symbols resolve thin —
this stage reports genuinely unresolvable tickers honestly under
``missing_data`` (``unresolved_symbols`` with per-ticker reason codes for US)
rather than fabricating metadata.
"""
```

(기존 문단의 의미를 보존하되 US 폴백 사실을 반영. 정확한 기존 텍스트는 파일 상단 14–21 라인 참조하여 해당 문단만 교체.)

- [ ] **Step 2: Lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run ruff check app/services/action_report/snapshot_backed/collectors/symbol.py app/services/investment_stages/stages/symbol.py && uv run ruff format --check app/services/action_report/snapshot_backed/collectors/symbol.py app/services/investment_stages/stages/symbol.py`
Expected: All checks passed.

- [ ] **Step 3: 변경 모듈 테스트 전체**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py tests/services/investment_stages/stages/test_symbol.py -v`
Expected: PASS (collector + stage 전부 green).

- [ ] **Step 4: Mutation import guard (read-only invariant 확인)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-414 && uv run pytest -k "import_guard or mutation" -q 2>&1 | tail -20`
Expected: symbol 모듈 관련 import guard 테스트 green (broker mutation 미도입 확인). guard 테스트가 없으면 skip.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-414
git add app/services/investment_stages/stages/symbol.py
git commit -m "docs(ROB-414): SymbolStage known-limitation에 US universe 폴백 반영

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec 커버리지:**
- Unit 1 (US 폴백 해소) → Task 1, 2 ✅
- Unit 2 (reason_code / unresolved payload) → Task 3 ✅
- Unit 3 (스테이지 렌더) → Task 5 ✅
- KR/crypto 무변경 → Task 4 ✅
- docstring/검증 → Task 6 ✅
- reason_code 4종(not_registered/inactive/universe_empty/universe_lookup_error) → Task 3 모두 구현 (lookup_error는 except 경로, 단위 테스트는 핵심 3종 커버; lookup_error는 except 분기로 명시).

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드 포함.

**Type 일관성:** `_resolve_symbol_payloads`는 Task 3에서 `(payloads, reasons)` 튜플 반환으로 일관 변경(crypto/KR도 빈 dict 반환). `_resolve_us_universe_payloads`는 Task 3에서 `(payloads, reason_map)` 튜플로 최종 확정(Task 1의 단일-리스트 버전을 Task 3에서 교체 — Task 1 호출부도 Task 3에서 함께 갱신됨). `us_reasons` 변수명 collect 전체에서 일관.

> 주의(실행자): Task 1은 `_resolve_us_universe_payloads`를 리스트 반환으로 만들고, Task 3에서 튜플 반환으로 **교체**한다. 또한 `_resolve_symbol_payloads`도 Task 1에서 리스트, Task 3에서 튜플 반환으로 바뀐다. Task 3의 코드 블록이 최종 형태이므로 그대로 덮어쓰면 된다.

**안전 경계 재확인:** US-only 분기, KR/crypto 무변경, migration 0, broker/order/watch/order-intent mutation 없음, read-only.
