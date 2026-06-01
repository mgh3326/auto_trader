# ROB-389 stale 스냅샷 fresh-라벨 차단 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** stale screener/momentum 데이터가 `fresh`로 라벨되지 않게 하고, 경과일(days_stale)·baseline·리프레시 갭 진단을 정직하게 표면화한다.

**Architecture:** (Bug A) `momentum_candidates`의 `"fresh" if rows else "missing"` 동어반복을 `trading_date` vs `expected_kr_baseline_date` 비교로 교체. (Bug B) `candidate_universe`의 equity coverage baseline을 raw UTC `now.date()` → `expected_baseline_date(market)`로 세션 인식화. (Bug C) 두 콜렉터 페이로드에 `expected_baseline_date`/`latest_partition_date`/`days_stale` 노출. (Bug D) `fresh=0 & stale>0`일 때 구조화 진단 로그. crypto 경로는 24/7라 불변.

**Tech Stack:** Python 3.13, pytest (`uv run pytest`), 기존 `app/services/invest_screener_snapshots/freshness.py` + `momentum_candidates.py` + `candidate_universe.py` 콜렉터.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-389-stale-freshness-honesty-design.md`

---

## File Structure

- **Modify** `app/services/invest_screener_snapshots/freshness.py` — `classify_momentum_freshness` 순수 헬퍼 추가.
- **Modify** `app/mcp_server/tooling/momentum_candidates.py` — 동어반복 제거 + days_stale/baseline 노출.
- **Modify** `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` — equity coverage 세션 인식 + 진단 로그 + 페이로드 baseline/days_stale + stale 메시지 경과일.
- **Test** `tests/test_invest_screener_snapshots_freshness.py`, `tests/test_invest_momentum_events.py`, `tests/services/action_report/test_candidate_universe_collector_evidence.py` — 기존 파일에 추가.

> 실행 시 모든 명령은 worktree `/Users/mgh3326/work/auto_trader.rob-389`에서 `uv run` 으로 수행한다.

---

## Task 1: `classify_momentum_freshness` 순수 헬퍼 (Bug A 지원)

**Files:**
- Modify: `app/services/invest_screener_snapshots/freshness.py` (`classify_investor_flow_partition` 정의 뒤, 라인 ~342 이후)
- Test: `tests/test_invest_screener_snapshots_freshness.py`

- [ ] **Step 1: Write the failing test**

`tests/test_invest_screener_snapshots_freshness.py` 끝에 추가:

```python
def test_classify_momentum_freshness_fresh_when_latest_matches_baseline():
    from app.services.invest_screener_snapshots.freshness import (
        classify_momentum_freshness,
        expected_kr_baseline_date,
    )

    now = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)  # 18:00 KST Mon
    expected = expected_kr_baseline_date(now)
    state, days_stale = classify_momentum_freshness(
        latest_trading_date=expected, now=now
    )
    assert state == "fresh"
    assert days_stale == 0


def test_classify_momentum_freshness_stale_with_days_elapsed():
    from app.services.invest_screener_snapshots.freshness import (
        classify_momentum_freshness,
        expected_kr_baseline_date,
    )

    now = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    expected = expected_kr_baseline_date(now)
    old = expected - dt.timedelta(days=14)
    state, days_stale = classify_momentum_freshness(latest_trading_date=old, now=now)
    assert state == "stale"
    assert days_stale == 14
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_snapshots_freshness.py -k momentum_freshness -v`
Expected: FAIL with `ImportError: cannot import name 'classify_momentum_freshness'`.

- [ ] **Step 3: Implement the helper**

`app/services/invest_screener_snapshots/freshness.py`, `classify_investor_flow_partition` 함수 정의 직후에 추가:

```python
def classify_momentum_freshness(
    *, latest_trading_date: dt.date, now: dt.datetime
) -> tuple[DataState, int]:
    """Classify a KR momentum partition by its trading date.

    ``fresh`` when ``latest_trading_date`` is at or after the expected KR
    baseline date for ``now``; otherwise ``stale``. The second tuple element is
    ``days_stale`` — calendar days the partition lags the expected baseline
    (``0`` when fresh). Callers must handle the empty-rows -> ``"missing"`` case
    before calling this; this helper never returns ``"missing"``.
    """
    expected = expected_kr_baseline_date(now)
    if latest_trading_date >= expected:
        return "fresh", 0
    return "stale", (expected - latest_trading_date).days
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_screener_snapshots_freshness.py -k momentum_freshness -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_screener_snapshots/freshness.py tests/test_invest_screener_snapshots_freshness.py
git commit -m "feat(ROB-389): add classify_momentum_freshness trading-date helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: momentum_candidates 동어반복 제거 (Bug A, 주 버그)

