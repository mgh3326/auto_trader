# ROB-404 — execution event consumer → 즉시 mock reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redis `execution:{market}` 채널의 kis_mock 체결 이벤트를 소비해 해당 symbol을 즉시 reconcile하는 default-off consumer + paused taskiq 주기 reconcile 폴백을 추가한다 (correlation_id 멱등, ROB-400 attribution 커널 재사용).

**Architecture:** consumer는 `execution:*`를 psubscribe → kis_mock-fill 하드 필터 → correlation_id SETNX 디듀프 → gate에 따라 `run_kis_mock_reconciliation(symbol=...)`(ROB-400 커널 경유)을 dry-run/실반영 호출. 메시지 처리는 순수 `handle_message(raw)`로 분리해 실 pubsub 없이 단위테스트. 주기 태스크는 schedule 없는(paused) taskiq task로 등록하고 env flag로 게이트.

**Tech Stack:** Python 3.13, redis.asyncio pub/sub, taskiq, SQLAlchemy async, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-404-execution-event-consumer-design.md`

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 플래그 | `KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED`/`..._PERIODIC_ENABLED` (default False) |
| `app/jobs/kis_mock_reconciliation_job.py` | reconcile 진입점 | `symbol` 인자 추가 → `list_open_orders(symbol=...)` |
| `app/services/kis_mock_execution_consumer.py` | consumer | psubscribe loop + `handle_message`(필터/디듀프/gate/reconcile). 신규 |
| `scripts/kis_mock_execution_consumer.py` | operator CLI | preflight/run 진입점. 신규 |
| `app/tasks/kis_mock_reconciliation_tasks.py` | 주기 태스크 | paused taskiq task, env-gated. 신규 |
| `tests/test_kis_mock_execution_consumer.py` | consumer 단위 | 신규 |
| `tests/jobs/test_kis_mock_reconciliation_job.py` | job symbol 필터 | 추가 |
| `tests/test_kis_mock_reconciliation_periodic_task.py` | 주기 태스크 | 신규 |

---

## Task 1: config 플래그 + job symbol 필터

**Files:**
- Modify: `app/core/config.py:460`
- Modify: `app/jobs/kis_mock_reconciliation_job.py:75-86`
- Test: `tests/jobs/test_kis_mock_reconciliation_job.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/jobs/test_kis_mock_reconciliation_job.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_run_passes_symbol_to_list_open_orders(db_session, monkeypatch):
    from app.services.kis_mock_lifecycle_service import KISMockLifecycleService

    captured: dict = {}

    async def _fake_list_open_orders(self, *, limit=100, symbol=None, **kw):
        captured["symbol"] = symbol
        captured["limit"] = limit
        return []  # empty → run short-circuits before broker/holdings

    monkeypatch.setattr(
        KISMockLifecycleService, "list_open_orders", _fake_list_open_orders
    )
    result = await run_kis_mock_reconciliation(
        db_session, symbol="005930", dry_run=True
    )
    assert captured["symbol"] == "005930"
    assert result["orders_processed"] == 0


def test_reconcile_gate_flags_default_false():
    from app.core.config import settings

    assert settings.KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED is False
    assert settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED is False
```

> `run_kis_mock_reconciliation` import가 파일 상단에 없으면 추가: `from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation`.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/jobs/test_kis_mock_reconciliation_job.py -k "symbol or gate_flags" -v`
Expected: FAIL — `run_kis_mock_reconciliation() got an unexpected keyword argument 'symbol'` + `AttributeError` (config 플래그 없음).

- [ ] **Step 3-a: config 플래그 추가**

`app/core/config.py` — `EXECUTION_LEDGER_COMMIT_ENABLED`(460행) 다음에 추가:

```python
    # ROB-404 — kis_mock execution-event consumer + periodic reconcile.
    # Default off: the consumer runs reconcile in dry-run preflight and the
    # periodic taskiq task returns paused until an operator flips these.
    KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED: bool = False
    KIS_MOCK_RECONCILE_PERIODIC_ENABLED: bool = False
```

