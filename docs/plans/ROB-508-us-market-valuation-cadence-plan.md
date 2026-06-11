# ROB-508 — US market_valuation_snapshots 갱신 cadence + 프리셋 에러/표시 보강 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US 펀더멘털 프리셋 4종(high_yield_value/undervalued_growth/profitable_company/undervalued_breakout)이 직전 거래일 기준 `dataState: fresh`로 서빙되도록 Prefect 일일 갱신을 추가하고, 알 수 없는 프리셋 에러에 유효 목록을 동봉하며, 펀더멘털 행의 가격/등락률/거래량 표시 공백을 메운다.

**Architecture:** 작업이 **두 레포**에 걸친다. ① `auto_trader`(이 worktree): 프리셋 에러 메시지 + 로더 행 가격 hydration (코드 변경, migration 0, 브로커/주문 mutation 0). ② `robin-prefect-automations`(`/Users/mgh3326/services/prefect`): auto_trader CLI를 subprocess로 감싸는 신규 flow 2개 + paused-by-default deployment 등록. 프로덕션 스케줄러는 Prefect이며(검증: `prefect deployment ls`에 market_valuation 계열 부재 확정), prefect flow는 auto_trader의 `app/flows/`를 import하지 않고 **CLI를 직접 호출**하는 것이 기존 패턴이다 (`flows/auto_trader/invest_kr_fundamentals_snapshots.py` 미러).

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / pytest (auto_trader) · Prefect 3 / subprocess CLI wrapper (robin-prefect-automations)

---

## 검증된 사실 (2026-06-11 grounding)

| 전제 | 검증 결과 |
|---|---|
| `market_valuation_snapshots` Prefect deployment 부재 | **확정** — `prefect deployment ls`(127.0.0.1:4200)에 US Invest Screener Snapshots, invest_kr_fundamentals_snapshots 등은 있으나 market_valuation/us_fundamentals 계열 없음 |
| US `financial_fundamentals_snapshots` 갱신 스케줄 부재 | **확정** — `scripts/build_us_fundamentals_snapshots.py`(yfinance, annual 기본)는 CLI만 존재 |
| AC① freshness는 cadence만으로 닫힘 | **확정** — ROB-440에서 `_us_valuation_partition_computed_at` backfill(`screener_service.py:2167-2176`) + `fundamentals_screener.py:426` `val_state = "fresh" if val_date == today_market_date` 이미 wired. 파티션이 최신이면 fresh로 분류됨 |
| US `undervalued_growth`/`profitable_company` 행 가격 `"-"` | **확정** — `load_fundamentals_preset_from_snapshots`(`fundamentals_screener.py:332-519`)는 가격 join 없음. row에 `close`/`volume`/`change_rate` 키 부재 |
| US `high_yield_value`/`undervalued_breakout`은 가격 join 있으나 **priceLabel은 여전히 `"-"`** | **확정** — 로더 row 키가 `latest_close`인데 priceLabel은 `row.get("close") or row.get("price") or row.get("current_price")`만 읽음(`screener_service.py:2336-2347`). changePct/volume은 키 일치(`change_rate`/`volume`)로 정상 |
| KR 펀더멘털 표시는 스코프 밖 | **확정** — KR display는 ROB-428 PR-B에서 tvscreener 로더로 교체돼 `"close": _to_float(snap.price)` 이미 채움(`kr_fundamentals_tv_screener.py:428`) |
| 프리셋 에러 위치 | `screener_service.py:1731-1747` — `warnings=[f"알 수 없는 프리셋: {preset_id}"]`만 반환. 유효 목록은 `screener_presets.py`의 `preset_definitions(market)`로 조회 가능 |
| Prefect flow 최신 패턴 | `flows/auto_trader/invest_kr_fundamentals_snapshots.py` — env-file commit gate(`_env_file_commit_gate_enabled`), lockdir, `redact_secrets`, `paused=True` 기본 deploy 함수 |
| market_valuation CLI 출력 | `built {N} valuation snapshots for {M} {MARKET} symbols (dry_run={bool}, batches={N})` + `committed {N} rows.` / `--dry-run: no rows written.` / `COMMIT BLOCKED: {reason}` (exit 2) |

**스코프 제외:** KR 펀더멘털 행 enrichment(ROB-428에서 완료), TaskIQ 쪽 US 슬롯 추가(운영 스케줄러는 Prefect — `app/tasks/market_valuation_snapshot_tasks.py`의 KR TaskIQ 태스크는 건드리지 않음), `ScreenerResultsResponse` 스키마 필드 추가(warning 텍스트로 충분).

---

