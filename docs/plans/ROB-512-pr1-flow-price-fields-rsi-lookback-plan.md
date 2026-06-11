# ROB-512 PR1 — flow 프리셋 가격 필드 + RSI lookback 수리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `screen_stocks_snapshot`의 investor_flow_momentum/쌍끌이(double_buy) 결과에 현재가·등락률·거래량을 채우고(갭 1), RSI enrichment가 구조적으로 항상 실패하던 원인(`_LOOKBACK=10 < RSI14 최소 15종가`)을 수리한다(갭 2).

**Architecture:** ① 빌더의 `closes_window` 깊이를 10→30으로 늘려 RSI14 계산을 가능하게 한다(read-time enrichment `_rsi_by_symbol`은 심볼 기준 재조회라 코드 변경 불필요; 새 파티션 빌드 후부터 효력). ② flow 로더(`_load_investor_flow_discovery_from_snapshots`)에 같은 함수의 market_cap lookup 패턴을 그대로 미러링한 fail-open 가격 lookup(healthy `invest_screener_snapshots` 파티션)을 추가해 `close`/`change_rate`/`change_amount`/`volume` 키를 채운다 — 행 자격(qualification)은 절대 바꾸지 않는다(inner-join 금지, 가격 없으면 None). ③ double_buy 로더는 이미 가격을 join하지만 row 키가 `latest_close`라 포맷터(`row.get("close")`)와 불일치 → additive로 `close`/`change_amount` 키를 추가한다.

**Tech Stack:** Python 3.13 + SQLAlchemy async, pytest(`uv run pytest`), Ruff/ty. Migration 0 (스키마 변경 없음 — `closes_window`는 JSONB).

**검증된 근본 원인 (이슈 본문과 다른 점):**
- RSI 미작동은 join 누락이 아니다. `_rsi_by_symbol()`(`app/services/invest_view_model/screener_analysis_enrichment.py:149-183`)은 row와 무관하게 심볼로 `InvestScreenerSnapshot.closes_window`를 재조회해 RSI14를 계산하는데, 빌더 `_LOOKBACK = 10`(`app/services/invest_screener_snapshots/builder.py:17`)이 최대 10종가만 저장하고 `build_rsi14_from_closes`는 15종가 미만이면 None을 반환한다(같은 파일 :39-53 `len(clean) < 15`). 그래서 **모든 프리셋에서** rsiSucceeded=0이었다(이슈 실측: 3프리셋 × 41행 전부).
- 포맷터(`app/services/invest_view_model/screener_service.py:2336-2360`)가 읽는 키: priceLabel→`close|price|current_price`, changePctLabel→`change_rate`, changeAmountLabel→`change_amount`, volumeLabel→`volume`. flow 로더 row에는 4개 전부 없음; double_buy row에는 `change_rate`/`volume`은 있으나 `close`/`change_amount`가 없어 priceLabel만 "-"였다.

**스코프 제외 (PR1 아님):** KR 한글 카테고리(갭 3 — 매핑 소스 자체가 레포에 없어 별도 브레인스톰), flow 적재 lag(갭 4 — `investor_flow_schedule_enabled`/`investor_flow_snapshots_commit_enabled` default-off, operator 활성화 트랙), 커스텀 필터(ROB-439).