- [ ] **Step 3-b: job symbol 인자 추가**

`app/jobs/kis_mock_reconciliation_job.py` — 시그니처(75–82행)에 `symbol` 추가하고 `list_open_orders` 호출(86행)에 전달:

```python
async def run_kis_mock_reconciliation(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 100,
    symbol: str | None = None,
    thresholds: ReconcilerThresholds | None = None,
    kis_client: KISClient | None = None,
) -> dict[str, Any]:
    """Fetch open mock orders, fetch mock holdings, propose & optionally apply transitions.

    ``symbol`` (ROB-404) restricts reconciliation to one symbol's open orders —
    the delta-budget kernel groups by (symbol, side) so a single-symbol pass is
    self-consistent. ``None`` keeps the full-batch behavior.
    """
    thresholds = thresholds or ReconcilerThresholds()
    lifecycle_svc = KISMockLifecycleService(db)
    open_rows = await lifecycle_svc.list_open_orders(limit=limit, symbol=symbol)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/jobs/test_kis_mock_reconciliation_job.py -v`
Expected: PASS (신규 + 기존 job 테스트).

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/jobs/kis_mock_reconciliation_job.py tests/jobs/test_kis_mock_reconciliation_job.py
git commit -m "feat(ROB-404): reconcile gate flags + per-symbol reconcile filter"
```

---

## Task 2: Consumer 메시지 처리 (`handle_message`)

**Files:**
- Create: `app/services/kis_mock_execution_consumer.py`
- Test: `tests/test_kis_mock_execution_consumer.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_execution_consumer.py` 생성:

```python
"""ROB-404 — kis_mock execution-event consumer message handling."""

from __future__ import annotations

import json

import pytest

from app.services.kis_mock_execution_consumer import KISMockExecutionConsumer


class _FakeRedis:
    """Minimal SETNX-style fake: set(nx=True) returns True once per key."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _session_factory():
    return _FakeSession()


def _make_consumer(force_dry_run=False):
    calls: list[dict] = []

    async def _fake_reconcile(db, *, symbol=None, dry_run=True, **kw):
        calls.append({"symbol": symbol, "dry_run": dry_run})
        return {"success": True, "orders_processed": 0}

    consumer = KISMockExecutionConsumer(
        redis_client=_FakeRedis(),
        reconcile_fn=_fake_reconcile,
        session_factory=_session_factory,
        force_dry_run=force_dry_run,
    )
    return consumer, calls


def _fill_event(**over):
    event = {
        "account_mode": "kis_mock",
        "broker": "kis",
        "fill_yn": "Y",
        "execution_type": "1",
        "symbol": "005930",
        "correlation_id": "corr-1",
    }
    event.update(over)
    return json.dumps(event)


@pytest.mark.asyncio
async def test_kis_mock_fill_triggers_reconcile_for_symbol(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled"
    assert calls == [{"symbol": "005930", "dry_run": False}]


@pytest.mark.asyncio
async def test_gate_off_runs_dry_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        False,
        raising=False,
    )
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled_dry_run"
    assert calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_live_event_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event(account_mode="kis_live"))
    assert outcome == "ignored_non_mock_fill"
    assert calls == []


@pytest.mark.asyncio
async def test_non_fill_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event(fill_yn="N", execution_type="0"))
    assert outcome == "ignored_non_mock_fill"
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_correlation_id_skipped(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer()
    first = await consumer.handle_message(_fill_event())
    second = await consumer.handle_message(_fill_event())
    assert first == "reconciled"
    assert second == "skipped_dedup"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_correlation_id_skipped(monkeypatch):
    consumer, calls = _make_consumer()
    event = json.loads(_fill_event())
    event.pop("correlation_id")
    outcome = await consumer.handle_message(json.dumps(event))
    assert outcome == "ignored_no_correlation_id"
    assert calls == []


@pytest.mark.asyncio
async def test_unparseable_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    assert await consumer.handle_message("not json") == "ignored_unparseable"
    assert calls == []


@pytest.mark.asyncio
async def test_preflight_force_dry_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer(force_dry_run=True)
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled_dry_run"
    assert calls[0]["dry_run"] is True
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_execution_consumer.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.kis_mock_execution_consumer`.

- [ ] **Step 3: 구현**

`app/services/kis_mock_execution_consumer.py` 생성:

```python
"""ROB-404 — Redis execution-event consumer for kis_mock fills.

