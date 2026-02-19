# Freqtrade NFI Research Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a fork-based `freqtrade + NostalgiaForInfinity` research pipeline for Binance Spot, then persist normalized research results into `auto_trader` Postgres (`research` schema) without using `freqtrade` for live trading.

**Architecture:** Keep research execution in a dedicated `auto-freqtrade` fork repository and keep live trading execution in `auto_trader`. Run heavy research jobs on Mac and lightweight jobs on Raspberry Pi, then ingest normalized result files into `auto_trader` via a dedicated parser, gate evaluator, and DB ingestion flow.

**Tech Stack:** Freqtrade, Docker Compose, Python 3.13, pytest, Alembic, SQLAlchemy (async), Pydantic, Postgres, TaskIQ (optional scheduler integration)

---

## Preconditions

- Work in isolated worktrees for both repositories.
- Do not mix `freqtrade` runtime dependencies into `/Users/robin/PycharmProjects/auto_trader`.
- Keep this invariant: `freqtrade` is research-only, `auto_trader` is the only live trading engine.

## Required Skills During Execution

- `@test-driven-development`
- `@verification-before-completion`
- `@requesting-code-review`

### Task 1: Bootstrap `auto-freqtrade` fork workspace contract

**Files:**
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_repo_contract.py`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/.env.research.example`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/user_data/config_research.base.json`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/README.auto.md`
- Test: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_repo_contract.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_research_contract_files_exist():
    required = [
        ".env.research.example",
        "user_data/config_research.base.json",
        "README.auto.md",
    ]
    missing = [path for path in required if not Path(path).exists()]
    assert missing == []
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_repo_contract.py::test_research_contract_files_exist -v`  
Expected: FAIL with missing file list.

**Step 3: Write minimal implementation**

```json
{
  "exchange": { "name": "binance" },
  "dry_run": true,
  "stake_currency": "USDT"
}
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_repo_contract.py::test_research_contract_files_exist -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto-freqtrade add tests/test_repo_contract.py .env.research.example user_data/config_research.base.json README.auto.md
git -C /Users/robin/PycharmProjects/auto-freqtrade commit -m "chore: bootstrap auto-freqtrade research contract files"
```

### Task 2: Add NFI pin manifest and sync utility

**Files:**
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/config/strategy_pin.toml`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/scripts/sync_nfi_strategy.py`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_sync_nfi_strategy.py`
- Modify: `/Users/robin/PycharmProjects/auto-freqtrade/Makefile`
- Test: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_sync_nfi_strategy.py`

**Step 1: Write the failing test**

```python
from scripts.sync_nfi_strategy import build_download_url


def test_build_download_url_uses_commit_pin():
    url = build_download_url(
        repo="iterativv/NostalgiaForInfinity",
        commit="abc123",
        strategy_file="NostalgiaForInfinityX.py",
    )
    assert url.endswith("/abc123/NostalgiaForInfinityX.py")
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_sync_nfi_strategy.py::test_build_download_url_uses_commit_pin -v`  
Expected: FAIL with import/function error.

**Step 3: Write minimal implementation**

```python
def build_download_url(repo: str, commit: str, strategy_file: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{repo}/{commit}/"
        f"{strategy_file}"
    )
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_sync_nfi_strategy.py::test_build_download_url_uses_commit_pin -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto-freqtrade add config/strategy_pin.toml scripts/sync_nfi_strategy.py tests/test_sync_nfi_strategy.py Makefile
git -C /Users/robin/PycharmProjects/auto-freqtrade commit -m "feat: add pinned NFI strategy sync workflow"
```

### Task 3: Standardize backtest summary export format

**Files:**
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/scripts/export_backtest_summary.py`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_export_backtest_summary.py`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/reports/.gitkeep`
- Test: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_export_backtest_summary.py`

**Step 1: Write the failing test**

```python
from scripts.export_backtest_summary import normalize_summary


