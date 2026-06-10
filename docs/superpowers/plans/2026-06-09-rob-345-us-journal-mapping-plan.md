# ROB-345 — US kis_live journal 매핑 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US kis_live `/invest/reports` 의 journal stage가 active US KIS journal을 정확히 반영하고(payload 키 계약 정렬), KR/타 market journal과 섞이지 않으며(instrument_type scoping), "journal 없음" 과 "collector unavailable" 을 구분된 data_gap으로 남긴다.

**Architecture:** 세 read-only/additive 레이어 — (1) stage가 collector 실제 키 `active` 를 읽도록 정렬 + 잘못된 테스트 fixture 정정, (2) collector가 `get_trade_journal` 의 검증된 `market_map` 필터(instrument_type) + account scope를 미러하고 `account` provenance를 emit, (3) collector_status로 empty vs unavailable 구분. migration-0, broker/order mutation 없음.

**Tech Stack:** Python 3.13, async SQLAlchemy, pytest(-asyncio), FastAPI/MCP. 스펙: `docs/superpowers/specs/2026-06-09-rob-345-us-kis-live-journal-mapping-design.md`.

---

## File Structure

- Modify: `app/services/investment_stages/stages/portfolio_journal.py` — stage가 `active` 키를 읽고, collector_status로 unavailable 판정.
- Modify: `app/services/action_report/snapshot_backed/collectors/journal.py` — market(instrument_type) + kis account scoping, `account` emit, collector_status + unavailable 경로.
- Test: `tests/services/investment_stages/stages/test_portfolio_journal.py` — 잘못된 `entries` fixture를 실제 `active` shape로 정정 + empty/unavailable 케이스.
- Test (new): `tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py` — DB-seeded scoping + provenance + unavailable.

> 작업 디렉토리: worktree `/Users/mgh3326/work/auto_trader.rob-345` (branch `rob-345`). 시작 전 `git fetch --prune origin && git switch -c rob-345-impl origin/main` 권장(스펙 커밋 cherry-pick 또는 main 머지 후 진행). 본 플랜은 현재 worktree 기준.

---

## Task 1: stage가 collector 실제 키(`active`)를 읽도록 정렬 + fixture 정정

**Files:**
- Modify: `app/services/investment_stages/stages/portfolio_journal.py:117-118`
- Test: `tests/services/investment_stages/stages/test_portfolio_journal.py:40,287`

- [ ] **Step 1: 실제 collector shape를 쓰는 실패 테스트 추가**

`tests/services/investment_stages/stages/test_portfolio_journal.py` 에 추가:

```python
@pytest.mark.asyncio
async def test_portfolio_journal_reads_active_from_real_collector_shape():
    # ROB-345 회귀: collector는 active/recent_retrospective 를 emit한다.
    # stage가 "entries"를 읽던 버그(=항상 open journal: none)를 방지한다.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap("portfolio", {"buying_power_krw": 200000, "nav_krw": 1000000})
            ],
            "journal": [
                _snap(
                    "journal",
                    {
                        "active": [{"symbol": "AAPL", "thesis": "earnings"}],
                        "recent_retrospective": [],
                        "active_count": 1,
                        "retrospective_count": 0,
                        "collector_status": "ok",
                    },
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "AAPL" in (payload.summary or "")
    assert "journal" not in (payload.missing_data or [])
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py::test_portfolio_journal_reads_active_from_real_collector_shape -v`
Expected: FAIL — `"AAPL"` not in summary (stage가 `entries` 를 읽어 `open journal: none`).

- [ ] **Step 3: stage가 `active` 를 읽도록 수정**

`portfolio_journal.py` 의 현재:
```python
        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("entries", []))
        symbols = ", ".join(e.get("symbol", "?") for e in entries[:5])
```
를 다음으로 교체:
```python
        # ROB-345 — collector emits "active" (draft/active journals), not
        # "entries". Reading the wrong key made every market show
        # "open journal: none". Mirror the real journal payload contract.
        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("active", []))
        symbols = ", ".join(e.get("symbol", "?") for e in entries[:5])
```

- [ ] **Step 4: 기존 잘못된 fixture 2곳 정정**

`test_portfolio_journal.py:40` 및 `:287` 의
`_snap("journal", {"entries": [{"symbol": "035420", "thesis": "tech"}]})` /
`_snap("journal", {"entries": [{"symbol": "005930", "thesis": "tech"}]})` 를 각각
실제 shape로 교체:
```python
                _snap(
                    "journal",
                    {"active": [{"symbol": "035420", "thesis": "tech"}],
                     "recent_retrospective": [], "active_count": 1,
                     "retrospective_count": 0, "collector_status": "ok"},
                )
```
(두 번째는 symbol `"005930"`.)

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -v`
Expected: PASS (신규 + 정정된 기존 테스트 모두).

- [ ] **Step 6: 커밋**

```bash
git add app/services/investment_stages/stages/portfolio_journal.py \
        tests/services/investment_stages/stages/test_portfolio_journal.py