Subscribes to ``execution:*``, hard-filters to kis_mock fills, dedupes by
correlation_id (Redis SETNX), and runs ``run_kis_mock_reconciliation`` for the
affected symbol (ROB-400 delta-budget kernel — no new fill matching). Default
off: reconcile runs dry-run unless ``KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.services.execution_event import _get_redis_client

logger = logging.getLogger(__name__)

_DEDUP_KEY = "kis_mock:exec_processed:{correlation_id}"
_DEDUP_TTL_SECONDS = 3600
_CHANNEL_PATTERN = "execution:*"


class KISMockExecutionConsumer:
    def __init__(
        self,
        *,
        redis_client: Any = None,
        reconcile_fn: Callable[..., Any] = run_kis_mock_reconciliation,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        force_dry_run: bool = False,
    ) -> None:
        self._redis = redis_client
        self._reconcile_fn = reconcile_fn
        self._session_factory = session_factory
        self._force_dry_run = force_dry_run
        self._stop = asyncio.Event()

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await _get_redis_client()
        return self._redis

    @staticmethod
    def _is_kis_mock_fill(event: dict) -> bool:
        if event.get("account_mode") != "kis_mock":
            return False
        if event.get("broker") != "kis":
            return False
        return event.get("fill_yn") == "Y" or str(event.get("execution_type")) == "1"

    async def handle_message(self, raw_message: str) -> str:
        try:
            event = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return "ignored_unparseable"
        if not isinstance(event, dict) or not self._is_kis_mock_fill(event):
            return "ignored_non_mock_fill"

        symbol = event.get("symbol")
        correlation_id = event.get("correlation_id")
        if not symbol:
            logger.warning("kis_mock fill without symbol; skipping: %s", event)
            return "ignored_no_symbol"
        if not correlation_id:
            logger.warning(
                "kis_mock fill without correlation_id; cannot dedupe, skipping"
            )
            return "ignored_no_correlation_id"

        redis_client = await self._client()
        first = await redis_client.set(
            _DEDUP_KEY.format(correlation_id=correlation_id),
            "1",
            nx=True,
            ex=_DEDUP_TTL_SECONDS,
        )
        if not first:
            return "skipped_dedup"

        dry_run = self._force_dry_run or not (
            settings.KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED
        )
        async with self._session_factory() as db:
            await self._reconcile_fn(db, symbol=symbol, dry_run=dry_run)
        return "reconciled_dry_run" if dry_run else "reconciled"

    def request_stop(self) -> None:
        self._stop.set()
```

> `async with self._session_factory() as db` — `AsyncSessionLocal`은 async_sessionmaker라 `AsyncSessionLocal()`이 세션 컨텍스트를 반환한다. 테스트 fake는 `_session_factory()`가 `_FakeSession()`을 반환하도록 맞춘다(같은 호출 형태).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_execution_consumer.py -v`
Expected: PASS (8건).

- [ ] **Step 5: 커밋**

```bash
git add app/services/kis_mock_execution_consumer.py tests/test_kis_mock_execution_consumer.py
git commit -m "feat(ROB-404): kis_mock execution consumer message handling (filter/dedupe/gate)"
```

---

## Task 3: Consumer subscribe 루프 + operator CLI

**Files:**
- Modify: `app/services/kis_mock_execution_consumer.py` (`run` 루프 추가)
- Create: `scripts/kis_mock_execution_consumer.py`
- Test: `tests/test_kis_mock_execution_consumer.py` (run 루프 1메시지 테스트)

- [ ] **Step 1: run 루프 테스트 작성**

`tests/test_kis_mock_execution_consumer.py`에 추가:

```python
class _FakePubSub:
    def __init__(self, messages):
        self._messages = messages
        self.unsubscribed = False
        self.closed = False

    async def psubscribe(self, pattern):
        self._pattern = pattern

    async def listen(self):
        for m in self._messages:
            yield m

    async def punsubscribe(self, pattern):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


class _FakeRedisWithPubSub(_FakeRedis):
    def __init__(self, messages):
        super().__init__()
        self._pubsub = _FakePubSub(messages)

    def pubsub(self):
        return self._pubsub


@pytest.mark.asyncio
async def test_run_loop_dispatches_pmessage(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    messages = [
        {"type": "psubscribe", "data": 1},  # ignored
        {"type": "pmessage", "channel": "execution:kr", "data": _fill_event()},
    ]
    redis_client = _FakeRedisWithPubSub(messages)
    calls: list[dict] = []

    async def _fake_reconcile(db, *, symbol=None, dry_run=True, **kw):
        calls.append({"symbol": symbol, "dry_run": dry_run})
        return {"success": True}

    consumer = KISMockExecutionConsumer(
        redis_client=redis_client,
        reconcile_fn=_fake_reconcile,
        session_factory=_session_factory,
    )
    await consumer.run()
    assert calls == [{"symbol": "005930", "dry_run": False}]
    assert redis_client._pubsub.unsubscribed is True
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_execution_consumer.py -k "run_loop" -v`
Expected: FAIL — `AttributeError: 'KISMockExecutionConsumer' object has no attribute 'run'`.

- [ ] **Step 3-a: run 루프 구현**

`app/services/kis_mock_execution_consumer.py` — `request_stop` 앞에 추가:

```python
    async def run(self) -> None:
        """Subscribe to execution:* and dispatch each fill to handle_message."""
        redis_client = await self._client()
        pubsub = redis_client.pubsub()
        await pubsub.psubscribe(_CHANNEL_PATTERN)
        logger.info("kis_mock execution consumer subscribed to %s", _CHANNEL_PATTERN)
        try:
            async for message in pubsub.listen():
                if self._stop.is_set():
                    break
                if message.get("type") != "pmessage":
                    continue
                try:
                    outcome = await self.handle_message(message["data"])
                    logger.debug("kis_mock execution event outcome=%s", outcome)
                except Exception:  # noqa: BLE001 - one bad event must not kill loop
                    logger.exception("kis_mock execution consumer handler failed")
        finally:
            await pubsub.punsubscribe(_CHANNEL_PATTERN)
            await pubsub.aclose()
```

- [ ] **Step 3-b: operator CLI 생성**

`scripts/kis_mock_execution_consumer.py` 생성:

```python
"""ROB-404 — operator CLI for the kis_mock execution-event consumer.

Default-disabled by design: actual broker mutation only when
KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED=true. ``preflight`` forces dry-run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="kis_mock execution-event consumer")
    parser.add_argument(
        "mode",
        choices=["preflight", "run"],
        help="preflight: force dry-run reconcile; run: honor the env gate",
    )
    return parser


async def _amain(mode: str) -> int:
    # Lazy import so --help runs without Settings/secret env.
    from app.services.kis_mock_execution_consumer import KISMockExecutionConsumer

    consumer = KISMockExecutionConsumer(force_dry_run=(mode == "preflight"))
    await consumer.run()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
uv run pytest tests/test_kis_mock_execution_consumer.py -v
uv run python scripts/kis_mock_execution_consumer.py --help
```
Expected: 테스트 PASS(9건). `--help`는 secret 없이 usage 출력.

- [ ] **Step 5: 커밋**

```bash
git add app/services/kis_mock_execution_consumer.py scripts/kis_mock_execution_consumer.py tests/test_kis_mock_execution_consumer.py
git commit -m "feat(ROB-404): consumer subscribe loop + operator CLI (preflight/run)"
```

---

## Task 4: taskiq 주기 reconcile 태스크 (paused)

**Files:**
- Create: `app/tasks/kis_mock_reconciliation_tasks.py`
- Test: `tests/test_kis_mock_reconciliation_periodic_task.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_reconciliation_periodic_task.py` 생성:

```python
"""ROB-404 — paused taskiq periodic reconcile task."""

from __future__ import annotations

import pytest

import app.tasks.kis_mock_reconciliation_tasks as task_mod


@pytest.mark.asyncio
async def test_periodic_paused_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "KIS_MOCK_RECONCILE_PERIODIC_ENABLED", False, raising=False
    )
    called = {"n": 0}

    async def _fake_reconcile(*a, **k):
        called["n"] += 1
        return {"success": True}

    monkeypatch.setattr(task_mod, "run_kis_mock_reconciliation", _fake_reconcile)
    result = await task_mod.kis_mock_reconcile_periodic()
    assert result["status"] == "paused"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_periodic_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "KIS_MOCK_RECONCILE_PERIODIC_ENABLED", True, raising=False
    )
    captured = {"dry_run": None, "n": 0}

    async def _fake_reconcile(db, *, dry_run=True, **k):
        captured["dry_run"] = dry_run
        captured["n"] += 1
        return {"success": True, "orders_processed": 0}

    monkeypatch.setattr(task_mod, "run_kis_mock_reconciliation", _fake_reconcile)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.kis_mock_reconcile_periodic()
    assert result["success"] is True
    assert captured["n"] == 1
    assert captured["dry_run"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_reconciliation_periodic_task.py -v`
Expected: FAIL — `ModuleNotFoundError: app.tasks.kis_mock_reconciliation_tasks`.

- [ ] **Step 3: 구현**

`app/tasks/kis_mock_reconciliation_tasks.py` 생성:

```python
"""ROB-404 — paused taskiq periodic kis_mock reconcile (fallback to the
event-driven consumer). NO schedule: starts paused; an operator adds the cron
+ flips KIS_MOCK_RECONCILE_PERIODIC_ENABLED in a follow-up.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation

logger = logging.getLogger(__name__)


@broker.task(task_name="kis_mock.reconcile_periodic")  # no schedule → paused
async def kis_mock_reconcile_periodic() -> dict:
    if not settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED:
        return {
            "status": "paused",
            "message": "KIS_MOCK_RECONCILE_PERIODIC_ENABLED is False",
        }
    async with AsyncSessionLocal() as db:
        return await run_kis_mock_reconciliation(db, dry_run=False)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_reconciliation_periodic_task.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/kis_mock_reconciliation_tasks.py tests/test_kis_mock_reconciliation_periodic_task.py
git commit -m "feat(ROB-404): paused taskiq periodic reconcile task (env-gated)"
```

---

## Task 5: 회귀 + lint/format/typecheck

**Files:** (검증만)

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_kis_mock_execution_consumer.py tests/test_kis_mock_reconciliation_periodic_task.py tests/jobs/test_kis_mock_reconciliation_job.py tests/test_execution_event.py -p no:randomly -v
```
Expected: 전부 PASS.

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run ruff format --check app/ tests/ scripts/
```
Expected: 통과(필요 시 `uv run ruff format app/ tests/ scripts/` 후 재확인 + 커밋).

- [ ] **Step 3: typecheck (변경 파일)**

Run:
```bash
uv run ty check app/services/kis_mock_execution_consumer.py app/jobs/kis_mock_reconciliation_job.py app/tasks/kis_mock_reconciliation_tasks.py app/core/config.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-404): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준

- kis_mock 체결 이벤트 → 해당 symbol reconcile(이벤트 구동), live/비-fill 무시, correlation_id 멱등(중복 1회만).
- gate off(기본)면 dry-run preflight, on이면 실반영. 주기 태스크는 paused(`{"status":"paused"}`)이며 flag on일 때만 배치 reconcile.
- ROB-400 attribution 커널 그대로 재사용(새 매칭 로직 없음). 마이그레이션 0.
- 스케줄러 auto-start 없음 — operator CLI + paused taskiq.

## 범위 밖 (후속)

- 주기 태스크 cron schedule 등록 + flag flip(operator, 별도 PR).
- operator live-mock smoke: 실제 kis_mock WS 발행 → consumer reconcile 라운드트립(creds 필요).
- ROB-405 회고 배선이 이벤트/reconcile 위에 구축.