**Operator 후속 (PR 머지 후, 코드 외):** RSI는 새 스냅샷 빌드부터 효력 — `scripts/build_invest_screener_snapshots.py --market kr --all --commit` 재실행 전까지 기존 파티션(10종가)은 계속 RSI null. 정직한 동작이며 코드로 위장하지 않는다.

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/services/invest_screener_snapshots/builder.py` | 스냅샷 빌더 (`_LOOKBACK`, OHLCV fetch count) | Modify: `_LOOKBACK` 10→30 |
| `app/services/invest_view_model/screener_service.py` | flow 로더 `_load_investor_flow_discovery_from_snapshots` (:695-879) | Modify: 가격 lookup 블록 + row 키 4개 |
| `app/services/invest_view_model/double_buy_screener.py` | 쌍끌이 로더 | Modify: select에 `change_amount` 추가 + row에 `close`/`change_amount` 키 |
| `docs/runbooks/invest-screener-snapshots.md` | operator 런북 | Modify: §2에 RSI lookback 재빌드 노트 1줄 |
| `tests/test_invest_screener_snapshots_builder.py` | 빌더 테스트 | Modify: count=10 단언 갱신 + 신규 2 테스트 |
| `tests/test_invest_view_model_screener_service.py` | flow 로더 테스트 (`_FakeSession` 하니스) | Modify: 신규 2 테스트 + `_price_row` 헬퍼 |
| `tests/test_invest_view_model_double_buy_screener.py` | double_buy 테스트 (실 db_session) | Modify: 기존 테스트에 close 단언 추가 |

**테스트 하니스 주의사항 (구현자 필독):**
- `tests/test_invest_view_model_screener_service.py:57`의 autouse fixture `mock_screener_service_resolve_healthy`가 `resolve_healthy_partition`을 가로챈다. `_FakeSession`이면 **결과 리스트에서 1개를 pop**해 `scalar_one_or_none()`을 partition_date로 쓴다. 즉 `resolve_healthy_partition` 호출 1번 = `_FakeExecuteResult` 1개 소비. 시퀀스가 소진되면 `_FakeSession.execute`는 빈 결과를 반환한다(`tests/.../:144-148`) — 그래서 신규 lookup은 fail-open이면 기존 테스트를 깨지 않는다.
- flow 로더 내 `resolve_healthy_partition` 호출 순서(가격 블록을 market_cap 블록 **뒤에** 넣을 때): ① flow 파티션 ② market_cap(MarketValuationSnapshot) ③ 가격(InvestScreenerSnapshot). `_FakeSession` 시퀀스는 [①date, flow rows, name rows, ②date, mc rows, ③date, price rows] 순.
- double_buy 테스트는 공유 persistent test DB를 쓰므로 TRUNCATE 금지, 픽스처가 9-prefix 합성 심볼만 정리한다.

---

### Task 1: 빌더 `_LOOKBACK` 10→30 (RSI14 구조적 불가 해소)

**Files:**
- Modify: `app/services/invest_screener_snapshots/builder.py:17`
- Modify: `docs/runbooks/invest-screener-snapshots.md` (§2 끝)
- Test: `tests/test_invest_screener_snapshots_builder.py`

- [ ] **Step 1: 실패하는 테스트 2개 작성**

`tests/test_invest_screener_snapshots_builder.py` 끝에 추가:

```python
def test_lookback_supports_rsi14():
    """ROB-512: build_rsi14_from_closes는 최소 15종가가 필요하다. _LOOKBACK이
    그 밑이면 closes_window 기반 RSI enrichment가 전 심볼에서 구조적으로 None이
    된다(rsiSucceeded=0 회귀 가드)."""
    from app.services.invest_screener_snapshots import builder

    assert builder._LOOKBACK >= 15


@pytest.mark.asyncio
async def test_build_snapshot_stores_rsi_capable_closes_window(monkeypatch):
    """ROB-512: 30세션 OHLCV가 주어지면 closes_window에 15개 이상 저장되고,
    저장된 윈도우만으로 RSI14가 계산 가능해야 한다."""
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-01", periods=30),
            "close": [100.0 + i for i in range(30)],
            "volume": [1_000_000] * 30,
        }
    )
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    payload = await build_snapshot_for_symbol(
        market="kr", symbol="005930", today=dt.date(2026, 5, 9)
    )
    assert payload is not None
    assert len(payload.closes_window) >= 15

    from app.services.invest_view_model.screener_analysis_enrichment import (
        build_rsi14_from_closes,
    )

    assert build_rsi14_from_closes(payload.closes_window) is not None
```

(`pd`, `AsyncMock`, `dt`, `build_snapshot_for_symbol`은 이 파일 상단에 이미 import되어 있다 — 없으면 기존 import 블록에 합류.)

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-512 && uv run pytest tests/test_invest_screener_snapshots_builder.py -v -k "lookback_supports_rsi14 or rsi_capable"`
Expected: 2 FAIL — `assert 10 >= 15` / `assert 10 >= 15` (closes_window 길이 10).

- [ ] **Step 3: `_LOOKBACK` 변경**

`app/services/invest_screener_snapshots/builder.py:17`:

```python
# ROB-512: read-time RSI14 enrichment(build_rsi14_from_closes)는 최소 15종가가
# 필요하다. 10이던 시절 closes_window 기반 RSI는 전 심볼 None(rsiSucceeded=0)
# 이었다. 30 = 15 최소치 + EWM 안정화 여유(~6주 세션). 새 값은 다음 스냅샷
# 빌드부터 효력이며 기존 파티션은 재빌드 전까지 10종가 그대로다.
_LOOKBACK = 30
```

- [ ] **Step 4: 기존 count=10 단언 갱신**

`tests/test_invest_screener_snapshots_builder.py`의 `test_build_snapshot_for_symbol_kr` 마지막 줄(현재 `fetcher.assert_awaited_once_with("005930", "equity_kr", count=10)`)을 다음으로 교체:

```python
    fetcher.assert_awaited_once_with("005930", "equity_kr", count=30)
```

- [ ] **Step 5: 통과 확인 (파일 전체)**

Run: `uv run pytest tests/test_invest_screener_snapshots_builder.py -v`
Expected: 전부 PASS (derive_metrics 계열은 closes 인자를 직접 받으므로 영향 없음; `closes[-_LOOKBACK:]` 슬라이스는 입력이 30 미만이면 전량 저장이라 기존 10행 fixture도 동작 불변).

- [ ] **Step 6: 런북 노트**

`docs/runbooks/invest-screener-snapshots.md` §2 (Operator Workflow) 끝에 추가:

```markdown
> **ROB-512:** `_LOOKBACK`이 10→30으로 늘어 `closes_window` 기반 RSI14 enrichment가
> 가능해졌다. 효력은 **새 빌드 파티션부터** — 배포 후 `--market kr --all --commit`
> 재빌드 전까지 기존 파티션의 RSI는 계속 null(정상·정직한 동작)이다.
```

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_screener_snapshots/builder.py tests/test_invest_screener_snapshots_builder.py docs/runbooks/invest-screener-snapshots.md
git commit -m "fix(ROB-512): widen snapshot closes_window to 30 so RSI14 enrichment can succeed

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: flow 로더에 fail-open 가격 lookup (갭 1 본체)

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:781-862` 부근 (`_load_investor_flow_discovery_from_snapshots`)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_invest_view_model_screener_service.py`의 ROB-277 investor-flow 섹션(`test_investor_flow_rows_carry_snapshot_date_collected_at_and_classified_state` 뒤)에 추가. 파일 상단에 `from decimal import Decimal`이 이미 있는지 확인(없으면 추가).

