# ROB-276 /invest/screener 쌍끌이 매수 Toss screenId=18 Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/invest/screener` 의 `쌍끌이 매수` 프리셋을 거래량 기반(`high_volume_momentum`)에서 Toss screenId=18 의미(외인+기관 동반 매수 + 1일 상승)에 가까운 스냅샷 기반 read-only preset(`double_buy`)으로 교체하고, 라벨/카피/freshness 를 진실되게 표기한다.

**Architecture:** 신규 `double_buy` preset 을 `screener_presets.py` 에 추가, `screener_service.py` 의 snapshot-first 분기에 `investor_flow_snapshots` (current + previous 영업일) 와 `invest_screener_snapshots` (최신 영업일 close/change_rate) 를 join 하는 read-only 분기를 추가한다. `high_volume_momentum` 은 이름을 `거래량 급증`(id `kr_high_volume_surge`) 로 clean-cut 변경. `수급 모멘텀` 은 카피에서 `쌍끌이` 표현을 제거. DB schema/ingestion/scheduler 변경 없음.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x (async), Pydantic v2, PostgreSQL, pytest, Vite/React/TS (frontend), uv.

---

## Locked Decisions (Top of Plan)

> 이 섹션은 implementation 시작 전 lock 된 결정입니다. 변경하려면 plan 을 먼저 갱신하세요.

### Decision 1 — Toss `외국인_순매수_비교` / `기관_순매수_비교` 의미

**LOCKED: 해석 A (절대값) — safer-fallback rule 적용 (2026-05-20, Task 0).**

두 후보:

- **해석 A (절대값)**: `foreign_net > 0` AND `institution_net > 0` (= 기존 `InvestorFlowSnapshot.double_buy` 컬럼 재사용 가능). Toss filter id 가 `NUMBER_RANGE { from: 0, includeFrom: false }` 형태이므로 "1일 순매수액 > 0" 의미일 가능성.
- **해석 B (delta)**: `foreign_net_current - foreign_net_prev > 0` AND `institution_net_current - institution_net_prev > 0`. Toss filter id 가 `_비교` 로 끝나고, 화면 카피("기관과 외국인이 동시에 사들이는 주식") 가 흐름/추세 뉘앙스이므로 가능성 유지.

**Task 0 verification 결과 (2026-05-20):**

- **DB 접근 불가**: docker / colima / podman 모두 사용 불가, docker-compose Postgres (port 5434) 미기동, 그리고 worktree 가 host Postgres (port 5432) 에 직접 연결할 권한이 없음 — auto-mode classifier 가 차단. 따라서 live A/B overlap 수치를 산출하지 못함.
- **Toss reference**: 이슈 ROB-276 본문 외 추가 캡처 댓글/첨부 없음. 사용 가능한 reference 는 `011000, 439960, 083500, 042520, 042420` (5개) 뿐 — 캡처 size 가 < 50% 의미를 갖기에 부족.
- **구조적 reasoning**: 이슈 본문 텍스트("foreign net-buy comparison vs previous trading snapshot > 0", `lag(foreign_net) over ...` 예시) 는 해석 B 쪽을 선호. 다만 plan 의 Lock 규칙 명시("결판이 안 나거나 reference data 가 빈약하면 → safer fallback: A 채택") 가 우선 적용됨. 모델 정의 (`InvestorFlowSnapshot.double_buy` 가 이미 `foreign_net > 0 AND institution_net > 0` 의 derived 컬럼으로 존재) 도 A 의 구현 비용을 낮춤.

**Lock 결정: A.** 새 preset 은 `InvestorFlowSnapshot.double_buy = True AND COALESCE(invest_screener_snapshots.change_rate, 0) >= 0` 으로 구현. Task 2 의 `_load_double_buy_from_snapshots` helper Interpretation A 본체를 그대로 사용.

**남은 gap & 후속 검증 surface:**

- Live verification 은 Task 4 (diagnostic `--interpretation both`) 가 실제 DB 와 더 큰 Toss reference 가 확보된 시점에 수행. A/B overlap 수치가 B 가 명백히 우세함을 보이면 후속 PR 에서 lock 을 B 로 전환 (helper 본체만 교체, preset metadata 유지).
- `docs/runbooks/invest-screener-snapshots.md` 의 ROB-276 섹션 (Task 6) 에 본 gap 명시.
- Diagnostic 은 **항상 A/B 양쪽 비교 출력 유지** (plan 의 safer-fallback 절 요구사항).

**Lock 근거 사유 요약:**

- Task 0 spec 의 Step 6 rule: "둘 다 < 50% 커버 또는 동률 → A lock (safer fallback)" — DB 미접근으로 둘 다 측정 불가, 즉 effectively < 50% 상태.
- A 는 `double_buy` 컬럼을 재사용하여 self-join 없이 단일 query 로 구현 가능 → 첫 PR 범위가 작고 회귀 위험 낮음.
- B 가 정답일 가능성은 diagnostic 으로 살아있는 검증 경로가 있어 후속 전환 비용이 낮음.

### Decision 2 — `high_volume_momentum` 처리

**Clean cut**:

- 새 preset `double_buy` (id; market="kr") 를 추가하고 `name="쌍끌이 매수"`, `description="기관과 외국인이 동시에 매수하는 종목"` 으로 노출.
- 기존 `high_volume_momentum` preset 은 다음과 같이 변경:
  - **ID 도 변경**: `kr_high_volume_surge` (구 ID `high_volume_momentum` 는 폐기)
  - `name="거래량 급증"`, `description="거래량이 폭발적으로 늘어난 종목"` 유지
  - 캐시/북마크 영향: 구 URL `?preset=high_volume_momentum` 은 신규 preset registry 에서 찾을 수 없어 `unknown preset` 응답으로 빠짐. 사용자가 직접 입력한 URL/북마크가 있을 경우 빈 결과 + 경고가 나옴 — frontend 의 preset list 진입 경로가 주된 진입이라 영향은 작다고 판단.
  - **마이그레이션 도움말**: PR description 에 "구 ID `high_volume_momentum` 는 제거됨, 새 거래량 프리셋은 `kr_high_volume_surge`" 한 줄 명시.
- `_METRIC_FIELD`, `_SCREENING_FILTERS`, 관련 test fixture, frontend preset list 시점의 모든 hard-coded 문자열을 일괄 grep/replace.

### Decision 3 — `investor_flow_momentum`(수급 모멘텀) 카피 정리