# Part A — auto_trader (worktree `/Users/mgh3326/work/auto_trader.rob-508`, branch `rob-508`, base `origin/main`)

모든 명령은 worktree 루트에서 실행. 커밋 트레일러: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

### Task A0: 브랜치 base를 최신 origin/main으로 갱신

worktree 생성 시점이 ROB-512 머지(2e9ad1a7, 2026-06-11) 이전이라 base가 낡았다. ROB-512는 `double_buy_screener.py`/`screener_service.py`(investor_flow 영역, +60줄)에 같은 패턴의 `close` 키를 추가했고 **이 플랜의 대상 파일과 충돌하지 않음**을 확인했으나, `screener_service.py` 라인 번호가 시프트됐다.

- [ ] **Step A0-1: base 갱신** (rob-508에 아직 커밋 없음 — 플랜 문서는 untracked라 안전)

```bash
git fetch --prune origin
git status --short          # untracked 플랜 문서만 있어야 함
git switch -C rob-508 origin/main
```

- [ ] **Step A0-2: 본 플랜의 라인 번호 재확인** — `grep -n "알 수 없는 프리셋" app/services/invest_view_model/screener_service.py`로 A1 대상 위치 재탐색 (1730 근처에서 ~+60 시프트 가능). A2/A3 대상 파일은 ROB-512 미변경이라 그대로.

### Task A1: 알 수 없는 프리셋 에러에 유효 preset 목록 동봉

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:1730-1747`
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step A1-1: 실패하는 테스트 작성**

`tests/test_invest_view_model_screener_service.py` 끝에 추가 (기존 `_FakeResolver`, `build_screener_results` import 재사용):

```python
@pytest.mark.asyncio
async def test_unknown_preset_error_lists_valid_presets() -> None:
    """ROB-508: 알 수 없는 프리셋 에러는 해당 market의 유효(active) preset id
    목록을 동봉해야 한다 (예: preset="oversold" 오타 시 발견 가능하게)."""
    resp = await build_screener_results(
        preset_id="oversold",
        screening_service=_FakeScreeningService(),
        resolver=_FakeResolver(watched=set()),
        market="us",
        session=None,
    )
    assert resp.results == []
    assert len(resp.warnings) == 2
    assert resp.warnings[0] == "알 수 없는 프리셋: oversold"
    # 두 번째 warning에 US active preset id들이 포함돼야 함
    assert "사용 가능한 프리셋(us)" in resp.warnings[1]
    assert "consecutive_gainers" in resp.warnings[1]
    assert "undervalued_growth" in resp.warnings[1]
    # US에서 unsupported인 KR 전용 preset은 목록에 없어야 함
    assert "double_buy" not in resp.warnings[1]


@pytest.mark.asyncio
async def test_unknown_preset_error_lists_valid_presets_crypto() -> None:
    resp = await build_screener_results(
        preset_id="volume_surge",
        screening_service=_FakeScreeningService(),
        resolver=_FakeResolver(watched=set()),
        market="crypto",
        session=None,
    )
    assert "사용 가능한 프리셋(crypto)" in resp.warnings[1]
    assert "crypto_high_volume" in resp.warnings[1]
```

주의: 파일 상단의 기존 screening-service fake 클래스 이름을 확인해 맞출 것 (`_FakeScreeningService`가 없으면 기존 happy-path 테스트가 쓰는 stub을 그대로 사용). unknown-preset 분기는 screening_service를 호출하지 않으므로 어떤 stub이든 무방.

- [ ] **Step A1-2: 실패 확인**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v -k "unknown_preset"`
Expected: FAIL — `len(resp.warnings) == 2` (현재 1개)

- [ ] **Step A1-3: 구현**

`screener_service.py:1731-1747`의 `if preset is None:` 블록에서 warnings 부분을 교체:

```python
    if preset is None:
        freshness = _build_freshness(
            raw_timestamp=None,
            cache_hit=False,
            market=requested_market,
            now=now,
        )
        # ROB-508: 오타/구버전 클라이언트가 스스로 복구할 수 있도록 해당 market의
        # active preset id 목록을 동봉한다 (data_pending/unsupported는 제외).
        from app.services.invest_view_model.screener_presets import preset_definitions

        _valid_ids = [
            p.id
            for p in preset_definitions(requested_market)
            if p.availability == "active"
        ]
        return ScreenerResultsResponse(
            presetId=preset_id,
            title=preset_id,
            description="",
            filterChips=[],
            metricLabel="-",
            results=[],
            warnings=[
                f"알 수 없는 프리셋: {preset_id}",
                f"사용 가능한 프리셋({requested_market}): {', '.join(_valid_ids)}",
            ],
            freshness=freshness,
        )
```