```python
def _price_row(
    symbol: str,
    close: Any,
    change_rate: Any,
    change_amount: Any,
    volume: Any,
) -> Any:
    return type(
        "PriceRow",
        (),
        {
            "symbol": symbol,
            "latest_close": close,
            "change_rate": change_rate,
            "change_amount": change_amount,
            "daily_volume": volume,
        },
    )()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_investor_flow_rows_carry_price_fields_from_price_snapshot() -> None:
    """ROB-512 갭1: flow row에 close/change_rate/change_amount/volume이 healthy
    가격 파티션 lookup으로 채워져 priceLabel/changePctLabel/volumeLabel이 '-'가
    아니게 된다."""
    from app.services.invest_view_model.screener_service import (
        _load_investor_flow_discovery_from_snapshots,
    )

    snapshot_date = date(2026, 5, 15)
    session = _FakeSession(
        [
            # ① flow resolve_healthy_partition (autouse fixture가 pop)
            _FakeExecuteResult(scalar_rows=[snapshot_date]),
            # qualifying flow rows
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeInvestorFlowSnapshot(
                        symbol="005930", snapshot_date=snapshot_date
                    )
                ]
            ),
            # kr_symbol_universe names
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),
            # ② market_cap resolve (pop) + select
            _FakeExecuteResult(scalar_rows=[snapshot_date]),
            _FakeExecuteResult(rows=[]),
            # ③ price resolve (pop) + select  ← ROB-512 신규
            _FakeExecuteResult(scalar_rows=[snapshot_date]),
            _FakeExecuteResult(
                rows=[
                    _price_row(
                        "005930",
                        Decimal("80000"),
                        Decimal("1.27"),
                        Decimal("1000"),
                        1_234_567,
                    )
                ]
            ),
        ]
    )

    load_result = await _load_investor_flow_discovery_from_snapshots(
        session, market="kr", limit=20
    )

    assert load_result is not None
    assert len(load_result.rows) == 1
    row = load_result.rows[0]
    assert row["close"] == pytest.approx(80000.0)
    assert row["change_rate"] == pytest.approx(1.27)
    assert row["change_amount"] == pytest.approx(1000.0)
    assert row["volume"] == 1_234_567


@pytest.mark.unit
@pytest.mark.asyncio
async def test_investor_flow_price_lookup_fails_open_to_none_fields() -> None:
    """ROB-512: 가격 파티션이 없어도 행은 절대 탈락하지 않고 가격 키만 None
    (priceLabel '-' 유지 — 자격 변경 금지)."""
    from app.services.invest_view_model.screener_service import (
        _load_investor_flow_discovery_from_snapshots,
    )

    snapshot_date = date(2026, 5, 15)
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[snapshot_date]),
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeInvestorFlowSnapshot(
                        symbol="005930", snapshot_date=snapshot_date
                    )
                ]
            ),
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),
            # 이후 시퀀스 소진 → market_cap/price lookup 전부 빈 결과 (fail-open)
        ]
    )

    load_result = await _load_investor_flow_discovery_from_snapshots(
        session, market="kr", limit=20
    )

    assert load_result is not None
    assert len(load_result.rows) == 1
    row = load_result.rows[0]
    assert row["close"] is None
    assert row["change_rate"] is None
    assert row["change_amount"] is None
    assert row["volume"] is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v -k "investor_flow_rows_carry_price or investor_flow_price_lookup_fails_open"`
Expected: 2 FAIL — `KeyError: 'close'` (row dict에 키 없음).

- [ ] **Step 3: 로더에 가격 lookup 블록 추가**

`app/services/invest_view_model/screener_service.py`에서 market_cap 블록(`# ROB-426 PR3: market_cap via the healthy KR valuation partition.` :781-817) **바로 뒤**, `from app.services.invest_screener_snapshots.freshness import` (:819) **앞**에 삽입:

```python
    # ROB-512 갭1: 현재가/등락률/거래량 — healthy KR 가격 파티션에서 보조 표시
    # 필드를 가져온다(위 market_cap lookup 미러). fail-open: 가격 파티션이
    # 없거나 조회가 실패해도 수급 행 자격에는 영향이 없고 키만 None이다.
    price_map: dict[str, dict[str, Any]] = {}
    if candidate_snaps:
        from app.models.invest_screener_snapshot import InvestScreenerSnapshot

        price_hp = await resolve_healthy_partition(
            session,
            model=InvestScreenerSnapshot,
            date_col=InvestScreenerSnapshot.snapshot_date,
            market_col=InvestScreenerSnapshot.market,
            market="kr",
        )
        if price_hp is not None:
            try:
                _pq = await session.execute(
                    sa.select(
                        InvestScreenerSnapshot.symbol,
                        InvestScreenerSnapshot.latest_close,
                        InvestScreenerSnapshot.change_rate,
                        InvestScreenerSnapshot.change_amount,
                        InvestScreenerSnapshot.daily_volume,
                    ).where(
                        InvestScreenerSnapshot.market == "kr",
                        InvestScreenerSnapshot.snapshot_date
                        == price_hp.partition_date,
                        InvestScreenerSnapshot.symbol.in_(
                            [snap.symbol for snap in candidate_snaps]
                        ),
                    )
                )
                price_map = {
                    r.symbol: {
                        "close": (
                            float(r.latest_close)
                            if r.latest_close is not None
                            else None
                        ),
                        "change_rate": (
                            float(r.change_rate)
                            if r.change_rate is not None
                            else None
                        ),
                        "change_amount": (
                            float(r.change_amount)
                            if r.change_amount is not None
                            else None
                        ),
                        "volume": r.daily_volume,
                    }
                    for r in _pq.all()
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "investor_flow: price lookup failed: %s", exc, exc_info=True
                )
```

- [ ] **Step 4: row dict에 가격 키 4개 추가**