git commit -m "fix(ROB-345): journal stage reads collector 'active' key (not 'entries')"
```

---

## Task 2: collector market(instrument_type) + kis account scoping + `account` provenance

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/journal.py`
- Test (new): `tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py`

- [ ] **Step 1: DB-seeded scoping 실패 테스트 추가**

`tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py` 생성:

```python
import datetime as dt

import pytest

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _journal(symbol, instrument_type, *, account="kis", account_type="live",
             status="active"):
    return TradeJournal(
        symbol=symbol, instrument_type=instrument_type, side="buy",
        status=status, account_type=account_type, account=account,
        entry_price=1.0, quantity=1.0, thesis="t",
    )


def _req(market):
    return CollectorRequest(market=market, account_scope="kis_live", symbols=None)


@pytest.mark.asyncio
async def test_us_scope_excludes_kr_live_journals(db_session):
    db_session.add_all([
        _journal("AAPL", InstrumentType.equity_us),
        _journal("005930", InstrumentType.equity_kr),
    ])
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    active_syms = {e["symbol"] for e in payload["active"]}
    assert active_syms == {"AAPL"}
    assert payload["active"][0]["account"] == "kis"  # provenance emitted
    assert payload["collector_status"] == "ok"


@pytest.mark.asyncio
async def test_kr_scope_excludes_us_live_journals(db_session):
    db_session.add_all([
        _journal("AAPL", InstrumentType.equity_us),
        _journal("005930", InstrumentType.equity_kr),
    ])
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("kr"))
    active_syms = {e["symbol"] for e in results[0].payload_json["active"]}
    assert active_syms == {"005930"}


@pytest.mark.asyncio
async def test_us_scope_includes_legacy_null_account_kis_us(db_session):
    db_session.add(_journal("MSFT", InstrumentType.equity_us, account=None))
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    active_syms = {e["symbol"] for e in results[0].payload_json["active"]}
    assert "MSFT" in active_syms
```