주의: `preset_definitions`가 이미 모듈 상단에서 import돼 있으면 로컬 import 제거. `ScreenerPreset`의 availability 속성명은 `screener_presets.py:595` 근처 `_with_market`이 stamp하는 필드명(`availability`)과 일치 확인.

- [ ] **Step A1-4: 통과 확인**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: 신규 2개 PASS + 기존 전부 PASS (기존 unknown-preset 단언이 warnings 길이를 고정했다면 함께 수정)

- [ ] **Step A1-5: 커밋**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-508): unknown screener preset error lists valid preset ids"
```

### Task A2: US 펀더멘털 로더 행 가격/등락률/거래량 hydration

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py` (import 블록 + `load_fundamentals_preset_from_snapshots` 끝부분, 현재 492-519행 근처)
- Test: `tests/test_fundamentals_screener.py`

- [ ] **Step A2-1: 실패하는 테스트 작성**

`tests/test_fundamentals_screener.py`의 기존 테스트들이 쓰는 세션/스냅샷 fixture 패턴을 먼저 읽고 동일 패턴으로 추가. 핵심 단언:

```python
@pytest.mark.asyncio
async def test_us_fundamentals_rows_hydrate_price_from_invest_screener(db_session) -> None:
    """ROB-508: US 펀더멘털 행은 최신 invest_screener_snapshots 파티션에서
    close/change_rate/volume을 hydrate해야 한다 (priceLabel "-" 공백 해소)."""
    # given: market_valuation_snapshots(us, AAPL, roe/per 통과값)
    #        + financial_fundamentals_snapshots(us, AAPL, 연간 2기 성장 통과값)
    #        + invest_screener_snapshots(us, AAPL, latest_close=290.55,
    #          change_rate=1.2, daily_volume=1000)  ← 기존 fixture 헬퍼 재사용
    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="us",
        spec=FUNDAMENTALS_PRESET_SPECS["profitable_company"],
        limit=20,
    )
    assert result is not None and result.rows
    row = result.rows[0]
    assert row["close"] == pytest.approx(290.55)
    assert row["change_rate"] == pytest.approx(1.2)
    assert row["volume"] == 1000


@pytest.mark.asyncio
async def test_us_fundamentals_rows_tolerate_missing_quote(db_session) -> None:
    """invest_screener 파티션에 해당 심볼이 없어도 행은 유지되고 키만 None."""
    # given: valuation+fundamentals만 있고 invest_screener_snapshots에 행 없음
    result = await load_fundamentals_preset_from_snapshots(
        db_session, market="us",
        spec=FUNDAMENTALS_PRESET_SPECS["profitable_company"], limit=20,
    )
    assert result is not None and result.rows
    assert result.rows[0].get("close") is None  # 행 자체는 살아있음 (fail-open)
```

- [ ] **Step A2-2: 실패 확인**

Run: `uv run pytest tests/test_fundamentals_screener.py -v -k "hydrate or tolerate"`
Expected: FAIL — `row["close"]` KeyError/None

- [ ] **Step A2-3: 구현**

`fundamentals_screener.py` import 블록(25행 근처)에 추가:

```python
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
```

`load_fundamentals_preset_from_snapshots`에서 `for r in included:` 루프(492-508행, snapshot_date/market/dividend_yield stamp) **다음**, `return FundamentalsScreenResult(...)` **직전**에 추가:

```python
    # ROB-508: US display rows hydrate close/change_rate/volume from the latest
    # invest_screener partition (mirrors the high_yield_value US loader join,
    # high_yield_value_screener.py:74-105) so priceLabel/changePctLabel/volumeLabel
    # render instead of "-". Fail-open: a lookup failure must not drop the preset.
    if market == "us" and included:
        try:
            price_date = (
                await session.execute(
                    sa.select(sa.func.max(InvestScreenerSnapshot.snapshot_date)).where(
                        InvestScreenerSnapshot.market == market
                    )
                )
            ).scalar_one_or_none()
            if price_date is not None:
                quote_rows = (
                    await session.execute(
                        sa.select(
                            InvestScreenerSnapshot.symbol,
                            InvestScreenerSnapshot.latest_close,
                            InvestScreenerSnapshot.change_rate,
                            InvestScreenerSnapshot.daily_volume,
                        ).where(
                            InvestScreenerSnapshot.market == market,
                            InvestScreenerSnapshot.snapshot_date == price_date,
                            InvestScreenerSnapshot.symbol.in_(
                                [r["symbol"] for r in included]
                            ),
                        )
                    )
                ).mappings()
                quote_map = {q["symbol"]: q for q in quote_rows}
                for r in included:
                    q = quote_map.get(r["symbol"])
                    if q is None:
                        continue
                    r["close"] = (
                        float(q["latest_close"])
                        if q["latest_close"] is not None
                        else None
                    )
                    r["change_rate"] = (
                        float(q["change_rate"])
                        if q["change_rate"] is not None
                        else None
                    )
                    r["volume"] = q["daily_volume"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fundamentals_screener: price hydration failed (rows kept): %s",
                exc,
                exc_info=True,
            )
```