**Files:**
- Modify: `app/mcp_server/tooling/momentum_candidates.py` (`get_momentum_candidates_impl`, 라인 ~43-77)
- Test: `tests/test_invest_momentum_events.py`

- [ ] **Step 1: Write the failing test**

`tests/test_invest_momentum_events.py` 끝에 추가:

```python
class TestMomentumDataStateHonesty:
    """A 2.5-week-old partition must NOT be labeled fresh (ROB-389 regression)."""

    async def test_stale_partition_is_labeled_stale_with_days(self, monkeypatch):
        import datetime as dt

        from app.mcp_server.tooling import momentum_candidates as mod
        from app.services.invest_momentum_events.repository import (
            MomentumCandidateSignal,
        )

        old_date = dt.date(2026, 5, 13)
        row = MomentumCandidateSignal(
            symbol="000050",
            name="가나다",
            score=1.0,
            latest_snapshot_at=dt.datetime(2026, 5, 13, 11, 0, tzinfo=dt.UTC),
            trading_date=old_date,
            price=Decimal("1000"),
            change_rate=Decimal("1.0"),
            surface_count=1,
            venue_count=1,
            rank_delta=None,
            signals=[],
            theme_names=[],
            reason_codes=[],
        )

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return [row]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        assert result["data_state"] == "stale"
        assert result["days_stale"] >= 1
        assert result["latest_trading_date"] == old_date.isoformat()
        assert "expected_baseline_date" in result

    async def test_empty_rows_is_missing(self, monkeypatch):
        from app.mcp_server.tooling import momentum_candidates as mod

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return []

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)
        assert result["data_state"] == "missing"
```

