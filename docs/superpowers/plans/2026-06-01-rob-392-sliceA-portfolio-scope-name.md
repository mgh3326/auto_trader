# ROB-392 Slice A — portfolio NAV scope 라벨 + code-as-name 매핑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** portfolio NAV의 scope를 명시 라벨링하고(수치 불변), KR holdings/reference 행의 code-as-name(예: 035420)을 universe 이름으로 폴백 해석한다.

**Architecture:** portfolio collector payload에 `nav_scope`/`nav_scope_label`(additive) 추가 → portfolio_journal stage가 byte-identical summary는 그대로 두고 key_points에 라벨 surface. 이름은 `display_name`이 falsy/코드와 동일한 KR 행에 한해 `get_kr_names_by_symbols`(DB universe)로 폴백 해석(실패 시 코드 유지). read-only, 신규 HTTP surface 없음.

**Tech Stack:** Python 3.13, pytest (`uv run pytest`), 기존 `snapshot_backed/collectors/portfolio.py` + `investment_stages/stages/portfolio_journal.py` + `kr_symbol_universe_service`.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-392-sliceA-portfolio-scope-name-design.md`

---

## File Structure

- **Modify** `app/services/action_report/snapshot_backed/collectors/portfolio.py` — KIS-live payload에 `nav_scope`/`nav_scope_label`; KR 이름 폴백 헬퍼 + 호출.
- **Modify** `app/services/investment_stages/stages/portfolio_journal.py` — `nav_scope_label`을 key_points에 surface.
- **Test** `tests/services/action_report/snapshot_backed/test_collectors.py` — NAV scope 라벨 + 이름 폴백 헬퍼.
- **Test** `tests/services/investment_stages/stages/test_portfolio_journal.py` — key_points scope 라벨 surface.

> 실행 시 모든 명령은 worktree `/Users/mgh3326/work/auto_trader.rob-392`에서 `uv run` 으로 수행.
> `get_kr_names_by_symbols(symbols: list[str], db: AsyncSession | None = None) -> dict[str, str]`
> (`app/services/kr_symbol_universe_service.py:484`) — 누락/비활성 심볼은 결과에서 생략.

---

## Task 1: portfolio NAV scope 라벨 (증상4)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/portfolio.py` (KIS-live payload, 라인 ~420-436)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가. 기존 KIS-live 성공 테스트가 어떻게 collector를 구성하는지(`test_portfolio_v2_kr_kis_live_success_populates_kis_primary`, 라인 ~266)를 참고해 동일 픽스처를 재사용한다:

```python
@pytest.mark.asyncio
async def test_kr_kis_live_payload_carries_nav_scope_label():
    # Reuse the same fixture wiring as
    # test_portfolio_v2_kr_kis_live_success_populates_kis_primary.
    payload = await _run_kr_kis_live_portfolio_collect()  # helper from that test
    assert payload["primary_source"] == "kis"
    assert payload["nav_scope"] == "kis_primary_sellable"
    assert "ISA/Toss" in payload["nav_scope_label"]
    # 수치 회귀: holdings/count는 라벨 추가와 무관하게 유지.
    assert payload["count"] == len(payload["holdings"])
```