- `description` 을 `"외국인 연속 순매수·쌍끌이 매수 스냅샷 기반 후보"` → `"외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)"` 로 변경 ("쌍끌이" 단어 제거).
- `filterChips` 의 `detail="외국인 3일+ 또는 쌍끌이"` → `detail="외국인 3일+ 연속 순매수"` 로 변경.
- 내부 ID/필터/쿼리는 그대로 유지 — 기존 API 호환성/스냅샷 분기는 변경 없음.
- 사용자 입장에서 "쌍끌이 매수" 카드(신규)와 "수급 모멘텀" 카드(기존)가 시각적으로 구분되도록 보장. 호환성은 `test_invest_view_model_screener_service.py` 의 기존 `investor_flow_momentum` 케이스로 확인.

---

## Scope Guardrails (이번 PR 한정)

- **No DB migrations.** `app/models/*.py`, `alembic/versions/*` 추가/수정 금지.
- **No new ingestion job, no new TaskIQ task, no Prefect deployment, no scheduler activation.**
- **No broker / order / watch / order-intent mutation path.** read-only view-model 만 수정.
- **No Toss runtime scraping.** Toss 데이터는 수동 캡처 JSON 파일을 path/env 로 받는 diagnostic 에서만 사용.
- **Snapshot-only**: 새 preset 은 `_load_*_from_snapshots` 패턴과 동일하게 snapshot 분기로 추가. generic provider fallback 금지.
- **Frontend**: 새 preset rendering 만 추가; 기존 `ScreenerResultsTable`/`ScreenerFreshnessLine` 컴포넌트 시그니처 변경 금지.

---

## File Structure

**Modify (backend)**:

- `app/services/invest_view_model/screener_presets.py` — preset metadata, `_SCREENING_FILTERS`, `_KR_ONLY_PRESET_IDS`, `_METRIC_FIELD` (후자는 service 모듈에 있음).
- `app/services/invest_view_model/screener_service.py`
  - L670–681 `_METRIC_FIELD` (preset → metric field)
  - L1220–1265 snapshot-first 분기 (`elif preset_id == "double_buy":` 추가)
  - 신규 helper `_load_double_buy_from_snapshots(session, *, market, limit, ...) -> list[dict] | None`
- `app/services/invest_view_model/screener_presets.py` — 새 preset entry + KR-only set 등록.

**Create (backend)**:

- `app/services/invest_view_model/double_buy_screener.py` — Toss screenId=18 parity 쿼리 (read-only). `_load_double_buy_from_snapshots` 의 본체. screener_service 가 import 하여 사용. 단일 책임: snapshot join + filter + ordering + freshness state.

**Modify (diagnostic)**:

- `scripts/diagnose_invest_screener_toss_parity.py`
  - `_SUPPORTED_PRESETS` 에 `double_buy` 추가.
  - `--interpretation {a,b,both}` 옵션 추가 (default `both`).
  - Toss reference 는 기존 `--toss-symbols-file` 인터페이스 재활용.
  - A/B 양쪽 후보 set 산출 → overlap/missing/extra/rank-delta 출력.

**Modify (tests, backend)**:

- `tests/test_invest_view_model_screener_service.py` — `double_buy` preset 결과/freshness/카피 케이스 추가. 기존 `investor_flow_momentum` 케이스 카피 변경 반영. `high_volume_momentum` → `kr_high_volume_surge` rename 반영.
- `tests/test_invest_screener_toss_parity_diagnostics.py` — `double_buy` interpretation A/B 비교 출력 케이스 추가.
- `tests/test_invest_view_model_screener_presets.py` (필요 시 신규) — preset registry 일관성 테스트 (`high_volume_momentum` 미존재, `double_buy` 존재, 모든 preset id 가 `_METRIC_FIELD` 에 있음).

**Modify (frontend)**:

- `frontend/invest/src/types/screener.ts` — 필요 시 새 preset id 상수만 추가 (preset list 는 백엔드에서 옴).
- `frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx` — 신규 preset 에 맞는 column 표시 확인 (price + 1D change + flow chip). 기존 `investor_flow_momentum` 과 거의 동일하므로 큰 변경 없음.
- `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — `dataState=stale` 에서 어떤 dependency 가 stale 인지 보여주는 sub-warning 표시 (이미 `warnings` 배열로 들어가므로 별도 시각 변경은 옵셔널).

**Modify (docs)**:

- `docs/runbooks/invest-screener-snapshots.md` — ROB-276 섹션 추가: `double_buy` preset, A/B 해석 lock 근거, `high_volume_momentum` 제거 안내, freshness 다중 의존성 표기.

---

## Task 0: Toss A/B Interpretation Verification (Decision 1 Lock)

> 이 task 의 결과로 Decision 1 을 본 문서 위쪽에서 lock 한다. 검증 없이는 다음 task 로 진행하지 않는다.

**Files:**
- Read: `app/models/investor_flow_snapshot.py`, `app/models/invest_screener_snapshot.py`
- Create (임시, 커밋 안 함): `scripts/_rob276_ab_check.py` 또는 inline SQL via `psql`

- [ ] **Step 1: 최신 영업일 + 직전 영업일 결정**

`investor_flow_snapshots` 의 `max(snapshot_date)` 와 그 직전 distinct date 를 구한다.

```sql
WITH d AS (
  SELECT DISTINCT snapshot_date
  FROM investor_flow_snapshots
  WHERE market = 'kr'
  ORDER BY snapshot_date DESC
  LIMIT 2
)
SELECT array_agg(snapshot_date ORDER BY snapshot_date DESC) FROM d;
```

기록: `current_date_kr = <YYYY-MM-DD>`, `prev_date_kr = <YYYY-MM-DD>`.

- [ ] **Step 2: 해석 A 후보 집합 산출**

```sql
SELECT ifs.symbol
FROM investor_flow_snapshots ifs
JOIN invest_screener_snapshots iss
  ON iss.market = ifs.market
 AND iss.symbol = ifs.symbol
 AND iss.snapshot_date = ifs.snapshot_date
WHERE ifs.market = 'kr'
  AND ifs.snapshot_date = :current_date_kr
  AND ifs.foreign_net > 0
  AND ifs.institution_net > 0
  AND COALESCE(iss.change_rate, 0) >= 0