> `Decimal`은 파일 상단에 이미 import되어 있는지 확인하고 없으면 `from decimal import Decimal` 추가.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_momentum_events.py -k MomentumDataStateHonesty -v`
Expected: FAIL — `test_stale_partition...`가 `data_state == "fresh"`(현재 동어반복)로 실패, `days_stale` KeyError.

- [ ] **Step 3: Rewrite the data_state branch**

`app/mcp_server/tooling/momentum_candidates.py`:

상단 import에 추가:

```python
from app.services.invest_screener_snapshots.freshness import (
    classify_momentum_freshness,
    expected_kr_baseline_date,
)
```

`get_momentum_candidates_impl`의 `return {...}` 블록(라인 ~66-77)을 교체:

```python
    now = dt.datetime.now(dt.UTC)
    if rows:
        latest_trading_date = rows[0].trading_date
        data_state, days_stale = classify_momentum_freshness(
            latest_trading_date=latest_trading_date, now=now
        )
        empty_reason = None
    else:
        latest_trading_date = None
        data_state, days_stale = "missing", 0
        empty_reason = "no_naver_momentum_snapshots"

    return {
        "market": "kr",
        "data_state": data_state,
        "days_stale": days_stale,
        "expected_baseline_date": expected_kr_baseline_date(now).isoformat(),
        "latest_trading_date": (
            latest_trading_date.isoformat() if latest_trading_date else None
        ),
        "empty_reason": empty_reason,
        "items": [_candidate_to_dict(row) for row in rows],
        "scoring_notes": [
            "searchTop/quantTop/up/priceTop 동시 출현을 우대",
            "KRX+NXT 동시 출현과 테마 리더 편입을 보너스로 반영",
            "동일 surface의 직전 스냅샷 대비 순위 개선(rank_delta)을 반영",
            "read-only: 네이버/브로커 요청 없이 저장된 스냅샷만 조회",
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_momentum_events.py -k MomentumDataStateHonesty -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/momentum_candidates.py tests/test_invest_momentum_events.py
git commit -m "fix(ROB-389): classify momentum data_state by trading_date, not row presence

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: candidate_universe equity coverage 세션 인식 + 진단 로그 (Bug B + D)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` (imports; `_collect_top_gainers`, 라인 ~296-327)
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/test_candidate_universe_collector_evidence.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_equity_coverage_uses_session_aware_baseline(db_session, monkeypatch):
    import app.services.action_report.snapshot_backed.collectors.candidate_universe as cu
    from app.services.invest_screener_snapshots.freshness import expected_baseline_date

    fixed_now = dt.datetime(2026, 6, 1, 0, 30, tzinfo=dt.UTC)  # 09:30 KST Mon
    monkeypatch.setattr(cu, "utcnow", lambda: fixed_now)

    captured: dict = {}

    class _CapturingRepo(_FakeEquityRepository):
        async def coverage(self, *, market, today_trading_date):
            captured["baseline"] = today_trading_date
            return await super().coverage(
                market=market, today_trading_date=today_trading_date
            )

    repo = _CapturingRepo()
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope=None,
            symbols=[],
            candidate_limit=2,
            policy_snapshot={},
        )
    )

    # 09:30 KST is before the 16:20 preliminary, so baseline is the PRIOR weekday,
    # NOT raw UTC now.date() (2026-05-31, a Sunday).
    assert captured["baseline"] == expected_baseline_date("kr", now=fixed_now)
    assert captured["baseline"] != fixed_now.date()
```

> 이 테스트는 KR이 preset 경로(`_collect_kr_presets`)로 먼저 가는 것을 피하기 위해, preset 로더가 빈 결과를 반환하도록 monkeypatch가 필요할 수 있다. 기존 `test_kr_collector_falls_back_to_top_gainers_when_no_preset_rows`(라인 ~260)의 monkeypatch 패턴(세 loader를 빈 리스트 반환으로 설정)을 동일하게 적용한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k session_aware_baseline -v`
Expected: FAIL — 현재 `today_trading_date=now.date()`(2026-05-31)라 `captured["baseline"] == fixed_now.date()`가 되어 `!=` 단언 실패.

- [ ] **Step 3: Import baseline helper + logger**

`app/services/action_report/snapshot_backed/collectors/candidate_universe.py` 상단 import 블록에 추가:

```python
import logging
```

그리고 freshness import:

```python
from app.services.invest_screener_snapshots.freshness import expected_baseline_date
```

모듈 logger를 import 블록 뒤(상수 `TOP_N = 10` 근처)에 추가:

```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Make equity coverage session-aware + diagnostic log**

`_collect_top_gainers`(라인 ~296-304)의 coverage 호출과 그 직후를 교체:

```python
    async def _collect_top_gainers(
        self, request: CollectorRequest, now: dt.datetime, limit: int
    ) -> list[SnapshotCollectResult]:
        baseline = expected_baseline_date(request.market, now=now)
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=baseline
        )
        if coverage.fresh_count == 0 and coverage.stale_count > 0:
            logger.warning(
                "candidate_universe refresh gap: market=%s expected_baseline=%s "
                "latest_computed_at=%s stale_count=%d (snapshot build produced no "
                "partition for the expected baseline)",
                request.market,
                baseline.isoformat(),
                coverage.last_computed_at,
                coverage.stale_count,
            )
        usefulness = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
```

> `_collect_crypto`는 변경하지 않는다(24/7 시장, `coverage(today=now.date())` 유지).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k session_aware_baseline -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "fix(ROB-389): session-aware equity coverage baseline + refresh-gap diagnostic

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: candidate_universe 경과일/baseline 페이로드 노출 + stale 메시지 (Bug C)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` (`_missing_data` ~215-231; `_collect_top_gainers` ~296-327; `_build_candidate_result` ~441-510; `_build_preset_candidate_result` ~512-575)
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/test_candidate_universe_collector_evidence.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_stale_equity_payload_exposes_days_stale(db_session, monkeypatch):
    import app.services.action_report.snapshot_backed.collectors.candidate_universe as cu

    fixed_now = dt.datetime(2026, 6, 1, 11, 0, tzinfo=dt.UTC)  # 20:00 KST Mon
    monkeypatch.setattr(cu, "utcnow", lambda: fixed_now)

    class _StaleRepo(_FakeEquityRepository):
        async def coverage(self, *, market, today_trading_date):
            return CoverageCounts(
                market=market,
                today_trading_date=today_trading_date,
                fresh_count=0,
                stale_count=11638,
                last_computed_at=None,
            )

        async def list_top_candidates(self, *, market, limit=10):
            self.requested_limits.append(limit)
            return [
                InvestScreenerSnapshot(
                    market=market,
                    symbol="000050",
                    snapshot_date=dt.date(2026, 5, 13),
                    latest_close=Decimal("1000"),
                    change_rate=Decimal("1.0"),
                    source="kis",
                )
            ]

    repo = _StaleRepo()
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="us",
            account_scope=None,
            symbols=[],
            candidate_limit=5,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    assert payload["latest_partition_date"] == "2026-05-13"
    assert payload["days_stale"] >= 1
    assert "expected_baseline_date" in payload
    assert payload["usefulness"] == "stale_only"
    assert "일 지연" in payload["missing_data"]["what"]
```

> `market="us"`로 두어 KR preset 경로를 우회한다(US는 곧장 `_collect_top_gainers`). `InvestScreenerSnapshot` 생성 인자는 기존 모델 필드(`market`/`symbol`/`snapshot_date`/`latest_close`/`change_rate`/`source`)를 사용한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k days_stale -v`
Expected: FAIL — `payload["latest_partition_date"]` KeyError (페이로드에 아직 없음).

- [ ] **Step 3: Add days_stale to `_missing_data`**

`_missing_data`(라인 ~215-231) 시그니처와 stale 메시지를 교체:

```python
def _missing_data(
    market: str, usefulness: str, *, days_stale: int = 0
) -> dict[str, str] | None:
    if usefulness == "useful":
        return None
    market_ko = {"crypto": "암호화폐", "kr": "국내", "us": "미국"}.get(market, market)
    if usefulness == "stale_only":
        lag = f"{days_stale}일 지연, " if days_stale > 0 else ""
        return {
            "what": f"{market_ko} 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 ({lag}stale).",
            "why": "최신 모멘텀/거래대금 교차검증이 제한되어 신규 후보 판단 신뢰도가 낮아집니다.",
            "next": "스크리너 스냅샷 리프레시가 최신 거래일로 갱신되면 개선됩니다.",
            "confidence_impact": "cap 40",
        }
    return {
        "what": f"{market_ko} 스크리너 스냅샷이 비어 있습니다.",
        "why": "후보 유니버스를 평가할 수 없어 신규 매수 후보 판단 신뢰도가 제한됩니다.",
        "next": "스크리너 스냅샷 리프레시가 활성화되면 개선됩니다.",
        "confidence_impact": "cap 20",
    }
```

- [ ] **Step 4: Compute baseline/latest_partition_date/days_stale in `_collect_top_gainers` and thread into the builder**

`_collect_top_gainers`에서 (Task 3에서 추가한 `baseline` 계산 이후) rows 조회 직후 `latest_partition_date`/`days_stale`를 계산하고 `_build_candidate_result` 호출에 전달:

```python
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=limit
        )
        rows = _dedupe_rows(rows, key=lambda r: to_db_symbol(r.symbol))
        latest_partition_date = rows[0].snapshot_date if rows else None
        days_stale = (
            (baseline - latest_partition_date).days
            if latest_partition_date is not None and baseline > latest_partition_date
            else 0
        )
        evidence = build_candidate_evidence(
            market=request.market,
            preset="top_gainers",
            rows=[_equity_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market=request.market,
                preset="top_gainers",
                evidence=evidence,
                candidate_limit=limit,
                fresh_count=coverage.fresh_count,
                stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at,
                usefulness=usefulness,
                expected_baseline_date=baseline,
                latest_partition_date=latest_partition_date,
                days_stale=days_stale,
            )
        ]
```

- [ ] **Step 5: Add the new params to `_build_candidate_result` and the payload**

`_build_candidate_result`(라인 ~441-510) 시그니처에 세 인자를 추가(기본값으로 crypto 호출 호환):

```python
    def _build_candidate_result(
        self,
        *,
        request: CollectorRequest,
        now: dt.datetime,
        market: str,
        preset: str,
        evidence: list[CandidateEvidence],
        candidate_limit: int,
        fresh_count: int,
        stale_count: int,
        last_computed_at: dt.datetime | None,
        usefulness: str,
        expected_baseline_date: dt.date | None = None,
        latest_partition_date: dt.date | None = None,
        days_stale: int = 0,
    ) -> SnapshotCollectResult:
```

payload dict(라인 ~472-490)에 세 필드를 추가하고 `missing_data` 호출에 `days_stale`를 전달:

```python
            "expected_baseline_date": (
                expected_baseline_date.isoformat() if expected_baseline_date else None
            ),
            "latest_partition_date": (
                latest_partition_date.isoformat() if latest_partition_date else None
            ),
            "days_stale": days_stale,
            "usefulness": usefulness,
            "missing_data": _missing_data(market, usefulness, days_stale=days_stale),
```

> 기존 `"usefulness"`/`"missing_data"` 라인을 위 블록으로 대체(중복 키 금지). `_collect_crypto`는 새 인자를 넘기지 않으므로 기본값(None/None/0)이 적용되어 동작 불변.

- [ ] **Step 6: Expose baseline in the preset payload**

`_build_preset_candidate_result`(라인 ~512-575)의 payload에 `expected_baseline_date`(now 기준)와 `days_stale=0`를 추가. preset 경로는 단일 partition_date가 없으므로 `latest_partition_date=None`:

```python
        payload: dict[str, Any] = {
            "market": "kr",
            "preset": "toss_parity_multi",
            "as_of": now.isoformat(),
            "freshness_status": _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial"),
            "source_coverage": source_coverage,
            "candidate_limit": candidate_limit,
            "universe_count": universe_count,
            "capped": capped,
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": None,
            "expected_baseline_date": expected_baseline_date("kr", now=now).isoformat(),
            "latest_partition_date": None,
            "days_stale": 0,
            "usefulness": usefulness,
            "missing_data": _missing_data("kr", usefulness),
        }
```

> 주의: 지역 변수 충돌. `_build_preset_candidate_result` 안에서는 `expected_baseline_date`가 import된 **함수**를 가리켜야 한다(`_build_candidate_result`의 동명 파라미터와 달리 여기엔 동명 파라미터가 없으므로 함수 호출이 그대로 동작). 함수 import가 이름에 살아있는지 확인한다.

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k days_stale -v`
Expected: PASS (1 passed).

- [ ] **Step 8: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-389): expose expected_baseline/latest_partition/days_stale in candidate_universe

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 전체 검증 + lint + PR + 핸드오프