> 만약 `_run_kr_kis_live_portfolio_collect` 같은 공유 헬퍼가 없으면, `test_portfolio_v2_kr_kis_live_success_populates_kis_primary`(라인 ~266)의 collector 구성·`collect()` 호출을 그대로 복사해 `payload = results[0].payload_json`을 얻은 뒤 위 단언만 적용한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k nav_scope_label -v`
Expected: FAIL — `payload["nav_scope"]` KeyError.

- [ ] **Step 3: Add nav_scope fields to the KIS-live payload**

`app/services/action_report/snapshot_backed/collectors/portfolio.py`, KIS-live `payload = {...}`(라인 ~420)에 `sellable_summary` 뒤·`provenance` 앞에 두 키 추가:

```python
            "sellable_summary": sellable_summary,
            "nav_scope": "kis_primary_sellable",
            "nav_scope_label": (
                "NAV는 KIS 실거래(매도가능) 보유 + 현금 기준 · "
                "ISA/Toss 참조분(reference_holdings)은 제외"
            ),
            "provenance": {
```

> 이 두 키는 KIS-live KR 경로 payload에만 추가한다(수치 로직·다른 경로 불변).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k nav_scope_label -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/portfolio.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-392): label portfolio NAV scope (kis_primary_sellable)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: portfolio_journal stage가 nav_scope_label을 key_points에 surface

**Files:**
- Modify: `app/services/investment_stages/stages/portfolio_journal.py` (`run`, 라인 ~138 `key_points` 직후)
- Test: `tests/services/investment_stages/stages/test_portfolio_journal.py`

- [ ] **Step 1: Write the failing test**

`tests/services/investment_stages/stages/test_portfolio_journal.py` 끝에 추가. 기존 KR 성공 테스트의 portfolio payload 픽스처를 참고해 `nav_scope_label`을 넣고 key_points에 노출되는지 확인:

```python
@pytest.mark.asyncio
async def test_portfolio_journal_surfaces_nav_scope_label_in_key_points():
    # 기존 KR 케이스와 동일하게 StageContext를 구성하되 portfolio payload에
    # nav_scope_label을 추가한다(아래는 그 핵심만; 기존 헬퍼/픽스처 재사용).
    label = "NAV는 KIS 실거래(매도가능) 보유 + 현금 기준 · ISA/Toss 참조분(reference_holdings)은 제외"
    context = _kr_context_with_portfolio_payload(
        {
            "holdings": [{"ticker": "005930", "value_krw": 1_000_000}],
            "cash": {"krw": 500_000},
            "buying_power": {"krw": 500_000},
            "nav_scope_label": label,
        }
    )
    artifact = await PortfolioJournalStage().run(context)
    assert label in artifact.key_points
```

> `_kr_context_with_portfolio_payload` / `PortfolioJournalStage` 임포트·StageContext 구성은 이 테스트 파일의 기존 KR 테스트(예: NAV/summary를 검증하는 케이스)의 픽스처를 그대로 재사용한다. 핵심 단언은 `label in artifact.key_points` 하나다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -k surfaces_nav_scope_label -v`
Expected: FAIL — label이 key_points에 없음.

- [ ] **Step 3: Surface the label in key_points (summary는 불변)**

`app/services/investment_stages/stages/portfolio_journal.py`, `run`의 `key_points = [...]`(라인 ~138) 직후에 추가:

```python
        key_points = [e.get("thesis", "") for e in entries[:5] if e.get("thesis")]
        nav_scope_label = payload.get("nav_scope_label")
        if isinstance(nav_scope_label, str) and nav_scope_label:
            key_points = [nav_scope_label, *key_points]
```

> byte-identical KR `summary` 문자열은 변경하지 않는다(라벨은 key_points로만 노출).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -k surfaces_nav_scope_label -v`
Expected: PASS.

- [ ] **Step 5: Run the full portfolio_journal suite (byte-identical summary 회귀)**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -v`
Expected: 모두 PASS (summary contract 무손상).

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_stages/stages/portfolio_journal.py tests/services/investment_stages/stages/test_portfolio_journal.py
git commit -m "feat(ROB-392): surface NAV scope label in portfolio_journal key_points

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: KR code-as-name 폴백 (증상5)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/portfolio.py` (모듈 헬퍼 `_apply_kr_name_fallback` + KR 경로 호출)
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

- [ ] **Step 1: Write the failing test (pure helper)**

`tests/services/action_report/snapshot_backed/test_collectors.py` 끝에 추가:

```python
def test_apply_kr_name_fallback_fills_code_as_name_rows():
    from app.services.action_report.snapshot_backed.collectors.portfolio import (
        _apply_kr_name_fallback,
    )

    rows = [
        {"ticker": "035420", "display_name": None},      # missing
        {"ticker": "035720", "display_name": "035720"},  # code-as-name
        {"ticker": "005930", "display_name": "삼성전자"},  # already good
    ]
    _apply_kr_name_fallback(rows, {"035420": "NAVER", "035720": "카카오"})
    assert rows[0]["display_name"] == "NAVER"
    assert rows[1]["display_name"] == "카카오"
    assert rows[2]["display_name"] == "삼성전자"  # untouched


def test_apply_kr_name_fallback_keeps_code_when_unresolved():
    from app.services.action_report.snapshot_backed.collectors.portfolio import (
        _apply_kr_name_fallback,
    )

    rows = [{"ticker": "999999", "display_name": None}]
    _apply_kr_name_fallback(rows, {})  # lookup returned nothing
    assert rows[0]["display_name"] is None  # no fabricated name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k apply_kr_name_fallback -v`
Expected: FAIL — `ImportError: cannot import name '_apply_kr_name_fallback'`.

- [ ] **Step 3: Add the pure helper**

`app/services/action_report/snapshot_backed/collectors/portfolio.py`, 모듈 레벨(다른 `_*_to_dict` 헬퍼 근처)에 추가:

```python
def _apply_kr_name_fallback(
    rows: list[dict[str, Any]], name_map: dict[str, str]
) -> None:
    """Fill ``display_name`` for rows whose name is missing or equals the code.

    In-place. A row is fixed only when ``name_map`` has a real name for its
    ``ticker``; otherwise the code is kept (never fabricate a name).
    """
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str):
            continue
        current = row.get("display_name")
        is_code_as_name = not current or current == ticker
        if is_code_as_name and ticker in name_map:
            row["display_name"] = name_map[ticker]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k apply_kr_name_fallback -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire the fallback into the KR collect path**

`app/services/action_report/snapshot_backed/collectors/portfolio.py` 상단 import에 추가:

```python
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
```

KIS-live `payload = {...}` 구성 **직전**(라인 ~420, `holdings_out`/`reference_holdings`가 확정된 지점), KR 시장일 때만 이름 폴백을 적용:

```python
        if request.market == "kr":
            name_rows = [*holdings_out, *reference_holdings]
            need = sorted(
                {
                    r["ticker"]
                    for r in name_rows
                    if isinstance(r.get("ticker"), str)
                    and (not r.get("display_name") or r.get("display_name") == r["ticker"])
                }
            )
            if need:
                try:
                    name_map = await get_kr_names_by_symbols(need, db=self._session)
                except Exception:  # noqa: BLE001 — name fallback is best-effort
                    name_map = {}
                _apply_kr_name_fallback(name_rows, name_map)
```

> `self._session`은 collector가 이미 보유. lookup 실패는 fail-open(코드 유지). `holdings_out`/`reference_holdings`의 dict는 참조로 공유되므로 in-place 수정이 payload에 반영된다.

- [ ] **Step 6: Run the portfolio collector suite for regressions**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k "portfolio or apply_kr_name or nav_scope" -v`
Expected: 모두 PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/portfolio.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-392): fallback-resolve KR code-as-name portfolio rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 전체 검증 + lint + PR + 핸드오프