> `db_session` fixture: 기존 collector/DB 테스트에서 쓰는 async session fixture를 재사용한다. 동일 패턴이 `tests/` 에 존재하는지 확인하고(예: `tests/conftest.py` 의 `db_session`), 테이블이 `create_all` 로 준비되는지 확인. (ROB-407 메모: timescaledb 확장으로 alembic 차단 시 `db_session` dep가 `create_all` 을 보장.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py -v`
Expected: FAIL — 현재 collector는 market 필터가 없어 US scope에 005930 포함 + `account` 키 부재(KeyError).

- [ ] **Step 3: collector에 scoping + provenance 구현**

`journal.py` 상단 import에 추가:
```python
from sqlalchemy import desc, or_, select
from app.models.trading import InstrumentType
```
모듈 상수 추가(`_DEFAULT_RECENT_LIMIT` 근처):
```python
# Mirror get_trade_journal market_map (app/mcp_server/tooling/trade_journal_tools.py)
# — the only no-migration market discriminator on trade_journals.
_MARKET_TO_INSTRUMENT: dict[str, InstrumentType] = {
    "crypto": InstrumentType.crypto,
    "kr": InstrumentType.equity_kr,
    "us": InstrumentType.equity_us,
}
```
`collect()` 의 쿼리 빌드 직전 scope 필터 구성:
```python
        scope_filters = [TradeJournal.account_type == "live"]
        itype = _MARKET_TO_INSTRUMENT.get(request.market)
        if itype is not None:
            scope_filters.append(TradeJournal.instrument_type == itype)
        if request.account_scope == "kis_live":
            # KIS broker scope; legacy rows may carry account=NULL (kis_live_ledger
            # now writes account="kis"). instrument_type already excludes crypto.
            scope_filters.append(
                or_(TradeJournal.account == "kis", TradeJournal.account.is_(None))
            )
```
`active_stmt` / `recent_stmt` 의 `.where(...)` 를 scope_filters 사용으로 교체:
```python
        active_stmt = (
            select(TradeJournal)
            .where(*scope_filters, TradeJournal.status.in_(_ACTIVE_STATUSES))
            .order_by(desc(TradeJournal.updated_at))
        )
        recent_stmt = (
            select(TradeJournal)
            .where(*scope_filters, TradeJournal.status.in_(_RETROSPECTIVE_STATUSES))
            .order_by(desc(TradeJournal.updated_at))
            .limit(self._recent_limit)
        )
```
`_journal_to_dict` 에 `account` 추가(provenance):
```python
        "account_type": j.account_type,
        "account": j.account,
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/action_report/snapshot_backed/collectors/journal.py \
        tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py
git commit -m "fix(ROB-345): scope journal collector by market+kis account, emit account provenance"
```

---

## Task 3: empty vs collector-unavailable 구분 (collector_status + stage data_gap)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/journal.py` (collect)
- Modify: `app/services/investment_stages/stages/portfolio_journal.py` (run)
- Test: 위 두 테스트 파일

- [ ] **Step 1: collector unavailable + empty 테스트 추가**

`test_journal_collector.py` 에 추가:
```python
@pytest.mark.asyncio
async def test_empty_active_reports_ok_status(db_session):
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    assert payload["active"] == []
    assert payload["collector_status"] == "ok"


@pytest.mark.asyncio
async def test_query_failure_reports_unavailable(monkeypatch, db_session):
    collector = JournalSnapshotCollector(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "execute", _boom)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    assert payload["collector_status"] == "unavailable"
    assert results[0].freshness_status == "unavailable"
```

`test_portfolio_journal.py` 에 추가:
```python
@pytest.mark.asyncio
async def test_journal_unavailable_marks_data_gap():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [_snap("portfolio", {"buying_power_krw": 1, "nav_krw": 10})],
            "journal": [_snap("journal", {"active": [], "recent_retrospective": [],
                                          "collector_status": "unavailable"})],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "journal" in (payload.missing_data or [])


@pytest.mark.asyncio
async def test_journal_empty_ok_is_not_data_gap():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [_snap("portfolio", {"buying_power_krw": 1, "nav_krw": 10})],
            "journal": [_snap("journal", {"active": [], "recent_retrospective": [],
                                          "collector_status": "ok"})],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "journal" not in (payload.missing_data or [])
    assert "open journal: none" in (payload.summary or "")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py tests/services/investment_stages/stages/test_portfolio_journal.py -v`
Expected: FAIL — collector_status 부재 + stage가 unavailable을 구분하지 않음.

- [ ] **Step 3: collector — collector_status + unavailable 경로 구현**

`collect()` 의 실행부를 try/except로 감싸고 status를 payload에 추가:
```python
        try:
            active_rows = (await self._session.execute(active_stmt)).scalars().all()
            recent_rows = (await self._session.execute(recent_stmt)).scalars().all()
        except Exception:  # defensive — surface as collector unavailable, never crash
            logger.exception("journal collector query failed")
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={
                        "active": [], "recent_retrospective": [],
                        "active_count": 0, "retrospective_count": 0,
                        "recent_limit": self._recent_limit,
                        "collector_status": "unavailable",
                        "error": "journal_query_failed",
                    },
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    errors={"reason": "journal_query_failed"},
                )
            ]
```
정상 payload에 `"collector_status": "ok"` 추가:
```python
        payload: dict[str, Any] = {
            "active": [_journal_to_dict(j) for j in active_rows],
            "recent_retrospective": [_journal_to_dict(j) for j in recent_rows],
            "active_count": len(active_rows),
            "retrospective_count": len(recent_rows),
            "recent_limit": self._recent_limit,
            "collector_status": "ok",
        }
```
파일 상단에 `import logging` + `logger = logging.getLogger(__name__)` 가 없으면 추가.

- [ ] **Step 4: stage — unavailable 판정 구현**

`portfolio_journal.py` 의 `journal_snaps = context.snapshots_for("journal")` 직후/관련부에 헬퍼 + 판정 추가. 현재
`missing_data = [] if journal_snaps else ["journal"]` 를 교체:
```python
        def _journal_collector_status(snap: Any) -> str | None:
            return (getattr(snap, "payload_json", None) or {}).get("collector_status")

        journal_unavailable = (not journal_snaps) or any(
            _journal_collector_status(s) == "unavailable" for s in journal_snaps
        )
        missing_data = ["journal"] if journal_unavailable else []
```
(나머지 `missing_data` 누적 로직은 그대로. summary는 Task 1에서 `active` 기반으로 이미
"open journal: none" 정상 출력.)

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py tests/services/investment_stages/stages/test_portfolio_journal.py -v`
Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
git add app/services/action_report/snapshot_backed/collectors/journal.py \
        app/services/investment_stages/stages/portfolio_journal.py \
        tests/services/action_report/snapshot_backed/collectors/test_journal_collector.py \
        tests/services/investment_stages/stages/test_portfolio_journal.py
git commit -m "feat(ROB-345): distinguish empty journals from collector-unavailable (data_gap)"
```

---

## Task 4: lint / typecheck / 전체 게이트

- [ ] **Step 1: lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean. (CI는 app/ + tests/ 둘 다 검사 — 메모리 교훈.)

- [ ] **Step 2: typecheck**

Run: `uv run ty check app/`
Expected: no new errors.

- [ ] **Step 3: 관련 스위트 회귀**

Run: `uv run pytest tests/services/investment_stages tests/services/action_report/snapshot_backed -v`
Expected: PASS (KR 경로 포함 회귀 없음).

- [ ] **Step 4: 필요 시 style 커밋**

```bash
git add -A && git commit -m "style(ROB-345): ruff check and formatting"
```

---

## Self-review (작성자 체크)
- 스펙 §4.1/4.2/4.3 → Task 1/2/3 매핑됨. AC 4건 전부 테스트 보유.
- placeholder 없음(모든 코드/명령 구체).
- 타입 일관: `collector_status`("ok"/"unavailable"), payload 키 `active`, `_MARKET_TO_INSTRUMENT` 값 = get_trade_journal market_map.
- 안전: read-only collector, no broker/order mutation, KR 회귀 테스트 포함, migration 없음.
- 미수정 경계: dead `us/` 모듈, `trade_journal_read_service`(범위 외).