**Files:** 없음 (검증 전용)

- [ ] **Step 1: Run the touched suites**

Run:
```bash
uv run pytest tests/test_invest_screener_snapshots_freshness.py tests/test_invest_momentum_events.py tests/services/action_report/test_candidate_universe_collector_evidence.py -v
```
Expected: 모두 PASS (신규 + 기존 회귀 없음).

> `test_invest_momentum_events.py`/candidate_universe 테스트 일부는 `db_session` 픽스처(Postgres)를 사용한다. 로컬 DB가 없으면 해당 케이스는 환경 의존이며 CI에서 권위 있게 검증된다. 신규 추가 케이스 중 `MomentumDataStateHonesty`/`session_aware_baseline`/`days_stale`는 fake/monkeypatch 기반이라 DB 불필요.

- [ ] **Step 2: Lint (CLAUDE.md 게이트)**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 둘 다 통과. (format 위반이면 `uv run ruff format app/ tests/` 후 재확인·커밋.)

- [ ] **Step 3: Push branch and open PR (base: main)**

Run:
```bash
git push -u origin rob-389
gh pr create --base main --title "fix(ROB-389): stale 스냅샷이 fresh로 라벨되는 문제 차단" --body "$(cat <<'EOF'
## 요약
ROB-389: stale screener/momentum 데이터가 `fresh`로 보이지 않게 하고 경과일·진단을 정직하게 표면화.

1. **Bug A (주 버그)** — `get_momentum_candidates`의 `"fresh" if rows else "missing"` 동어반복 제거. `trading_date` vs `expected_kr_baseline_date` 비교로 `fresh`/`stale`/`missing` 분류 + `days_stale`/`expected_baseline_date`/`latest_trading_date` 노출.
2. **Bug B** — `candidate_universe` equity coverage baseline을 raw UTC `now.date()` → `expected_baseline_date(market)` 세션 인식화 (crypto는 24/7라 불변).
3. **Bug C** — candidate_universe 페이로드에 `expected_baseline_date`/`latest_partition_date`/`days_stale` 노출 + stale 메시지에 `N일 지연`.
4. **Bug D** — `fresh=0 & stale>0`일 때 구조화 refresh-gap 진단 `logger.warning`.

## 테스트
- `tests/test_invest_screener_snapshots_freshness.py` — `classify_momentum_freshness`
- `tests/test_invest_momentum_events.py` — 2.5주 전 partition → `stale`(+days_stale), 빈 행 → `missing`
- `tests/services/action_report/test_candidate_universe_collector_evidence.py` — 세션 인식 baseline + days_stale 페이로드

## 안전 경계
read-only. broker/order/watch mutation 없음. **DB 마이그레이션 없음**(페이로드 JSON만 추가). scheduler/Prefect 활성화·prod backfill 없음.

## 잔여 (handoff)
- "11,638 stale / 0 fresh"의 근본 운영 원인(스냅샷 빌드 잡이 최신 거래일 partition을 생성하지 못함)은 **운영/데이터 영역** — 별도 operator issue. 본 PR은 정직한 라벨링 + 진단 로그만 추가하고 scheduler/trigger/backfill은 넣지 않음.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL 출력. (출력된 URL 확인 후에만 PR 번호 인용.)

- [ ] **Step 4: ROB-394 handoff 코멘트 작성**

ROB-394에 ROB-389 결과(PR 링크 + 검증 결과 + 잔여: 스냅샷 빌드 잡 운영 활성화는 별도 operator issue)를 남기고, 다음 순서가 ROB-390임을 명시한다. (Linear `save_comment`.)

---

## Self-Review

**Spec coverage:**
- Bug A momentum 동어반복 → Task 1(helper) + Task 2(impl). ✅
- Bug B coverage 세션 인식 → Task 3. ✅
- Bug C 경과일/baseline 페이로드 → Task 4. ✅
- Bug D 진단 로그 → Task 3. ✅
- 테스트 T1/T2/T3/T4 → Task 1/Task 2/Task 3/Task 4. ✅
- 안전 경계(read-only, no migration, crypto 불변) → 코드 변경에 mutation/마이그레이션 없음, `_collect_crypto` 미변경. ✅
- 비목표(빌드 잡 운영, US/crypto 의미론, cap 로직) → Task 5 handoff에 명시, 미변경. ✅

**Placeholder scan:** 모든 step에 실제 코드/명령. "적절한 처리" 류 없음. ✅

**Type consistency:** `classify_momentum_freshness(*, latest_trading_date, now) -> tuple[DataState, int]`(Task1 정의 ↔ Task2 사용 ↔ T1 단언); `_missing_data(market, usefulness, *, days_stale=0)`(Task4 정의 ↔ 호출); `_build_candidate_result(..., expected_baseline_date=None, latest_partition_date=None, days_stale=0)`(Task4 정의 ↔ Task4 Step4 호출 ↔ crypto 기본값); payload 키 `expected_baseline_date`/`latest_partition_date`/`days_stale`(Task4 ↔ T4 단언 ↔ Task2 momentum 동일 명명). ✅

**주의 (구현 시):** Task 4 Step 6에서 `_build_candidate_result`는 동명 **파라미터** `expected_baseline_date`를 갖지만 `_build_preset_candidate_result`/`_collect_*`는 import된 **함수** `expected_baseline_date`를 호출한다. 파라미터 섀도잉이 함수 스코프를 넘지 않으므로 안전하나, 구현자는 `_build_candidate_result` 본문 안에서 함수가 아닌 파라미터를 참조함을 인지한다(본문에서 함수 재호출 불필요).