주의: `market == "us"` 게이트 필수 — 이 로더의 KR 경로는 reports/PIT(DART) 전용이며 display가 아니므로 join을 추가하지 않는다.

- [ ] **Step A2-4: 통과 확인**

Run: `uv run pytest tests/test_fundamentals_screener.py -v`
Expected: 신규 2개 포함 전부 PASS

- [ ] **Step A2-5: 커밋**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-508): hydrate US fundamentals rows with price/change/volume from invest_screener partition"
```

### Task A3: high_yield_value / undervalued_breakout 로더 `close` 키 정합

두 US 로더는 invest_screener join을 이미 하지만 row 키가 `latest_close`라 priceLabel(`close|price|current_price`만 읽음)이 `"-"`가 된다. `close` 키를 추가한다 (additive — reports/PIT collector 소비자에는 키 추가가 무해).

**Files:**
- Modify: `app/services/invest_view_model/high_yield_value_screener.py:196-198` (row dict)
- Modify: `app/services/invest_view_model/undervalued_breakout_screener.py:227-229` (row dict)
- Test: `tests/test_invest_view_model_high_yield_value_screener.py`

- [ ] **Step A3-1: 실패하는 테스트 작성**

`tests/test_invest_view_model_high_yield_value_screener.py`에 기존 fixture 패턴으로:

```python
@pytest.mark.asyncio
async def test_rows_carry_close_alias_for_price_label(db_session) -> None:
    """ROB-508: priceLabel은 row['close']를 읽으므로 latest_close와 동일 값의
    close 키가 있어야 한다."""
    rows = await load_high_yield_value_from_snapshots(db_session, market="us", limit=20)
    assert rows
    assert rows[0]["close"] == rows[0]["latest_close"]
```

undervalued_breakout 동형 테스트를 해당 테스트 파일(`grep -l undervalued_breakout tests/`로 확인)에 추가.

- [ ] **Step A3-2: 실패 확인**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py -v -k close_alias`
Expected: FAIL — KeyError 'close'

- [ ] **Step A3-3: 구현**

`high_yield_value_screener.py` row dict(196행 근처) `"latest_close": ...` 항목 다음에:

```python
                "close": (
                    float(r["latest_close"]) if r["latest_close"] is not None else None
                ),
```

`undervalued_breakout_screener.py` row dict(227행 근처)에 동일 추가:

```python
                "close": float(r["latest_close"])
                if r["latest_close"] is not None
                else None,
```

- [ ] **Step A3-4: 통과 확인**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_fundamentals_screener.py -v` (+ undervalued_breakout 테스트 파일)
Expected: PASS

- [ ] **Step A3-5: 커밋**

```bash
git add app/services/invest_view_model/high_yield_value_screener.py app/services/invest_view_model/undervalued_breakout_screener.py tests/
git commit -m "fix(ROB-508): add close alias so US valuation preset rows render priceLabel"
```

### Task A4: 게이트 + 런북 + PR

- [ ] **Step A4-1: 로컬 게이트 (CI 적색 예방 — ROB-469 교훈: app/ + tests/ + scripts/ 전부)**

```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
make typecheck
uv run pytest tests/test_invest_view_model_screener_service.py tests/test_fundamentals_screener.py tests/test_invest_view_model_high_yield_value_screener.py tests/test_invest_view_model_screener_presets.py tests/test_screener_presets_profitable_company.py -v
```

Expected: 전부 green. 이후 스크리너 인접 전체:

```bash
uv run pytest tests/ -v -m "not integration and not slow" -k "screener or fundamentals or preset"
```

- [ ] **Step A4-2: 런북 갱신**

`docs/runbooks/invest-screener-snapshots.md`에 §추가: "US market_valuation / us_fundamentals 일일 갱신 (ROB-508)" — Prefect deployment 이름, 스케줄, env 게이트(`MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED`), 수동 빌드 fallback 커맨드(`uv run python -m scripts.build_market_valuation_snapshots --market us --all --common-stocks-only --commit`), freshness 확인 방법(스크리너 응답 `freshness.primary.snapshotDate`/`dataState`).

- [ ] **Step A4-3: 커밋 + PR**

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "docs(ROB-508): runbook for US market_valuation daily refresh"
git push -u origin rob-508
gh pr create --base main --title "feat(ROB-508): US screener preset error listing + fundamentals row price hydration" --body "..."
```