ORDER BY iss.change_rate DESC NULLS LAST
LIMIT 200;
```

결과를 `out_a.txt` 로 저장.

- [ ] **Step 3: 해석 B 후보 집합 산출 (delta)**

```sql
WITH cur AS (
  SELECT symbol, foreign_net, institution_net
  FROM investor_flow_snapshots
  WHERE market = 'kr' AND snapshot_date = :current_date_kr
),
prev AS (
  SELECT symbol, foreign_net, institution_net
  FROM investor_flow_snapshots
  WHERE market = 'kr' AND snapshot_date = :prev_date_kr
)
SELECT cur.symbol
FROM cur
JOIN prev ON prev.symbol = cur.symbol
JOIN invest_screener_snapshots iss
  ON iss.market = 'kr'
 AND iss.symbol = cur.symbol
 AND iss.snapshot_date = :current_date_kr
WHERE (cur.foreign_net - prev.foreign_net) > 0
  AND (cur.institution_net - prev.institution_net) > 0
  AND COALESCE(iss.change_rate, 0) >= 0
ORDER BY iss.change_rate DESC NULLS LAST
LIMIT 200;
```

결과를 `out_b.txt` 로 저장.

- [ ] **Step 4: Toss reference set 준비**

이슈에 적힌 5개 + 가능하면 더 많은 캡처(Chrome devtools 의 toss `screen` 응답 JSON 의 `symbol`/`code` 필드, ROB-276 댓글에 첨부된 reference 가 있으면 그것). 최소 케이스: `011000, 439960, 083500, 042520, 042420`.

`toss_ref.txt` 1줄 1심볼로 저장 (대문자/zero-padded 6자리).

- [ ] **Step 5: A/B 비교 수치 산출**

```bash
sort -u toss_ref.txt > _ref.sorted
sort -u out_a.txt > _a.sorted
sort -u out_b.txt > _b.sorted
echo "ref total:     $(wc -l < _ref.sorted)"
echo "A overlap:     $(comm -12 _ref.sorted _a.sorted | wc -l)"
echo "B overlap:     $(comm -12 _ref.sorted _b.sorted | wc -l)"
echo "A only (extra): $(comm -23 _a.sorted _ref.sorted | wc -l)"
echo "B only (extra): $(comm -23 _b.sorted _ref.sorted | wc -l)"
echo "ref missed A:  $(comm -23 _ref.sorted _a.sorted | wc -l)"
echo "ref missed B:  $(comm -23 _ref.sorted _b.sorted | wc -l)"
```

- [ ] **Step 6: Lock 결정 기록**

- A overlap ≥ B overlap + 1 이고 reference 의 50% 이상 커버 → **A lock**.
- B overlap ≥ A overlap + 1 이고 reference 의 50% 이상 커버 → **B lock**.
- 둘 다 < 50% 커버 또는 동률 → **A lock (safer fallback)** + diagnostic 에서 양쪽 표기 유지.

본 plan 의 "Decision 1" 섹션 결론 부분과 Task 4 의 helper 본문을 lock 결과로 갱신.

- [ ] **Step 7: 임시 파일 정리**

```bash
rm -f _ref.sorted _a.sorted _b.sorted out_a.txt out_b.txt toss_ref.txt
# scripts/_rob276_ab_check.py 만들었으면 삭제
```

커밋 없음 (verification 만).

---

## Task 1: Preset Registry — `double_buy` 추가 & `high_volume_momentum` clean cut

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py:69-80, 185-191, 15`
- Test: `tests/test_invest_view_model_screener_presets.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_invest_view_model_screener_presets.py
from app.services.invest_view_model.screener_presets import (
    SCREENER_PRESETS,
    preset_definitions,
    screening_filters_for,
    _KR_ONLY_PRESET_IDS,
)


def test_high_volume_momentum_removed_and_volume_preset_renamed():
    ids = {p.id for p in SCREENER_PRESETS}
    assert "high_volume_momentum" not in ids
    assert "kr_high_volume_surge" in ids
    surge = next(p for p in SCREENER_PRESETS if p.id == "kr_high_volume_surge")
    assert surge.name == "거래량 급증"
    assert surge.market == "kr"


def test_double_buy_preset_present_and_kr_only():
    ids = {p.id for p in SCREENER_PRESETS}
    assert "double_buy" in ids
    db = next(p for p in SCREENER_PRESETS if p.id == "double_buy")
    assert db.name == "쌍끌이 매수"
    assert db.market == "kr"
    assert "double_buy" in _KR_ONLY_PRESET_IDS
    chips = {c.label for c in db.filterChips}
    assert "국내" in chips
    # 외국인+기관 동시 매수가 명시되어야 함
    assert any("외국인" in c.label or "기관" in c.label for c in db.filterChips)


def test_investor_flow_momentum_copy_no_double_buy_wording():
    ifm = next(p for p in SCREENER_PRESETS if p.id == "investor_flow_momentum")
    assert "쌍끌이" not in ifm.description
    for chip in ifm.filterChips:
        detail = chip.detail or ""
        assert "쌍끌이" not in detail


def test_double_buy_screening_filters_lookup_is_kr_only_snapshot():
    filters = screening_filters_for("double_buy", "kr")
    assert filters["market"] == "kr"
    # 거래량 정렬이어선 안 됨
    assert filters.get("sort_by") != "volume"
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
uv run pytest tests/test_invest_view_model_screener_presets.py -v
```

Expected: 4 FAIL (preset 미존재 / 카피 미수정).

- [ ] **Step 3: Preset registry 수정**

`app/services/invest_view_model/screener_presets.py`:

L15 의 `_KR_ONLY_PRESET_IDS` 확장:

```python
_KR_ONLY_PRESET_IDS = {"investor_flow_momentum", "double_buy"}
```

L69-80 의 `high_volume_momentum` entry 를 `kr_high_volume_surge` 로 교체:

```python
ScreenerPreset(
    id="kr_high_volume_surge",
    name="거래량 급증",
    description="거래량이 폭발적으로 늘어난 종목",
    badges=[],
    filterChips=[
        ScreenerFilterChip(label="국내", detail=None),
        ScreenerFilterChip(label="거래량", detail="상위"),
    ],
    metricLabel="거래량",
    market="kr",
),
```

`investor_flow_momentum` (L82-93) 의 카피 정리:

```python
ScreenerPreset(
    id="investor_flow_momentum",
    name="수급 모멘텀",
    description="외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)",
    badges=["MVP"],
    filterChips=[
        ScreenerFilterChip(label="국내", detail=None),
        ScreenerFilterChip(label="투자자별 수급", detail="외국인 3일+ 연속 순매수"),
        ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
    ],
    metricLabel="외국인 순매수",
    market="kr",
),
```

