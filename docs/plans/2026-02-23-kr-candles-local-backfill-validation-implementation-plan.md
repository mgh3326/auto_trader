# KR Candles Local Backfill Validation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 로컬 환경에서 실제 KIS 호출로 KR 3세션 backfill을 수행하고 `kr_candles_1m` 적재 및 `kr_candles_1h` 시간봉 생성을 검증한다.

**Architecture:** 신규 코드 추가 없이 기존 운영 엔트리포인트(`scripts/sync_kr_candles.py`)를 그대로 실행한다. 실행 전 선행조건(DB/Timescale/심볼 유니버스/자격증명)을 확인하고, 실행 후 SQL 검증으로 성공 기준(1m 적재 + 1h 조회 가능)을 판정한다. 실패 시 @systematic-debugging 흐름으로 즉시 원인 분류 후 재시도한다.

**Tech Stack:** Python 3.13+, uv, PostgreSQL/TimescaleDB, SQLAlchemy AsyncSession, KIS Open API

---

### Task 1: Preflight Guard Checks

**Files:**
- Reference: `scripts/sync_kr_candles.py`
- Reference: `app/services/kr_candles_sync_service.py`
- Reference: `scripts/sql/kr_candles_timescale.sql`

**Step 1: Write the failing preflight check command**

```bash
uv run python - <<'PY'
import asyncio
from sqlalchemy import text
from app.core.config import settings
from app.core.db import AsyncSessionLocal

required = ["kis_app_key", "kis_app_secret", "DATABASE_URL"]
missing = [name for name in required if not getattr(settings, name, None)]
if missing:
    raise SystemExit(f"Missing required settings: {missing}")

async def main():
    async with AsyncSessionLocal() as session:
        for obj in ("public.kr_candles_1m", "public.kr_candles_1h", "public.kr_symbol_universe"):
            val = (await session.execute(text("SELECT to_regclass(:n)"), {"n": obj})).scalar_one()
            if not val:
                raise SystemExit(f"Missing DB object: {obj}")
        universe_cnt = (await session.execute(text("SELECT COUNT(*) FROM public.kr_symbol_universe WHERE is_active = true"))).scalar_one()
        if int(universe_cnt) == 0:
            raise SystemExit("kr_symbol_universe has no active rows")
        print("preflight_ok")

asyncio.run(main())
PY
```

**Step 2: Run check to verify preconditions**

Run: 위 명령 실행  
Expected: `preflight_ok` 출력.  
If fail: blocker를 기록하고 Task 1 Step 3로 이동.

**Step 3: Resolve blockers minimally**

Run exactly what is needed:
- Migration required: `uv run alembic upgrade head`
- Universe missing/stale: `make sync-kr-symbol-universe`

**Step 4: Re-run preflight**

Run: Step 1 명령 재실행  
Expected: PASS (`preflight_ok`)

**Step 5: Commit**

```bash
# 코드 변경이 없으면 commit 생략 (no-op)
git status --short
```

---

### Task 2: Execute Real Backfill (3 Sessions)

**Files:**
- Run: `scripts/sync_kr_candles.py`
- Reference: `app/jobs/kr_candles.py`

**Step 1: Write a failing runtime assertion target**

성공 조건: 스크립트 exit code `0` and payload/status `completed`.

**Step 2: Run backfill command**

Run:

```bash
uv run python scripts/sync_kr_candles.py --mode backfill --sessions 3
```

Expected: 종료 코드 `0`, 로그에 `KR candles sync completed` 포함.

**Step 3: If failure, debug with bounded evidence (@systematic-debugging)**

Run:

```bash
uv run python scripts/sync_kr_candles.py --mode backfill --sessions 1
```

Expected: 최소 범위에서 동일 실패 재현 여부 확인.  
실패 유형을 `precondition`, `KIS API`, `DB upsert`로 분류 후 원인 하나씩 해소.

**Step 4: Re-run target scope**

Run:

```bash
uv run python scripts/sync_kr_candles.py --mode backfill --sessions 3
```

Expected: PASS (`completed`)

**Step 5: Commit**

```bash
# 코드 변경이 없으면 commit 생략 (no-op)
git status --short
```

---

### Task 3: Validate Success Criteria (1m Loaded + 1h Available)

**Files:**
- Reference: `scripts/sql/kr_candles_timescale.sql`

**Step 1: Write failing SQL assertions**

```sql
SELECT symbol, venue, COUNT(*) AS cnt, MIN(time) AS min_time, MAX(time) AS max_time
FROM public.kr_candles_1m
GROUP BY symbol, venue
ORDER BY cnt DESC
LIMIT 20;
```

```sql
SELECT symbol, bucket, open, high, low, close, volume, value, venues
FROM public.kr_candles_1h
ORDER BY bucket DESC
LIMIT 50;
```

**Step 2: Run validation queries**

Run via local SQL client (`psql`, DBeaver, TablePlus) against same `DATABASE_URL`.  
Expected:
- 첫 쿼리에서 유의미한 `cnt > 0` 결과 존재
- 둘째 쿼리에서 최근 `bucket` 행 존재

**Step 3: Optional symbol-level assertion**

```sql
SELECT
  (SELECT COUNT(*) FROM public.kr_candles_1m WHERE symbol = '005930') AS m1_rows,
  (SELECT COUNT(*) FROM public.kr_candles_1h WHERE symbol = '005930') AS h1_rows;
```

Expected: `m1_rows > 0` and `h1_rows > 0`

**Step 4: Final verification gate (@verification-before-completion)**

성공 판정:
1. `kr_candles_1m` 적재 확인
2. `kr_candles_1h` 시간봉 조회 확인

실패 시 Task 2/Task 1로 되돌아가 원인 해소 후 재검증.

**Step 5: Commit**

```bash
# 코드 변경이 없으면 commit 생략 (no-op)
git status --short
```