PR 본문에 명시: migration 0 / 브로커·주문 mutation 0 / cadence 본체는 robin-prefect-automations PR (링크).

---

# Part B — robin-prefect-automations (`/Users/mgh3326/services/prefect`, base `main`)

**주의:** 이 레포는 원격 CI가 없고 로컬 테스트가 authoritative (ROB-413 메모). 작업 전 `git status --short`로 dirty 여부 확인하고 feature branch 생성: `git switch -c feature/rob-508-market-valuation-us-flow`.

### Task B1: `market_valuation_snapshots_us` flow

**Files:**
- Create: `/Users/mgh3326/services/prefect/flows/auto_trader/market_valuation_snapshots_us.py`
- Test: `/Users/mgh3326/services/prefect/tests/test_market_valuation_snapshots_us.py`

- [ ] **Step B1-1: 실패하는 테스트 작성** (summary 파서 — flow 자체는 subprocess라 파서만 단위테스트, 기존 `tests/test_market_events.py` 스타일)

```python
from flows.auto_trader.market_valuation_snapshots_us import _parse_summary


def test_parse_summary_dry_run() -> None:
    stdout = (
        "built 4231 valuation snapshots for 4380 US symbols (dry_run=True, batches=22):\n"
        "idempotency:\n  wouldInsert: 4100\n  wouldUpdate: 131\n"
        "\n--dry-run: no rows written.\n"
    )
    s = _parse_summary(stdout, "")
    assert s["snapshots_built"] == 4231
    assert s["symbols_resolved"] == 4380
    assert s["market"] == "US"
    assert s["dry_run"] is True
    assert s["committed_rows"] == 0


def test_parse_summary_committed() -> None:
    stdout = (
        "built 4231 valuation snapshots for 4380 US symbols (dry_run=False, batches=22):\n"
        "\ncommitted 4231 rows.\n"
    )
    s = _parse_summary(stdout, "")
    assert s["dry_run"] is False
    assert s["committed_rows"] == 4231


def test_parse_summary_commit_blocked() -> None:
    s = _parse_summary("\nCOMMIT BLOCKED: coverage 0.31 below floor 0.80\n", "")
    assert "coverage 0.31" in s["block_reason"]
```

- [ ] **Step B1-2: 실패 확인**

Run: `cd /Users/mgh3326/services/prefect && uv run pytest tests/test_market_valuation_snapshots_us.py -v`
Expected: FAIL — module not found

- [ ] **Step B1-3: flow 구현**

`flows/auto_trader/market_valuation_snapshots_us.py` 신규 — `invest_kr_fundamentals_snapshots.py`를 베이스로, CLI/게이트/락만 교체:

```python
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.client.schemas.schedules import CronSchedule

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from robin_automation.news_ingestor import redact_secrets  # noqa: E402

AUTO_TRADER_COMMON_SH = "/Users/mgh3326/services/auto_trader/scripts/common.sh"
DEFAULT_AUTO_TRADER_ENV_FILE = "/Users/mgh3326/services/auto_trader/shared/.env.prod.native"
LOCK_DIR = "/tmp/rob508_us_market_valuation_snapshots.lockdir"
COMMIT_GATE_KEY = "MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED"
BUILD_RE = re.compile(
    r"built\s+(?P<snapshots>\d+)\s+valuation snapshots\s+"
    r"for\s+(?P<symbols>\d+)\s+(?P<market>\w+)\s+symbols\s+"
    r"\(dry_run=(?P<dry_run>True|False),\s+batches=(?P<batches>\d+)\)"
)


def _tail(text: str, max_lines: int = 80, max_chars: int = 10000) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def _env_file_commit_gate_enabled() -> bool:
    direct = os.getenv(COMMIT_GATE_KEY)
    if direct is not None:
        return direct.strip().lower() in {"1", "true", "yes", "on"}
    env_file = Path(
        os.getenv("AUTO_TRADER_ENV_FILE")
        or os.getenv("ENV_FILE")
        or DEFAULT_AUTO_TRADER_ENV_FILE
    )
    try:
        for line in env_file.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(f"{COMMIT_GATE_KEY}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value.lower() in {"1", "true", "yes", "on"}
    except FileNotFoundError:
        return False
    return False


def _parse_summary(stdout: str, stderr: str) -> dict[str, Any]:
    combined = "\n".join(part for part in (stdout, stderr) if part)
    summary: dict[str, Any] = {}
    match = BUILD_RE.search(combined)
    if match:
        summary.update(
            {
                "market": match.group("market"),
                "snapshots_built": int(match.group("snapshots")),
                "symbols_resolved": int(match.group("symbols")),
                "dry_run": match.group("dry_run") == "True",
                "batches": int(match.group("batches")),
            }
        )
    committed_match = re.search(r"committed\s+(\d+)\s+rows", combined)
    if committed_match:
        summary["committed_rows"] = int(committed_match.group(1))
    if "--dry-run: no rows written" in combined:
        summary["committed_rows"] = 0
    block_match = re.search(r"COMMIT BLOCKED:\s+(.+)", combined)
    if block_match:
        summary["block_reason"] = block_match.group(1).strip()
    return summary


@task(retries=1, retry_delay_seconds=300)
def run_us_market_valuation_snapshot_build(
    *,
    all_symbols: bool,
    limit: int | None,
    batch_size: int,
    concurrency: int,
    common_stocks_only: bool,
    with_high_52w_date: bool,
    commit_with_gate: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    args = [
        "uv",
        "run",
        "python",
        "-m",
        "scripts.build_market_valuation_snapshots",
        "--market",
        "us",
        "--batch-size",
        str(batch_size),
        "--concurrency",
        str(concurrency),
    ]
    if all_symbols:
        args.append("--all")
    elif limit is not None:
        args.extend(["--limit", str(limit)])
    if common_stocks_only:
        args.append("--common-stocks-only")
    if with_high_52w_date:
        args.append("--with-high-52w-date")

    commit_gate_enabled = _env_file_commit_gate_enabled()
    if commit_with_gate and commit_gate_enabled:
        args.append("--commit")

    quoted_args = " ".join(subprocess.list2cmdline([arg]) for arg in args)
    command = (
        "set -euo pipefail; "
        f"if ! mkdir {LOCK_DIR}; then "
        "echo 'another US market valuation snapshot run is active'; exit 0; "
        "fi; "
        f"trap 'rmdir {LOCK_DIR}' EXIT; "
        f"source {AUTO_TRADER_COMMON_SH} >/dev/null; "
        "export PUBLIC_API_PATHS='[]'; "
        "export PYTHONPATH='.'; "
        "cd \"$AUTO_TRADER_CURRENT\"; "
        f"{quoted_args}"
    )
    completed = subprocess.run(
        ["/bin/bash", "-lc", command],
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = redact_secrets(completed.stdout or "")
    stderr = redact_secrets(completed.stderr or "")
    summary = _parse_summary(stdout, stderr)
    result = {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "summary": summary,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "all_symbols": all_symbols,
        "limit": limit,
        "common_stocks_only": common_stocks_only,
        "with_high_52w_date": with_high_52w_date,
        "commit_with_gate": commit_with_gate,
        "commit_gate_enabled": commit_gate_enabled,
    }
    # CLI exit 2 = PartialCommitBlocked (coverage floor) — 실패로 표면화해
    # Prefect Error Watchdog가 잡게 한다 (silent-success 금지).
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


@flow(name="US Market Valuation Snapshots", log_prints=True)
def us_market_valuation_snapshots_flow(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    batch_size: int = 100,
    concurrency: int = 4,
    common_stocks_only: bool = True,
    with_high_52w_date: bool = False,
    commit_with_gate: bool = True,
    timeout_seconds: int = 10800,
) -> dict[str, Any]:
    logger = get_run_logger()
    result = run_us_market_valuation_snapshot_build(
        all_symbols=all_symbols,
        limit=limit,
        batch_size=batch_size,
        concurrency=concurrency,
        common_stocks_only=common_stocks_only,
        with_high_52w_date=with_high_52w_date,
        commit_with_gate=commit_with_gate,
        timeout_seconds=timeout_seconds,
    )
    logger.info(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def deploy_daily_us_market_valuation_snapshots(*, paused: bool = True) -> object:
    """ROB-508: US 펀더멘털 프리셋 freshness용 일일 valuation 갱신.

    08:30 KST 화–토 = US 정규장 마감(06:00/07:00 KST) 후, 06:10 KST US invest
    screener 빌드(같은 yfinance를 침)와의 동시 실행을 피하는 슬롯. 첫 등록은
    paused=True — 수동 dry-run flow run 검증 후 paused=False로 재배포.
    """
    return us_market_valuation_snapshots_flow.from_source(
        source=str(PROJECT_ROOT),
        entrypoint=(
            "flows/auto_trader/market_valuation_snapshots_us.py"
            ":us_market_valuation_snapshots_flow"
        ),
    ).deploy(
        name="daily-post-us-close",
        work_pool_name="pyri-process",
        schedule=CronSchedule(cron="30 8 * * 2-6", timezone="Asia/Seoul"),
        parameters={
            "all_symbols": True,
            "limit": None,
            "batch_size": 100,
            "concurrency": 4,
            "common_stocks_only": True,
            "with_high_52w_date": False,
            "commit_with_gate": True,
            "timeout_seconds": 10800,
        },
        paused=paused,
        build=False,
        push=False,
        tags=["auto-trader", "market-valuation", "snapshots", "us", "freshness", "rob-508"],
    )


if __name__ == "__main__":
    us_market_valuation_snapshots_flow(
        all_symbols=False,
        limit=5,
        commit_with_gate=False,
        timeout_seconds=900,
    )
```