같은 함수의 rows.append 블록(:843-862)에서 `"name": symbol_names.get(snap.symbol),` 줄 바로 뒤에 추가 (루프 안에서 `price = price_map.get(snap.symbol) or {}`를 `rows.append(` 직전에 선언):

```python
        price = price_map.get(snap.symbol) or {}
        rows.append(
            {
                "symbol": snap.symbol,
                "market": "kr",
                "name": symbol_names.get(snap.symbol),
                "close": price.get("close"),
                "change_rate": price.get("change_rate"),
                "change_amount": price.get("change_amount"),
                "volume": price.get("volume"),
                "foreign_net": snap.foreign_net,
                ...  # 이하 기존 키 그대로 (foreign_net부터 _market_cap_source까지 무변경)
            }
        )
```

(`...`은 기존 코드 유지 표기 — 실제 편집은 기존 키들을 그대로 둔 채 4줄 + price 선언 1줄만 삽입한다.)

- [ ] **Step 5: 통과 확인 (신규 + 기존 회귀)**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: 전부 PASS. 특히 `test_investor_flow_rows_carry_snapshot_date_collected_at_and_classified_state`(3-result 시퀀스)는 신규 lookup이 빈 결과로 fail-open되어 그대로 PASS해야 한다 — 깨지면 자격/순서를 건드린 것이므로 구현을 수정한다.

- [ ] **Step 6: 실DB wiring 회귀**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py -v`
Expected: 전부 PASS (단언이 snapshot_date/state뿐이라 additive 키는 무해; 가격 파티션 부재 시 fail-open 확인).

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "fix(ROB-512): investor_flow rows carry close/change_rate/change_amount/volume from healthy price partition

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: double_buy row에 `close`/`change_amount` 키 (priceLabel 수선)

**Files:**
- Modify: `app/services/invest_view_model/double_buy_screener.py` (:97-148 select, :253-284 row dict)
- Test: `tests/test_invest_view_model_double_buy_screener.py`

- [ ] **Step 1: 기존 테스트에 실패하는 단언 추가**

`tests/test_invest_view_model_double_buy_screener.py`의 `test_returns_rows_filtered_by_double_buy_and_positive_change_rate` 끝(`assert target["_screener_snapshot_state"] in {"fresh", "stale"}` 뒤)에 추가:

```python
    # ROB-512: 포맷터(priceLabel)는 row["close"]를 읽는다 — latest_close만 있으면
    # 쌍끌이 결과의 현재가가 '-'로 렌더된다.
    assert target["close"] == pytest.approx(12000.0)
    assert target["change_amount"] == pytest.approx(2000.0)
    assert target["latest_close"] == pytest.approx(12000.0)  # 기존 소비자 호환 유지
```

그리고 같은 테스트의 시드에서 911000 `InvestScreenerSnapshot`에 `change_amount` 추가 (:129 `latest_close=decimal.Decimal("12000"),` 뒤):

```python
                change_amount=decimal.Decimal("2000"),
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_invest_view_model_double_buy_screener.py -v -k "test_returns_rows_filtered"`
Expected: FAIL — `KeyError: 'close'`.

- [ ] **Step 3: 로더 수정**

`app/services/invest_view_model/double_buy_screener.py` — candidate select(:106-109)에서 `InvestScreenerSnapshot.change_rate,` 뒤에 추가:

```python
            InvestScreenerSnapshot.change_amount,
```

row dict(:253-284)에서 `"latest_close": (...)` 항목 **앞**에 추가 (둘 다 유지 — `latest_close`는 기존 소비자 호환용, `close`는 포맷터 계약):

```python
                "close": (
                    float(r["latest_close"]) if r["latest_close"] is not None else None
                ),
                "change_amount": (
                    float(r["change_amount"])
                    if r["change_amount"] is not None
                    else None
                ),