def test_normalize_summary_maps_total_trades():
    payload = {"strategy": {"NFI": {"total_trades": 42}}}
    data = normalize_summary(payload, strategy_name="NFI")
    assert data["total_trades"] == 42
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_export_backtest_summary.py::test_normalize_summary_maps_total_trades -v`  
Expected: FAIL with import/function error.

**Step 3: Write minimal implementation**

```python
def normalize_summary(payload: dict, strategy_name: str) -> dict:
    metrics = payload["strategy"][strategy_name]
    return {
        "strategy_name": strategy_name,
        "total_trades": int(metrics.get("total_trades", 0)),
    }
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_export_backtest_summary.py::test_normalize_summary_maps_total_trades -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto-freqtrade add scripts/export_backtest_summary.py tests/test_export_backtest_summary.py reports/.gitkeep
git -C /Users/robin/PycharmProjects/auto-freqtrade commit -m "feat: add normalized backtest summary export"
```

### Task 4: Add `research` DB schema migration in `auto_trader`

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/alembic/versions/<timestamp>_add_research_backtest_tables.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/app/models/research_backtest.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/models/__init__.py`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/integration/test_research_schema_migration.py`

**Step 1: Write the failing test**

```python
import pytest


@pytest.mark.integration
async def test_research_tables_exist(async_session):
    result = await async_session.execute(
        "select to_regclass('research.backtest_runs')"
    )
    assert result.scalar() == "research.backtest_runs"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/integration/test_research_schema_migration.py::test_research_tables_exist -v`  
Expected: FAIL with missing relation.

**Step 3: Write minimal implementation**

```python
op.execute("CREATE SCHEMA IF NOT EXISTS research")
op.create_table(
    "backtest_runs",
    sa.Column("id", sa.BigInteger(), primary_key=True),
    sa.Column("run_id", sa.String(length=64), nullable=False, unique=True),
    sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
    schema="research",
)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run alembic upgrade head && uv run pytest --no-cov tests/integration/test_research_schema_migration.py::test_research_tables_exist -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto_trader add alembic/versions app/models/research_backtest.py app/models/__init__.py tests/integration/test_research_schema_migration.py
git -C /Users/robin/PycharmProjects/auto_trader commit -m "feat: add research schema backtest core tables"
```

### Task 5: Implement normalized payload schema/parser

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/schemas/research_backtest.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/app/services/research_backtest_parser.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_backtest_parser.py`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_backtest_parser.py`

**Step 1: Write the failing test**

```python
from app.services.research_backtest_parser import parse_backtest_summary


def test_parse_backtest_summary_reads_required_fields():
    payload = {"run_id": "run-1", "total_trades": 25, "profit_factor": 1.4}
    parsed = parse_backtest_summary(payload)
    assert parsed.run_id == "run-1"
    assert parsed.total_trades == 25
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_backtest_parser.py::test_parse_backtest_summary_reads_required_fields -v`  
Expected: FAIL with import error.

**Step 3: Write minimal implementation**

```python
from app.schemas.research_backtest import BacktestRunSummary


def parse_backtest_summary(payload: dict) -> BacktestRunSummary:
    return BacktestRunSummary.model_validate(payload)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_backtest_parser.py::test_parse_backtest_summary_reads_required_fields -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto_trader add app/schemas/research_backtest.py app/services/research_backtest_parser.py tests/test_research_backtest_parser.py
git -C /Users/robin/PycharmProjects/auto_trader commit -m "feat: add freqtrade summary parser contract"
```

### Task 6: Implement gate evaluator (`minimum_trade_count` + metric thresholds)

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/services/research_gate_service.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_gate_service.py`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_gate_service.py`

**Step 1: Write the failing test**

```python
from app.services.research_gate_service import evaluate_candidate


def test_reject_when_closed_trade_count_is_too_low():
    result = evaluate_candidate(
        total_trades=8,
        profit_factor=1.8,
        max_drawdown=0.08,
        config={"minimum_trade_count": 20},
    )
    assert result.status == "FAIL"
    assert result.reason_code == "MIN_TRADES"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_gate_service.py::test_reject_when_closed_trade_count_is_too_low -v`  
Expected: FAIL with import/function error.

**Step 3: Write minimal implementation**

```python
def evaluate_candidate(*, total_trades: int, profit_factor: float, max_drawdown: float, config: dict):
    min_trades = int(config.get("minimum_trade_count", 0))
    if total_trades < min_trades:
        return GateResult(status="FAIL", reason_code="MIN_TRADES")
    return GateResult(status="PASS", reason_code="OK")
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_gate_service.py::test_reject_when_closed_trade_count_is_too_low -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto_trader add app/services/research_gate_service.py tests/test_research_gate_service.py
git -C /Users/robin/PycharmProjects/auto_trader commit -m "feat: add research candidate gate evaluator"
```

### Task 7: Add ingestion service + CLI entrypoint

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/services/research_ingestion_service.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/scripts/ingest_freqtrade_report.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_ingestion_service.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/integration/test_ingest_freqtrade_report.py`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_research_ingestion_service.py`

**Step 1: Write the failing test**

```python
from app.services.research_ingestion_service import ingest_summary_payload