- [ ] **Step B1-4: 테스트 통과 확인**

Run: `cd /Users/mgh3326/services/prefect && uv run pytest tests/test_market_valuation_snapshots_us.py -v`
Expected: 3 PASS

- [ ] **Step B1-5: 실제 CLI 출력으로 BUILD_RE 검증** (regex가 추정이므로 실측 필수)

```bash
cd /Users/mgh3326/work/auto_trader.rob-508 && uv run python -m scripts.build_market_valuation_snapshots --market us --limit 2 2>&1 | tail -20
```

출력의 `built ... valuation snapshots for ...` 라인이 BUILD_RE와 매치하는지 확인, 불일치 시 regex와 테스트 fixture 문자열을 실측값으로 수정.

- [ ] **Step B1-6: 커밋**

```bash
cd /Users/mgh3326/services/prefect
git add flows/auto_trader/market_valuation_snapshots_us.py tests/test_market_valuation_snapshots_us.py
git commit -m "feat(rob-508): US market valuation snapshots daily flow (paused-by-default deploy)"
```

### Task B2: `us_fundamentals_snapshots` 주간 flow

`financial_fundamentals_snapshots` US(yfinance 연간 손익)는 분기 단위로나 바뀌므로 **주간(일요일 09:00 KST)** 갱신이면 충분. 구조는 B1과 동일 — 차이점만 명시:

**Files:**
- Create: `/Users/mgh3326/services/prefect/flows/auto_trader/us_fundamentals_snapshots.py`
- Test: `/Users/mgh3326/services/prefect/tests/test_us_fundamentals_snapshots.py`

- [ ] **Step B2-1: B1 파일을 복제해 다음만 교체**
  - CLI 모듈: `scripts.build_us_fundamentals_snapshots` (`--market` 플래그 없음 — us 고정; `--batch-size`/`--common-stocks-only`/`--with-high-52w-date` 플래그 없음 — 제거)
  - flow 파라미터에 `include_dividends: bool = True` 추가 → `--with-dividends` (US steady_dividend/future_dividend_king 후속 대비; 비용 동일 yfinance call 내 포함)
  - `LOCK_DIR = "/tmp/rob508_us_fundamentals_snapshots.lockdir"`
  - flow name: `"US Financial Fundamentals Snapshots"` / deploy name: `"weekly-sunday"`
  - `CronSchedule(cron="0 9 * * 0", timezone="Asia/Seoul")`
  - `_parse_summary`의 BUILD_RE는 **Step B2-2 실측 후 작성** (이 CLI의 출력 포맷은 market_valuation과 다를 수 있음)
  - tags: `["auto-trader", "fundamentals", "snapshots", "us", "rob-508"]`

- [ ] **Step B2-2: 실측 출력 확보 + 파서 테스트 작성·통과**

```bash
cd /Users/mgh3326/work/auto_trader.rob-508 && uv run python -m scripts.build_us_fundamentals_snapshots --limit 2 2>&1 | tail -25
```

출력 summary 라인으로 BUILD_RE/테스트 작성 → `uv run pytest tests/test_us_fundamentals_snapshots.py -v` PASS.

- [ ] **Step B2-3: 커밋**

```bash
git add flows/auto_trader/us_fundamentals_snapshots.py tests/test_us_fundamentals_snapshots.py
git commit -m "feat(rob-508): weekly US financial fundamentals snapshots flow"
```

### Task B3: 로컬 스모크 + PR

- [ ] **Step B3-1: 전체 테스트 + 로컬 dry-run 스모크**

```bash
cd /Users/mgh3326/services/prefect && uv run pytest tests/ -v
uv run python flows/auto_trader/market_valuation_snapshots_us.py   # __main__: limit 5, gate OFF → dry-run
```

Expected: 테스트 전부 PASS; 스모크 result에 `"commit_gate_enabled": false`, `"dry_run": true`(또는 commit 플래그 미부착), `ok: true`.