**Files:** 없음 (검증 전용)

- [ ] **Step 1: Run the touched suites**

Run:
```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py tests/services/investment_stages/stages/test_portfolio_journal.py -v
```
Expected: 모두 PASS (신규 + 기존 회귀 없음, 특히 byte-identical KR summary).

- [ ] **Step 2: Lint (CLAUDE.md 게이트)**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 둘 다 통과. (format 위반이면 `uv run ruff format app/ tests/` 후 재확인·커밋 — `ruff check`만으로는 lint job이 떨어진다.)

- [ ] **Step 3: Push branch and open PR (base: main)**

Run:
```bash
git push -u origin rob-392
gh pr create --base main --title "fix(ROB-392) Slice A: portfolio NAV scope 라벨 + KR code-as-name 매핑" --body "$(cat <<'EOF'
## 요약
ROB-392 **Slice A** (증상4 + 증상5만; 증상1/2는 별도 이슈, 증상3은 by-design Hermes).

1. **NAV scope 라벨 (증상4)** — portfolio collector payload에 `nav_scope="kis_primary_sellable"` + `nav_scope_label`(ISA/Toss 참조분 제외 명시) 추가. portfolio_journal stage가 byte-identical summary는 그대로 두고 key_points에 라벨 surface. **수치 로직 불변** — NAV는 여전히 KIS primary holdings + cash(reference 미합산, ROB-297 유지).
2. **KR code-as-name 매핑 (증상5)** — holdings/reference 행의 `display_name`이 null이거나 코드와 같으면 `get_kr_names_by_symbols`(DB universe)로 폴백 해석. 해석 실패 시 코드 유지(거짓 이름 금지).

## 증상1 전제 정정 (integrity)
증상1은 "symbol 스냅샷에 KIS RSI/컨센서스/레벨이 있는데 stage가 평탄화"라고 했으나, 실측 결과 `SymbolSnapshotCollector`는 그 evidence를 **전혀 담지 않음**(quote만; evidence는 `analyze_stock_batch` 생성, 미캡처). "승격"이 아니라 "캡처"가 필요한 큰 작업 → **별도 이슈로 분리**.

## 테스트
- `tests/services/action_report/snapshot_backed/test_collectors.py` — NAV scope 라벨 + 이름 폴백 헬퍼(해석 성공/실패)
- `tests/services/investment_stages/stages/test_portfolio_journal.py` — key_points scope 라벨 surface (summary 회귀 무손상)

## 안전 경계
read-only. broker/order/watch mutation 없음. 신규 HTTP surface 없음(DB universe lookup). NAV 수치/merge 로직 불변. DB 마이그레이션 없음(payload additive). deterministic이 LLM 합성 대체 안 함(라벨/이름만).

## 잔여 (handoff)
- 증상1/2 (KIS RSI/consensus/level을 symbol evidence로 캡처) = 별도 큰 이슈 (collector enrichment + 신규 HTTP surface 검토).
- 증상3 (news 합성) = by-design Hermes compose.
- US/crypto 이름 폴백, symbol stage key_points 코드 노출(universe-sync 갭) = 비목표.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL 출력. (출력된 URL 확인 후에만 PR 번호 인용.)

- [ ] **Step 4: ROB-394 handoff 코멘트 + 증상1/2 별도 이슈 메모**

ROB-394에 ROB-392 Slice A 결과(PR 링크 + 검증 + 증상1 전제 정정 + 증상1/2 별도 이슈 필요)를 남기고, 다음 순서가 ROB-391임을 명시한다. (Linear `save_comment`.)

---

## Self-Review

**Spec coverage:**
- 변경1 NAV scope 라벨 → Task 1(payload) + Task 2(stage surface). ✅
- 변경2 code-as-name 폴백 → Task 3(helper + wiring). ✅
- 테스트 T1/T2/T3/T4 → Task1/Task2/Task3(2건). ✅
- 안전 경계(read-only, no HTTP, NAV 불변, no migration) → payload/stage additive, 수치 미변경. ✅
- 비목표(증상1/2/3, US/crypto 이름, symbol stage) → 미구현, Task4 handoff 명시. ✅

**Placeholder scan:** 코드 step은 실제 코드 포함. Task1/2 Step1은 기존 픽스처 재사용을 명시하되 핵심 단언은 구체적(픽스처 헬퍼명은 파일 기존 테스트에서 확정). ✅

**Type consistency:** `_apply_kr_name_fallback(rows: list[dict], name_map: dict[str,str]) -> None`(Task3 정의 ↔ 호출 ↔ T3/T4); payload 키 `nav_scope`/`nav_scope_label`(Task1 정의 ↔ T1 ↔ Task2 소비 ↔ T2); `get_kr_names_by_symbols(need, db=self._session)`(시그니처 `(symbols, db=None)`와 일치). ✅