```

- [ ] **Step 4: 통과 확인 (파일 전체)**

Run: `uv run pytest tests/test_invest_view_model_double_buy_screener.py -v`
Expected: 전부 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/double_buy_screener.py tests/test_invest_view_model_double_buy_screener.py
git commit -m "fix(ROB-512): double_buy rows expose close/change_amount keys the formatter reads

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: 풀 게이트 (lint/format/ty/관련 스위트) + PR

**Files:** 변경 없음 (검증 전용)

- [ ] **Step 1: 포맷/린트/타입**

Run (순서대로):
```bash
uv run ruff format app/ tests/
uv run ruff check app/ tests/
make lint
```
Expected: format 변경 0(또는 변경분 재커밋), check/lint clean. (교훈: CI lint는 app/+tests/ 둘 다 본다; ty는 `app/` 전체.)

- [ ] **Step 2: 관련 테스트 스위트 일괄**

Run:
```bash
uv run pytest tests/test_invest_screener_snapshots_builder.py \
  tests/test_invest_view_model_screener_service.py \
  tests/test_invest_view_model_double_buy_screener.py \
  tests/test_partition_health_loader_wiring.py \
  tests/services/test_screener_analysis_enrichment.py \
  tests/test_screener_snapshot_tool.py \
  tests/test_screener_service_investor_flow_chip.py \
  tests/test_invest_view_model_screener_presets.py -v
```
Expected: 전부 PASS. (교훈: 시그니처/row 계약을 바꾸는 PR은 소비자 디렉토리 전수 실행 — 위 목록이 flow/double_buy row의 알려진 소비 표면이다. 실DB 공유 테스트가 run-ordering으로 깨지면 단독 재실행으로 회귀 여부를 분리한다.)

- [ ] **Step 3: 포맷 재확인 후 잔여 변경 커밋 (있을 때만)**

```bash
git status --short
# ruff format이 바꾼 파일이 있으면:
git add -A && git commit -m "style(ROB-512): ruff format

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

- [ ] **Step 4: push + PR 생성**

```bash
git push -u origin rob-512
gh pr create --base main \
  --title "fix(ROB-512): flow 프리셋 가격 필드 + RSI lookback 수리 (PR1)" \
  --body "$(cat <<'EOF'
## Summary
- investor_flow_momentum 결과에 close/change_rate/change_amount/volume을 healthy 가격 파티션 lookup(fail-open)으로 채움 — priceLabel/changePctLabel/volumeLabel '-' 해소 (갭 1)
- 빌더 `_LOOKBACK` 10→30: RSI14는 최소 15종가 필요 — 기존엔 모든 프리셋에서 rsiSucceeded=0이 구조적으로 강제됐음 (갭 2 진짜 근본 원인; 이슈 본문의 'join 누락' 가설과 다름)
- double_buy row에 포맷터 계약 키 `close`/`change_amount` 추가 (latest_close 유지)

## Out of scope
KR 한글 카테고리(갭3), flow 적재 lag(갭4=operator 활성화 트랙), 커스텀 필터(ROB-439)

## Operator follow-up
RSI 효력은 **새 스냅샷 빌드부터** — 배포 후 `build_invest_screener_snapshots --market kr --all --commit` 재빌드 필요. Migration 0.

## Test plan
- [ ] Task1~3 신규/갱신 테스트 + 관련 8개 스위트 green
- [ ] make lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: CI green 확인**

Run: `gh pr checks --watch`
Expected: 전부 green. 적색이면 로그를 먼저 읽고(공유 DB run-ordering/xdist 무관 실패 패턴 구분) 원인별 수정.

---

## Self-Review

- **Spec coverage:** 갭1(flow 가격 3필드) → Task 2; 갭2(RSI) → Task 1(검증된 근본 원인 기준); double_buy priceLabel 부수 결함 → Task 3; operator 재빌드 정직성 → Task 1 Step 6 런북 + PR body. 갭3/4/5는 명시적 스코프 제외. ✓
- **Placeholder scan:** Task 2 Step 4의 `...`은 "기존 키 무변경 유지" 표기로 실제 편집 지시가 병기되어 있음 — 그 외 TBD/TODO 없음. ✓
- **Type consistency:** row 키는 전부 포맷터가 읽는 실제 키(`close`/`change_rate`/`change_amount`/`volume`)와 일치(:2336-2360 확인). `_price_row` 속성명은 select 컬럼명(`latest_close`/`daily_volume`)과 일치. `_FakeSession` pop 시퀀스는 autouse fixture(:57-100) 동작 기준으로 검증. ✓