- [ ] **Step B3-2: PR 생성** (이 레포 관례 확인 — 원격 있으면 PR, 없으면 main 직머지 전 사용자 확인)

```bash
git push -u origin feature/rob-508-market-valuation-us-flow
gh pr create --base main --title "feat(rob-508): US market valuation + fundamentals snapshot flows" --body "..."
```

---

# Part C — Operator 활성화 체크리스트 (머지 후, 코드 밖)

플랜 실행자가 하는 일이 아님 — Linear ROB-508 코멘트로 게시하고 user가 수행:

1. `[ ]` auto_trader PR 머지 + 프로덕션 배포 (`$AUTO_TRADER_CURRENT` 갱신 — prefect flow는 배포된 체크아웃의 CLI를 호출하므로 A2/A3 표시 보강은 배포 후 반영)
2. `[ ]` 프로덕션 env 파일(`/Users/mgh3326/services/auto_trader/shared/.env.prod.native`)에 `MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED=true` 추가 (없으면 flow는 영구 dry-run)
3. `[ ]` prefect 레포에서 deployment 등록: `uv run python -c "from flows.auto_trader.market_valuation_snapshots_us import deploy_daily_us_market_valuation_snapshots as d; d(paused=True)"` (us_fundamentals 동형)
4. `[ ]` 수동 flow run (dry-run 파라미터: `all_symbols=False, limit=20, commit_with_gate=False`) → summary에서 coverage/필드 커버리지 확인
5. `[ ]` `paused=False` 재배포 (`d(paused=False)`)
6. `[ ]` 다음 거래일 아침 검증: US `undervalued_growth` 스크리너 호출 → `freshness.primary.snapshotDate` == 직전 거래일, `dataState: "fresh"`, 행 `priceLabel` ≠ `"-"` — **이 확인까지가 ROB-508 Done 게이트**

---

## 결정 사항 (구현자가 재논의하지 말 것 — 근거 포함)

| 결정 | 근거 |
|---|---|
| Prefect로 cadence (TaskIQ 아님) | 운영 스케줄러가 Prefect임을 라이브 확인 (`prefect deployment ls`). auto_trader의 KR TaskIQ 태스크는 default-off로 잠들어 있고, 이번에 깨우지 않음 |
| auto_trader에 `app/flows/` 래퍼 **안 만듦** | 기존 prefect flow들은 전부 CLI subprocess 호출 패턴 — `app/flows/`는 import용일 뿐 프로덕션 미사용. YAGNI |
| 스케줄 08:30 KST 화–토 | US 마감(06~07 KST) 이후 + 06:10 US invest_screener 빌드(동일 yfinance 의존)와 시간 분리. 가격 join은 read-time이라 빌드 순서가 정합성에 영향 없음 |
| `with_high_52w_date=False` 기본 | 심볼당 추가 OHLC call로 heavy (ROB-434 결정 계승). undervalued_breakout US의 date-recency는 sort tiebreak이라 None 허용. operator가 ad-hoc run에서 켤 수 있게 flow param으로만 노출 |
| us_fundamentals는 주간 | 연간 손익 데이터 — 일일 갱신은 yfinance 부하 대비 무의미 |
| 에러 응답은 warning 텍스트로 (스키마 필드 추가 없음) | `ScreenerResultsResponse` 스키마 변경 없이 AC 충족. MCP/HTTP 소비자 모두 warnings는 이미 표면화됨 |
| KR 표시 공백 스코프 제외 | ROB-428 PR-B에서 KR display는 tvscreener 로더로 이미 close 채움 — 잔여 KR 갭은 ROB-428 트랙 |
| exit 2 (PartialCommitBlocked) → flow 실패 | clean-success 위장 금지 (ROB-305 §4 원칙 계승). Prefect Error Watchdog가 잡음 |

## Self-Review 결과

- AC① freshness → Part B + Part C 6 (cadence + 검증), AC② 유효 목록 → Task A1, AC③ 가격 표기 → Task A2+A3. 커버 완료.
- 이슈의 "enrichment 가격으로라도 표기" 제안 대신 snapshot join을 선택 — analysisContext.consensus는 별도 외부 호출 의존이라 결정적이지 않음. join이 같은 표면의 기존 패턴(high_yield_value)과 일치.
- 테스트 fixture 세부(db_session 헬퍼명 등)는 각 테스트 파일의 기존 패턴을 따르도록 명시 — 실행자는 파일을 먼저 읽고 동일 패턴 사용.
- BUILD_RE는 실측 검증 스텝(B1-5, B2-2)을 포함해 추정-드리프트 차단.