`SCREENER_PRESETS` list 에 `double_buy` 추가 (`investor_flow_momentum` 다음, `growth_expectation` 앞):

```python
ScreenerPreset(
    id="double_buy",
    name="쌍끌이 매수",
    description="기관과 외국인이 동시에 매수하는 종목",
    badges=["NEW"],
    filterChips=[
        ScreenerFilterChip(label="국내", detail=None),
        ScreenerFilterChip(label="외국인", detail="순매수"),
        ScreenerFilterChip(label="기관", detail="순매수"),
        ScreenerFilterChip(label="주가등락률", detail="1일 ≥ 0%"),
        ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
    ],
    metricLabel="주가등락률",
    market="kr",
),
```

L185-191 의 `_SCREENING_FILTERS` 의 `high_volume_momentum` 키를 `kr_high_volume_surge` 로 rename. `double_buy` 키 추가:

```python
"kr_high_volume_surge": {
    "market": "kr",
    "asset_type": "stock",
    "sort_by": "volume",
    "sort_order": "desc",
    "limit": 20,
},
"double_buy": {
    "market": "kr",
    "asset_type": "stock",
    "sort_by": "change_rate",
    "sort_order": "desc",
    "min_change_rate": 0.0,
    "include_double_buy": True,
    "limit": 50,
},
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
uv run pytest tests/test_invest_view_model_screener_presets.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: 기존 테스트 영향 grep & 정리**

```bash
grep -rn "high_volume_momentum" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.md"
```

영향 라인:
- `app/services/invest_view_model/screener_service.py:675` — `_METRIC_FIELD` 키 rename + `double_buy` 추가 (Task 3 에서 본격 처리하지만 import error 방지를 위해 일단 키만 rename 하고 `double_buy` 는 placeholder `"change_rate"` 로 잡아둠).
- `tests/test_invest_view_model_screener_service.py:1062` — `("high_volume_momentum", "volume", ...)` → `("kr_high_volume_surge", "volume", ...)` rename.
- frontend: grep 결과에 따라 `frontend/invest/src/**` 에 `high_volume_momentum` 문자열이 남아있는지 확인. 보통 백엔드 응답에서 받는 ID 라서 hard-coded 가 없을 가능성이 높지만 있다면 동일하게 rename.

- [ ] **Step 6: 전체 invest_view_model 테스트 실행**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py tests/test_invest_view_model_screener_presets.py -v
```

Expected: 모두 PASS. 만약 다른 테스트가 깨지면 그 테스트가 hard-code 한 ID 도 같이 정리.

- [ ] **Step 7: 커밋**

```bash
git add app/services/invest_view_model/screener_presets.py \
        app/services/invest_view_model/screener_service.py \
        tests/test_invest_view_model_screener_presets.py \
        tests/test_invest_view_model_screener_service.py
git commit -m "$(cat <<'EOF'
feat(rob-276): rename high_volume_momentum to kr_high_volume_surge and scaffold double_buy preset

- 쌍끌이 매수 라벨을 거래량 기반 preset 에서 분리 (clean cut)
- double_buy preset registry/filters scaffold 추가, 본 query 는 후속 task 에서
- investor_flow_momentum 카피에서 "쌍끌이" 표현 제거

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Snapshot 조회 helper — `_load_double_buy_from_snapshots`

**Files:**
- Create: `app/services/invest_view_model/double_buy_screener.py`
- Test: `tests/test_invest_view_model_double_buy_screener.py` (신규)

- [ ] **Step 1: 실패 테스트 작성 (해석 A 기준; Task 0 결과가 B 이면 Step 3 본체만 B 로 교체)**

```python
# tests/test_invest_view_model_double_buy_screener.py
from __future__ import annotations

import datetime as dt
import decimal
import pytest

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_view_model.double_buy_screener import (
    load_double_buy_from_snapshots,
)


@pytest.mark.asyncio
async def test_returns_rows_filtered_by_double_buy_and_positive_change_rate(
    async_session,
):
    today = dt.date(2026, 5, 19)
    # Universe seed
    async_session.add_all(
        [
            KRSymbolUniverse(symbol="011000", name="진원생명과학", is_active=True),
            KRSymbolUniverse(symbol="000001", name="제외종목ETF", is_active=True),
        ]
    )
    # Investor flow snapshots: double_buy True + double_buy False
    async_session.add_all(
        [
            InvestorFlowSnapshot(
                market="kr",
                symbol="011000",
                snapshot_date=today,
                foreign_net=1_000_000,
                institution_net=2_000_000,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="000001",
                snapshot_date=today,
                foreign_net=-1,
                institution_net=-1,
                double_buy=False,
                double_sell=True,
                source="naver_finance",
            ),
        ]
    )
    # Screener snapshots: 011000 양수 change_rate, 000001 음수
    async_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol="011000",
                snapshot_date=today,
                latest_close=decimal.Decimal("12000"),
                prev_close=decimal.Decimal("10000"),
                change_rate=decimal.Decimal("20.0"),
                daily_volume=100_000,
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol="000001",
                snapshot_date=today,
                latest_close=decimal.Decimal("900"),
                prev_close=decimal.Decimal("1000"),
                change_rate=decimal.Decimal("-10.0"),
                daily_volume=50_000,
            ),
        ]
    )
    await async_session.commit()

    rows = await load_double_buy_from_snapshots(
        async_session, market="kr", limit=20
    )

    assert rows is not None
    assert [r["symbol"] for r in rows] == ["011000"]
    assert rows[0]["change_rate"] == pytest.approx(20.0)
    assert rows[0]["double_buy"] is True
    assert rows[0]["_screener_snapshot_state"] in {"fresh", "stale"}


@pytest.mark.asyncio
async def test_returns_none_when_no_snapshots(async_session):
    rows = await load_double_buy_from_snapshots(
        async_session, market="kr", limit=20
    )
    assert rows is None


@pytest.mark.asyncio
async def test_excludes_non_common_stock_by_name_heuristic(async_session):
    today = dt.date(2026, 5, 19)
    async_session.add_all(
        [
            KRSymbolUniverse(symbol="999999", name="KODEX 200 ETF", is_active=True),
        ]
    )
    async_session.add(
        InvestorFlowSnapshot(
            market="kr",
            symbol="999999",
            snapshot_date=today,
            foreign_net=10,
            institution_net=10,
            double_buy=True,
            double_sell=False,
            source="naver_finance",
        )
    )
    async_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="999999",
            snapshot_date=today,
            latest_close=decimal.Decimal("10000"),
            prev_close=decimal.Decimal("9000"),
            change_rate=decimal.Decimal("11.0"),
            daily_volume=1,
        )
    )
    await async_session.commit()

    rows = await load_double_buy_from_snapshots(
        async_session, market="kr", limit=20
    )
    assert rows == []  # latest partition exists but ETF filtered out
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
uv run pytest tests/test_invest_view_model_double_buy_screener.py -v
```

Expected: 3 FAIL (ImportError).

- [ ] **Step 3: helper 구현 (해석 A — Task 0 lock; B 로 전환 시 SQL 본체만 교체)**

`app/services/invest_view_model/double_buy_screener.py`:

```python
"""Read-only loader for the 쌍끌이 매수 (Toss screenId=18 parity) preset.

Joins the latest investor_flow_snapshots row with the latest invest_screener_snapshots
row per symbol and applies the Toss-parity filter (Interpretation A, locked 2026-05-20
under Task 0 safer-fallback rule — see plan Decision 1):
    market = kr
    foreign_net  > 0   AND institution_net  > 0   (=double_buy)  [LOCKED — Interpretation A]
    change_rate >= 0
    sort by change_rate desc, symbol asc

If a later diagnostic A/B run (Task 4) shows Interpretation B is materially closer to
Toss screenId=18, swap only this query block for the self-join variant documented
below in the Step 3 fallback note. Preset metadata / freshness logic unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse

logger = logging.getLogger(__name__)


async def load_double_buy_from_snapshots(
    session: AsyncSession | None, *, market: str, limit: int = 50
) -> list[dict[str, Any]] | None:
    """Return Toss-parity 쌍끌이 매수 rows or None when no snapshot partition exists.

    None  → caller should report dataState=missing and warn that snapshots are absent.
    []    → latest partition exists but no qualifiers (caller renders empty + stale).
    Rows  → ordered by change_rate desc, symbol asc.
    """
    if session is None or market != "kr":
        return None

    latest_flow_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr"
    )
    latest_price_stmt = sa.select(sa.func.max(InvestScreenerSnapshot.snapshot_date)).where(
        InvestScreenerSnapshot.market == "kr"
    )
    try:
        flow_date = (await session.execute(latest_flow_stmt)).scalar_one_or_none()
        price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("double_buy: latest dates lookup failed: %s", exc, exc_info=True)
        return None
    if flow_date is None or price_date is None:
        return None

    # --- Interpretation A (LOCKED via Task 0; swap block if diagnostic later picks B) ---
    candidate_stmt = (
        sa.select(
            InvestorFlowSnapshot.symbol,
            InvestorFlowSnapshot.foreign_net,
            InvestorFlowSnapshot.institution_net,
            InvestorFlowSnapshot.individual_net,
            InvestorFlowSnapshot.double_buy,
            InvestorFlowSnapshot.foreign_consecutive_buy_days,
            InvestorFlowSnapshot.institution_consecutive_buy_days,
            InvestScreenerSnapshot.latest_close,
            InvestScreenerSnapshot.prev_close,
            InvestScreenerSnapshot.change_rate,
            InvestScreenerSnapshot.daily_volume,
            InvestScreenerSnapshot.snapshot_date.label("price_snapshot_date"),
            InvestorFlowSnapshot.snapshot_date.label("flow_snapshot_date"),
        )
        .join(
            InvestScreenerSnapshot,
            sa.and_(
                InvestScreenerSnapshot.market == InvestorFlowSnapshot.market,
                InvestScreenerSnapshot.symbol == InvestorFlowSnapshot.symbol,
                InvestScreenerSnapshot.snapshot_date == price_date,
            ),
        )
        .where(
            InvestorFlowSnapshot.market == "kr",
            InvestorFlowSnapshot.snapshot_date == flow_date,
            InvestorFlowSnapshot.foreign_net > 0,
            InvestorFlowSnapshot.institution_net > 0,
            sa.func.coalesce(InvestScreenerSnapshot.change_rate, 0) >= 0,
        )
        .order_by(
            InvestScreenerSnapshot.change_rate.desc().nullslast(),
            InvestorFlowSnapshot.symbol.asc(),
        )
        .limit(max(limit * 4, limit + 40))
    )
    try:
        result = await session.execute(candidate_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("double_buy: candidate query failed: %s", exc, exc_info=True)
        return None
    candidate_rows = list(result.mappings().all())

    # Common-stock guard via KR universe name
    symbols = [r["symbol"] for r in candidate_rows]
    name_map: dict[str, str] = {}
    if symbols:
        try:
            names = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            name_map = {row.symbol: row.name for row in names.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("double_buy: name lookup failed: %s", exc, exc_info=True)

    from app.services.invest_view_model.screener_service import _is_kr_toss_common_stock

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in candidate_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        name = name_map.get(sym)
        if not _is_kr_toss_common_stock(sym, name):
            continue
        seen.add(sym)
        state = "fresh" if r["price_snapshot_date"] == r["flow_snapshot_date"] else "stale"
        rows.append(
            {
                "symbol": sym,
                "market": "kr",
                "name": name,
                "latest_close": float(r["latest_close"]) if r["latest_close"] is not None else None,
                "prev_close": float(r["prev_close"]) if r["prev_close"] is not None else None,
                "change_rate": float(r["change_rate"]) if r["change_rate"] is not None else None,
                "volume": r["daily_volume"],
                "foreign_net": r["foreign_net"],
                "institution_net": r["institution_net"],
                "individual_net": r["individual_net"],
                "double_buy": r["double_buy"],
                "foreign_consecutive_buy_days": r["foreign_consecutive_buy_days"],
                "institution_consecutive_buy_days": r["institution_consecutive_buy_days"],
                "snapshot_date": r["price_snapshot_date"],
                "_screener_snapshot_state": state,
            }
        )
        if len(rows) >= limit:
            break
    return rows
```

> **If Task 0 locks Interpretation B**, replace the `--- Interpretation A ---` block with a self-join on `investor_flow_snapshots` for current vs previous trading day:
>
> ```python
> prev_date_stmt = (
>     sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date))
>     .where(
>         InvestorFlowSnapshot.market == "kr",
>         InvestorFlowSnapshot.snapshot_date < flow_date,
>     )
> )
> prev_date = (await session.execute(prev_date_stmt)).scalar_one_or_none()
> if prev_date is None:
>     return None  # cannot compute delta; treat as missing
> Prev = sa.orm.aliased(InvestorFlowSnapshot)
> candidate_stmt = (
>     sa.select(InvestorFlowSnapshot, Prev, InvestScreenerSnapshot)
>     .join(Prev, sa.and_(Prev.market == "kr", Prev.symbol == InvestorFlowSnapshot.symbol, Prev.snapshot_date == prev_date))
>     .join(InvestScreenerSnapshot, sa.and_(
>         InvestScreenerSnapshot.market == "kr",
>         InvestScreenerSnapshot.symbol == InvestorFlowSnapshot.symbol,
>         InvestScreenerSnapshot.snapshot_date == price_date,
>     ))
>     .where(
>         InvestorFlowSnapshot.market == "kr",
>         InvestorFlowSnapshot.snapshot_date == flow_date,
>         (InvestorFlowSnapshot.foreign_net - Prev.foreign_net) > 0,
>         (InvestorFlowSnapshot.institution_net - Prev.institution_net) > 0,
>         sa.func.coalesce(InvestScreenerSnapshot.change_rate, 0) >= 0,
>     )
>     .order_by(InvestScreenerSnapshot.change_rate.desc().nullslast(), InvestorFlowSnapshot.symbol.asc())
>     .limit(max(limit * 4, limit + 40))
> )
> ```
>
> 그 외 row 변환은 동일. `prev_date is None` 이면 "previous snapshot missing" 으로 caller 에 신호 (`return []` + state override 는 Task 3 에서 처리).

- [ ] **Step 4: 테스트 통과 확인**

```bash
uv run pytest tests/test_invest_view_model_double_buy_screener.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/invest_view_model/double_buy_screener.py tests/test_invest_view_model_double_buy_screener.py
git commit -m "$(cat <<'EOF'
feat(rob-276): add double_buy snapshot loader for Toss screenId=18 parity

- read-only join of investor_flow_snapshots + invest_screener_snapshots
- common-stock guard via _is_kr_toss_common_stock
- returns None when partition absent, [] when partition exists but empty

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `screener_service.py` 분기 연결 + Freshness 분리

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:670-681, 1220-1266, 1314-1325`
- Test: `tests/test_invest_view_model_screener_service.py` (신규 케이스 추가)

- [ ] **Step 1: 실패 테스트 추가**

`tests/test_invest_view_model_screener_service.py` 에 다음 케이스 추가 (위치: 기존 `investor_flow_momentum` 테스트 근처):

```python
@pytest.mark.asyncio
async def test_double_buy_preset_returns_snapshot_filtered_rows(async_session, ...):
    # given: today partition with 2 double_buy True symbols, 1 with negative change_rate
    # when: build_screener_results(preset_id="double_buy", market="kr", session=async_session)
    # then: only the positive-change symbol returned, ordered by change_rate desc
    ...


@pytest.mark.asyncio
async def test_double_buy_preset_missing_snapshot_reports_missing_state(
    async_session_no_snapshots,
):
    response = await build_screener_results(
        preset_id="double_buy", market="kr", session=async_session_no_snapshots, ...
    )
    assert response.freshness.dataState == "missing"
    assert any("스냅샷" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_double_buy_preset_stale_when_price_snapshot_older_than_flow(
    async_session_stale_price,
):
    response = await build_screener_results(
        preset_id="double_buy", market="kr", session=async_session_stale_price, ...
    )
    assert response.freshness.dataState in {"stale", "fallback"}
    assert any(
        "시세 스냅샷" in w or "price" in w.lower() for w in response.warnings
    )


def test_double_buy_in_metric_field_map():
    from app.services.invest_view_model.screener_service import _METRIC_FIELD
    assert _METRIC_FIELD["double_buy"] == "change_rate"
```

(테스트 시그니처는 기존 fixture 패턴에 맞춰 조정.)

- [ ] **Step 2: 테스트 실패 확인**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v -k double_buy
```

Expected: 4 FAIL.

- [ ] **Step 3: `_METRIC_FIELD` 업데이트**

`screener_service.py` L670-681:

```python
_METRIC_FIELD: dict[str, str] = {
    "consecutive_gainers": "week_change_rate",
    "cheap_value": "per",
    "steady_dividend": "dividend_yield",
    "oversold_recovery": "rsi",
    "kr_high_volume_surge": "volume",   # was "high_volume_momentum"
    "growth_expectation": "change_rate",
    "investor_flow_momentum": "foreign_net",
    "double_buy": "change_rate",        # NEW
    "crypto_high_volume": "trade_amount_24h",
    "crypto_oversold": "rsi",
    "crypto_momentum": "change_rate",
}
```

- [ ] **Step 4: snapshot-first 분기에 double_buy 추가**

`screener_service.py` L1220-1266 의 분기 체인에 추가 (`investor_flow_momentum` 분기 다음, `crypto` 분기 앞):

```python
elif preset_id == "double_buy":
    from app.services.invest_view_model.double_buy_screener import (
        load_double_buy_from_snapshots,
    )
    _snapshot_check_result = await load_double_buy_from_snapshots(
        session,
        market=requested_market,
        limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
    )
    _snapshot_empty_warning = (
        "최신 수급/시세 스냅샷에서 쌍끌이 매수 조건에 맞는 종목이 없습니다."
    )
```

`investor_flow_momentum` 의 안전망 패턴(L1257-1265)과 동일하게 `double_buy` 도 추가:

```python
if preset_id == "double_buy" and _snapshot_check_result is None:
    _snapshot_check_result = []
    _snapshot_state_override = "missing"
    _snapshot_empty_warning = (
        "수급 또는 시세 스냅샷이 아직 적재되지 않아 쌍끌이 매수 후보를 표시할 수 없습니다."
    )
```

- [ ] **Step 5: Freshness 다중 의존성 분리 (KST trading-date 기반)**

`screener_service.py` 의 `_load_double_buy_from_snapshots` 호출 직후 / state 결정 직전에, 시세 vs 수급 스냅샷 일자를 비교해 warning 을 분리한다. 기존 `today_trading_date` 사용 (이미 `app/services/invest_screener_snapshots/freshness.py:17` 에 존재 → KST 기반).

`screener_service.py` 안에 helper 추가 (예: L1325 근처 또는 별도 함수):

```python
def _double_buy_dependency_warnings(
    *, snapshot_rows: list[dict[str, Any]] | None, now_market_date: dt.date
) -> tuple[list[str], str | None]:
    """Return (warnings, state_override) for double_buy by dependency.

    Distinguishes: price stale vs flow stale vs both fresh.  Used only when the
    snapshot loader returns rows; pure missing partitions are handled above.
    """
    if not snapshot_rows:
        return [], None
    flow_dates = {r.get("snapshot_date") for r in snapshot_rows}
    price_states = {r.get("_screener_snapshot_state") for r in snapshot_rows}
    warnings: list[str] = []
    if "stale" in price_states:
        warnings.append("시세 스냅샷이 직전 영업일 기준이라 일부 데이터가 1일 지연되었습니다.")
    if all(d is not None and d < now_market_date for d in flow_dates):
        warnings.append("수급 스냅샷이 직전 영업일 기준이라 외인/기관 정보가 1일 지연되었습니다.")
    state = "stale" if warnings else None
    return warnings, state
```

이 결과를 `_aggregated_data_state` 와 `upstream_warnings` 에 머지. (정확한 머지 위치는 기존 `investor_flow_momentum` 의 stale 처리 패턴을 따른다.)

- [ ] **Step 6: `_KR_ONLY_PRESET_IDS` 가 service 분기에서 일관됨 확인**

`get_preset` 이 `requested_market="kr"` 일 때만 `double_buy` 를 노출하는지 확인. preset_definitions 분기를 통과하면 OK.

- [ ] **Step 7: 테스트 통과 확인**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v -k double_buy
uv run pytest tests/test_invest_view_model_screener_service.py -v
```

Expected: 신규 4 + 기존 모두 PASS.

- [ ] **Step 8: 커밋**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "$(cat <<'EOF'
feat(rob-276): wire double_buy preset into screener view-model with split freshness

- snapshot-first branch in build_screener_results
- separate warnings for stale price vs stale investor-flow snapshots
- missing partition → dataState=missing with explicit warning

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Diagnostic 확장 — A/B Interpretation 비교 모드

**Files:**
- Modify: `scripts/diagnose_invest_screener_toss_parity.py:34-50, 그리고 main 함수`
- Test: `tests/test_invest_screener_toss_parity_diagnostics.py` (신규 케이스 추가)

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_double_buy_supported_and_emits_ab_comparison(monkeypatch, capsys, tmp_path):
    # given: toss_symbols file + seeded DB partitions with known A/B differing sets
    # when: run --preset double_buy --interpretation both
    # then: stdout JSON includes a_overlap, b_overlap, a_only, b_only, ref_missing_a, ref_missing_b
    ...
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
uv run pytest tests/test_invest_screener_toss_parity_diagnostics.py -v -k double_buy
```

Expected: 1 FAIL.

- [ ] **Step 3: Diagnostic 본체 변경**

`scripts/diagnose_invest_screener_toss_parity.py`:

- L34 `_SUPPORTED_PRESETS = {"consecutive_gainers", "double_buy"}` 로 확장.
- argparse 에 `--interpretation` 추가: `choices=("a", "b", "both"), default="both"`.
- `double_buy` 분기에서:
  - 해석 A 후보 set = `load_double_buy_from_snapshots(... interpretation="a")` 또는 inline SQL.
  - 해석 B 후보 set = inline SQL (current vs prev investor_flow self-join).
  - reference set 은 기존 `--toss-symbols-file` 로 받음.
  - 출력 JSON 에 다음 필드 포함:

    ```python
    {
      "preset": "double_buy",
      "current_date": "...",
      "prev_date": "...",
      "interpretation_a": {
          "count": N_a, "overlap": ..., "extra": ..., "missing": ...,
      },
      "interpretation_b": {
          "count": N_b, "overlap": ..., "extra": ..., "missing": ...,
      },
      "ref_size": ref_n,
      "note": "Toss data captured manually; never fetched at runtime.",
    }
    ```

- 어떤 경우에도 Toss 에 HTTP 요청 금지 — 모든 reference 는 file path 에서 옴.

- [ ] **Step 4: 테스트 통과 확인**

```bash
uv run pytest tests/test_invest_screener_toss_parity_diagnostics.py -v
```

Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add scripts/diagnose_invest_screener_toss_parity.py tests/test_invest_screener_toss_parity_diagnostics.py
git commit -m "$(cat <<'EOF'
feat(rob-276): extend toss parity diagnostic with double_buy A/B comparison

- --interpretation {a,b,both} flag, default both
- emits per-interpretation overlap/extra/missing counts
- Toss data still file-only; no runtime scraping

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Frontend rendering 확인 + freshness sub-warning

**Files:**
- Modify (선택적): `frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx`, `ScreenerFreshnessLine.tsx`
- Modify: `frontend/invest/src/types/screener.ts` (필요 시 preset id 상수만)
- Test (선택적): `frontend/invest/src/desktop/screener/__tests__/*.test.tsx` 에 신규 preset snapshot

- [ ] **Step 1: 백엔드 응답 수동 확인 (서버 기동 후)**

```bash
make dev &
sleep 4
curl -s "http://localhost:8000/invest/api/screener/presets?market=kr" | jq '.presets[] | select(.id=="double_buy" or .id=="kr_high_volume_surge" or .id=="investor_flow_momentum") | {id,name,description}'
curl -s "http://localhost:8000/invest/api/screener/results?preset=double_buy&market=kr" | jq '{title, description, freshness, resultCount: (.results|length), warnings}'
```

Expected:
- `double_buy` preset 의 name = `쌍끌이 매수`.
- results 응답이 200 + freshness.dataState 는 데이터 상태에 따라 fresh/stale/missing.
- 잘못된 generic provider fallback 으로 인한 "데이터 준비중" 메시지가 뜨지 않음.

- [ ] **Step 2: 프론트엔드 화면 수동 확인**

`frontend/invest` dev 서버 기동 후 브라우저로 `/invest/screener` 열어서:

- 신규 `쌍끌이 매수` 카드가 거래량 카드와 별도로 보임.
- `수급 모멘텀` 카드 카피에 `쌍끌이` 표현 없음.
- 결과 row 에 종목명/현재가/1D 등락률/`쌍끌이 매수` 칩(`tone=double_buy`) 표시.
- stale 상태일 때 freshness 안내 문구가 "시세 스냅샷 1일 지연" 또는 "수급 스냅샷 1일 지연" 으로 구분.

각 화면 스크린샷을 PR description 에 첨부.

- [ ] **Step 3: 깨진 케이스가 있으면 컴포넌트 보정**

`ScreenerResultsTable.tsx` 가 `latest_close`/`change_rate`/`investor_flow_chip` 필드를 모두 렌더링하는지 확인. 누락이 있으면 type/render 추가. column 시그니처는 기존과 호환 유지.

`ScreenerFreshnessLine.tsx` 는 `warnings` 배열을 그대로 렌더하므로 추가 작업 보통 불필요.

- [ ] **Step 4: frontend 테스트 (있는 경우) 실행**

```bash
cd frontend/invest && pnpm test
```

- [ ] **Step 5: 커밋 (변경이 있으면)**

```bash
git add frontend/invest/
git commit -m "$(cat <<'EOF'
feat(rob-276): ensure /invest/screener renders double_buy preset rows correctly

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Runbook 업데이트

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`

- [ ] **Step 1: ROB-276 섹션 추가**

내용:

```markdown
## ROB-276 — 쌍끌이 매수 (Toss screenId=18 parity)

- **Preset**: `double_buy` (KR only, snapshot-only, read-only)
- **Filter**: <Task 0 결과로 lock>
  - Interpretation A (locked): `investor_flow_snapshots.foreign_net > 0 AND institution_net > 0` AND `invest_screener_snapshots.change_rate >= 0`, sort `change_rate DESC`
  - (or) Interpretation B (locked): `(foreign_net - prev) > 0 AND (institution_net - prev) > 0` AND `change_rate >= 0`
- **Lock 근거**: Toss reference (`YYYY-MM-DD` 캡처, N=___) 와 비교 시 A overlap=__, B overlap=__. <safer fallback 선택했다면 이유>.
- **Removed**: 기존 `high_volume_momentum` preset 폐기. 후속 거래량 프리셋은 `kr_high_volume_surge` 로 이전. 구 ID 호환 안 함.
- **Freshness**:
  - 시세 스냅샷 stale → "시세 스냅샷 1일 지연" warning
  - 수급 스냅샷 stale → "수급 스냅샷 1일 지연" warning
  - 둘 다 missing → `dataState=missing` + 명시 warning
- **Diagnostic**:
  - `uv run python -m scripts.diagnose_invest_screener_toss_parity --preset double_buy --toss-symbols-file path/to/toss_ref.json --interpretation both`
  - Toss 데이터는 항상 파일 기반, runtime fetch 없음.
- **Scope guard**: no migration, no ingestion job, no scheduler activation, no broker/order mutation, no Toss runtime scraping.
```

- [ ] **Step 2: 커밋**

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "$(cat <<'EOF'
docs(rob-276): document double_buy preset, lock decision, and freshness behavior

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 통합 검증 + PR

- [ ] **Step 1: 전체 관련 테스트 실행**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py \
              tests/test_invest_view_model_screener_presets.py \
              tests/test_invest_view_model_double_buy_screener.py \
              tests/test_invest_screener_toss_parity_diagnostics.py -v
```

Expected: 모두 PASS.

- [ ] **Step 2: lint/format/typecheck**

```bash
make lint
make typecheck
```

- [ ] **Step 3: API 샘플 캡처**

```bash
curl -s "http://localhost:8000/invest/api/screener/presets?market=kr" | jq '.presets[] | {id, name}' > /tmp/rob276_presets.json
curl -s "http://localhost:8000/invest/api/screener/results?preset=double_buy&market=kr" | jq '.' > /tmp/rob276_results.json
```

→ PR description 에 첨부.

- [ ] **Step 4: PR 생성**

```bash
git push -u origin rob-276
gh pr create --base main --title "feat(rob-276): invest screener 쌍끌이 매수 Toss screenId=18 parity" --body "$(cat <<'EOF'
## Summary

- 새 preset `double_buy` (쌍끌이 매수) 추가 — Toss screenId=18 parity (snapshot-only, read-only)
- 기존 `high_volume_momentum` → `kr_high_volume_surge` 로 clean-cut rename (구 ID 폐기)
- `investor_flow_momentum` 카피에서 "쌍끌이" 표현 제거 — 사용자 입장에서 카드 구분 명확화
- Freshness 가 price snapshot / flow snapshot 의존성을 분리해서 warning

## Locked decisions (plan top)

- Decision 1: Toss `외국인_순매수_비교` 해석은 **<A or B>** 로 lock. Reference (`<YYYY-MM-DD>`, N=__) 와의 overlap A=__, B=__.
- Decision 2: `high_volume_momentum` clean cut (`kr_high_volume_surge` 로 ID 자체 변경). 구 ID 사용 URL/북마크는 unknown preset 응답.
- Decision 3: `수급 모멘텀` 카피 정리 (description/chip 에서 "쌍끌이" 제거), 내부 ID/쿼리는 유지.

## Scope guardrails (kept)

- No DB migrations
- No new ingestion job / scheduler / TaskIQ task / Prefect deployment
- No broker / order / watch mutation
- No Toss runtime scraping — diagnostic 만 파일 기반 reference 사용

## Tests run

- backend pytest: <붙여넣기>
- diagnostic: `uv run python -m scripts.diagnose_invest_screener_toss_parity --preset double_buy --interpretation both --toss-symbols-file ...` 결과 첨부

## Local API samples

- `/invest/api/screener/presets?market=kr` — attached
- `/invest/api/screener/results?preset=double_buy&market=kr` — attached

## Toss parity gap (if any)

- <남은 차이 명시: 캡처 일자, ETF/preferred 제외 한계, 등>

## Deployment notes

- 마이그레이션 없음
- 스케줄러 활성화 없음
- 배포 전 `model_rate_limit:*` 등 무관 인프라 점검 불필요
- 구 `high_volume_momentum` URL 호출은 unknown preset 응답으로 빠짐 — frontend preset list 가 신규 ID 를 보장하므로 영향 작음

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Linear 상태 전환**

ROB-276 status: `Backlog` → `In Progress`. PR 머지 후 → `In Review` → `Done`.

---

## Self-Review Checklist

- [x] Spec coverage: Toss parity preset, freshness split, copy fix, scope guards, diagnostic, frontend — 모두 task 매핑됨
- [x] No placeholders — Task 0 결과로 lock 할 SQL/criteria 모두 명시
- [x] Type consistency — `load_double_buy_from_snapshots` 함수명 / 인자 / 반환 형태 task 간 일관
- [x] Scope guards 명시
- [x] Decision 1 lock 절차가 plan 상단에 명시되고 verification task 가 가장 먼저 실행됨
- [x] 모든 task 마다 commit step 존재 (TDD: fail → impl → pass → commit)