async def test_ingest_summary_payload_returns_run_id(async_session):
    payload = {"run_id": "run-20260219-01", "total_trades": 31, "profit_factor": 1.3}
    run_id = await ingest_summary_payload(async_session, payload)
    assert run_id == "run-20260219-01"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_ingestion_service.py::test_ingest_summary_payload_returns_run_id -v`  
Expected: FAIL with import error.

**Step 3: Write minimal implementation**

```python
async def ingest_summary_payload(session, payload: dict) -> str:
    parsed = parse_backtest_summary(payload)
    await upsert_backtest_run(session, parsed)
    await session.commit()
    return parsed.run_id
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_research_ingestion_service.py::test_ingest_summary_payload_returns_run_id -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto_trader add app/services/research_ingestion_service.py scripts/ingest_freqtrade_report.py tests/test_research_ingestion_service.py tests/integration/test_ingest_freqtrade_report.py
git -C /Users/robin/PycharmProjects/auto_trader commit -m "feat: ingest freqtrade research summaries into research schema"
```

### Task 8: Add lightweight Pi research job wrapper (research-only)

**Files:**
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/scripts/run_bt_light.sh`
- Create: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_run_bt_light.py`
- Modify: `/Users/robin/PycharmProjects/auto-freqtrade/Makefile`
- Test: `/Users/robin/PycharmProjects/auto-freqtrade/tests/test_run_bt_light.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_bt_light_script_has_pair_and_timerange_limits():
    script = Path("scripts/run_bt_light.sh").read_text()
    assert "--pairs" in script
    assert "--timerange" in script
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_run_bt_light.py::test_bt_light_script_has_pair_and_timerange_limits -v`  
Expected: FAIL with missing script.

**Step 3: Write minimal implementation**

```bash
freqtrade backtesting \
  --config user_data/config_research.base.json \
  --strategy NostalgiaForInfinityX \
  --timerange 20260101- \
  --pairs BTC/USDT ETH/USDT BNB/USDT
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto-freqtrade && pytest tests/test_run_bt_light.py::test_bt_light_script_has_pair_and_timerange_limits -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto-freqtrade add scripts/run_bt_light.sh tests/test_run_bt_light.py Makefile
git -C /Users/robin/PycharmProjects/auto-freqtrade commit -m "feat: add raspberry-pi lightweight backtest runner"
```

### Task 9: Add runbook and operational checklists

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/docs/runbooks/freqtrade-research-pipeline.md`
- Modify: `/Users/robin/PycharmProjects/auto_trader/README.md`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_config.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_runbook_exists():
    assert Path("docs/runbooks/freqtrade-research-pipeline.md").exists()
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_config.py::test_runbook_exists -v`  
Expected: FAIL because runbook file is missing.

**Step 3: Write minimal implementation**

```markdown
## Daily Research Routine
1. Run heavy backtest on Mac.
2. Run light sanity backtest on Pi.
3. Ingest summaries into research schema.
4. Review PASS/FAIL candidates manually.
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/robin/PycharmProjects/auto_trader && uv run pytest --no-cov tests/test_config.py::test_runbook_exists -v`  
Expected: PASS.

**Step 5: Commit**

```bash
git -C /Users/robin/PycharmProjects/auto_trader add docs/runbooks/freqtrade-research-pipeline.md README.md tests/test_config.py
git -C /Users/robin/PycharmProjects/auto_trader commit -m "docs: add freqtrade research pipeline runbook"
```

## Verification Checklist Before Merge

Run in `/Users/robin/PycharmProjects/auto_trader`:

```bash
uv run ruff check app scripts tests
uv run pyright app
uv run pytest -q tests/test_research_backtest_parser.py tests/test_research_gate_service.py tests/test_research_ingestion_service.py
uv run pytest -q tests/integration/test_research_schema_migration.py tests/integration/test_ingest_freqtrade_report.py
```

Run in `/Users/robin/PycharmProjects/auto-freqtrade`:

```bash
pytest -q tests/test_repo_contract.py tests/test_sync_nfi_strategy.py tests/test_export_backtest_summary.py tests/test_run_bt_light.py
```

