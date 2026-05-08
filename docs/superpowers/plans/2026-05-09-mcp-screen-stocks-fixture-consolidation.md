# MCP screen_stocks 테스트 fixture 통합 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SonarCloud에서 26%의 중복을 차지하는 MCP `screen_stocks` 테스트 영역에서 (1) 헬퍼 클래스의 인라인 재정의, (2) `TestScreenStocksTvScreenerContract` 클래스의 양쪽 존재 — 두 가지 구조 결함을 해소한다.

**Architecture:** `tests/_mcp_tooling_support.py` 가 모든 MCP 테스트 헬퍼의 단일 소스가 되도록 재정렬한다. `TestScreenStocksTvScreenerContract` 는 `tests/test_mcp_screen_stocks_tvscreener_contract.py` 단일 파일로 통합한다. 작업은 항상 *기존 테스트가 통과* 하는 상태를 보존하는 단위로 쪼개고, 각 단계 직후 영향받는 테스트 모듈을 실행한다.

**Tech Stack:** pytest, pytest-asyncio, ruff, ty, uv. 변경은 `tests/` 영역에 한정된다 — `app/` 의 프로덕션 코드는 변경하지 않는다.

**Reference Spec:** `docs/superpowers/specs/2026-05-09-mcp-screen-stocks-fixture-consolidation-design.md`

**Worktree & Branch:** 작업은 `chore/mcp-screen-stocks-fixture-consolidation` 브랜치(`origin/main` 기준)와 `~/.superset/worktrees/auto_trader/mcp-fixture-consolidation` worktree에서 진행한다 (이미 생성됨).

---

## File Inventory

| 파일 | 역할 | 작업 |
|---|---|---|
| `tests/_mcp_tooling_support.py` | MCP 테스트 헬퍼 단일 소스 | (필요 시) `fake_crypto_tvscreener_module` fixture 추가 |
| `tests/_mcp_screen_stocks_support.py` | screen_stocks 도메인 fixture + 잡다한 테스트 클래스 5+ | 인라인 헬퍼 제거 + `TestScreenStocksTvScreenerContract` 클래스 제거 |
| `tests/test_mcp_screen_stocks_tvscreener_contract.py` | tvscreener contract 테스트 (정본) | support에서 옮겨오는 메서드 통합 |
| `tests/test_crypto_composite_score.py` | crypto composite score 테스트 | 인라인 헬퍼 제거 |

**비대상 파일:**  `tests/test_mcp_screen_stocks_filters_and_rsi.py`, `tests/test_mcp_screen_stocks_crypto.py` — 이들 파일에는 인라인 헬퍼 없음. 본 PR 범위에서 직접 변경하지 않음 (검증 단계에서 실행만).

---

## Task 1: 기준선 캡처 (baseline)

**Files:**
- Read: `tests/_mcp_tooling_support.py`, `tests/_mcp_screen_stocks_support.py`, `tests/test_mcp_screen_stocks_tvscreener_contract.py`, `tests/test_crypto_composite_score.py`

- [ ] **Step 1: pytest 노드 ID 카운트 캡처 (실행 전)**

```bash
cd /Users/robin/.superset/worktrees/auto_trader/mcp-fixture-consolidation
uv run pytest --collect-only -q \
  tests/_mcp_screen_stocks_support.py \
  tests/test_mcp_screen_stocks_tvscreener_contract.py \
  tests/test_mcp_screen_stocks_filters_and_rsi.py \
  tests/test_mcp_screen_stocks_crypto.py \
  tests/test_crypto_composite_score.py 2>&1 | tee /tmp/baseline-collect.txt | tail -5
```

기대: 마지막 줄에 `N tests collected` 형태의 숫자. 그 N을 기록한다.

- [ ] **Step 2: 기준 테스트 실행 (현 상태가 그린인지 확인)**

```bash
uv run pytest \
  tests/_mcp_screen_stocks_support.py \
  tests/test_mcp_screen_stocks_tvscreener_contract.py \
  tests/test_mcp_screen_stocks_filters_and_rsi.py \
  tests/test_mcp_screen_stocks_crypto.py \
  tests/test_crypto_composite_score.py 2>&1 | tail -10
```

기대: `passed` 만 있고 `failed` / `error` 0건. failed가 있으면 본 PR 시작 전에 사용자에게 보고하고 멈춘다 (프리-existing 결함을 본 PR이 수정하지 않음).

- [ ] **Step 3: `_mcp_tooling_support.py` 의 export 확인**

```bash
grep -nE '^(class _TvCondition|class _TvField|class DummyMCP|def build_tools|def fake_crypto_tvscreener_module|@pytest.fixture)' tests/_mcp_tooling_support.py
```

기대: `_TvCondition`, `_TvField`, `DummyMCP`, `build_tools` 모두 정의되어 있음. `fake_crypto_tvscreener_module` 는 없을 가능성이 높다 (Task 2에서 추가).

---

## Task 2: `_mcp_tooling_support.py` 에 `fake_crypto_tvscreener_module` 추가

**Files:**
- Modify: `tests/_mcp_tooling_support.py`

- [ ] **Step 1: 기존 fixture 위치 파악**

```bash
grep -n '@pytest.fixture' tests/_mcp_tooling_support.py
```

마지막 fixture 정의 끝 위치를 메모한다 (그 아래에 새 fixture를 붙인다).

- [ ] **Step 2: `_mcp_screen_stocks_support.py` 의 정본 fixture 본문 추출**

```bash
sed -n '74,90p' tests/_mcp_screen_stocks_support.py
```

기대 출력 (본문은 `_TvField` 의 인스턴스를 SimpleNamespace 로 묶음):

```python
@pytest.fixture
def fake_crypto_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        CryptoField=SimpleNamespace(
            NAME=_TvField("name"),
            DESCRIPTION=_TvField("description"),
            PRICE=_TvField("price"),
            CHANGE_PERCENT=_TvField("change_percent"),
            RELATIVE_STRENGTH_INDEX_14=_TvField("rsi14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_TvField("adx14"),
            VOLUME_24H_IN_USD=_TvField("volume24h"),
            VALUE_TRADED=_TvField("value_traded"),
            MARKET_CAP=_TvField("market_cap"),
            EXCHANGE=_TvField("exchange"),
        )
    )
```

`test_crypto_composite_score.py:75–88` 의 동일 fixture 와 본문을 비교(diff). 동일하면 위 본문 그대로 사용. 다르다면 더 풍부한 쪽(필드 더 많은 쪽) 채택.

- [ ] **Step 3: `_mcp_tooling_support.py` 의 마지막 fixture 아래에 추가**

`SimpleNamespace` 가 이미 `from types import SimpleNamespace` 로 import 되어있는지 확인. 안 되어 있으면 import 블록에 추가. `_TvField` 는 같은 모듈에 정의돼있으므로 추가 import 불필요.

추가할 코드 (모듈 끝):

```python
@pytest.fixture
def fake_crypto_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        CryptoField=SimpleNamespace(
            NAME=_TvField("name"),
            DESCRIPTION=_TvField("description"),
            PRICE=_TvField("price"),
            CHANGE_PERCENT=_TvField("change_percent"),
            RELATIVE_STRENGTH_INDEX_14=_TvField("rsi14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_TvField("adx14"),
            VOLUME_24H_IN_USD=_TvField("volume24h"),
            VALUE_TRADED=_TvField("value_traded"),
            MARKET_CAP=_TvField("market_cap"),
            EXCHANGE=_TvField("exchange"),
        )
    )
```

- [ ] **Step 4: 기존 헬퍼 모듈만 단독 import 가능한지 확인**

```bash
uv run python -c "from tests._mcp_tooling_support import _TvField, _TvCondition, DummyMCP, build_tools, fake_crypto_tvscreener_module; print('ok')"
```

기대 출력: `ok`. import 에러 발생 시 누락된 import (`SimpleNamespace`, `pytest`) 보강.

- [ ] **Step 5: 커밋**

```bash
git add tests/_mcp_tooling_support.py
git commit -m "$(cat <<'EOF'
test(mcp): add fake_crypto_tvscreener_module fixture to shared support

Phase 1/4 of MCP screen_stocks fixture consolidation: prepare the single
source of truth before removing inline duplicates from per-domain test
files.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `_mcp_screen_stocks_support.py` 인라인 헬퍼 제거

**Files:**
- Modify: `tests/_mcp_screen_stocks_support.py`

- [ ] **Step 1: 현재 import 와 인라인 정의 위치 확인**

```bash
sed -n '1,30p' tests/_mcp_screen_stocks_support.py
```

`from tests._mcp_tooling_support import _patch_runtime_attr` 가 이미 존재한다 (line 26).

- [ ] **Step 2: 기존 import 라인을 확장**

`from tests._mcp_tooling_support import _patch_runtime_attr` 를 다음으로 교체:

```python
from tests._mcp_tooling_support import (
    DummyMCP,
    _TvCondition,
    _TvField,
    _patch_runtime_attr,
    build_tools,
    fake_crypto_tvscreener_module,
)
```

`fake_crypto_tvscreener_module` 는 fixture 함수이므로 import 후 모듈 네임스페이스에 노출되어 pytest 가 자동 발견한다.

- [ ] **Step 3: 인라인 정의 제거**

다음 블록(line 31–89)을 삭제 — 즉 `class _TvCondition:` 로 시작해 `def fake_crypto_tvscreener_module()` 함수 본문 끝까지 (`)` 닫는 줄까지). 정확히는:

- `class _TvCondition:` (line 31) ~ `class DummyMCP:` 직전까지 — 두 헬퍼 클래스
- `class DummyMCP:` ~ `def build_tools()` 직전까지
- `def build_tools()` ~ `@pytest.fixture` 직전까지
- `@pytest.fixture\ndef fake_crypto_tvscreener_module() -> SimpleNamespace:` ~ `)` 닫는 줄까지

이후 다음 fixture (`mock_krx_stocks`)가 바로 따라오게 한다.

검증: 정의 제거 후 파일에서 해당 심볼이 더 이상 정의되지 않는지 확인.

```bash
grep -nE '^(class _TvCondition|class _TvField|class DummyMCP|def build_tools|def fake_crypto_tvscreener_module)' tests/_mcp_screen_stocks_support.py
```

기대 출력: 빈 결과.

- [ ] **Step 4: 사용되지 않는 import 정리**

`from types import SimpleNamespace` 가 fixture 본문 외에서 사용되는지 확인. 사용되지 않으면 제거.

```bash
grep -n SimpleNamespace tests/_mcp_screen_stocks_support.py
```

남아있는 사용처가 있으면 import 유지, 없으면 import 라인 제거. `from unittest.mock import AsyncMock` 등 다른 import 도 같이 점검.

- [ ] **Step 5: 모듈 import & lint 확인**

```bash
uv run python -c "import tests._mcp_screen_stocks_support; print('imported ok')"
uv run ruff check tests/_mcp_screen_stocks_support.py
```

기대: 둘 다 에러 0. 만약 `_TvField` 등 정의가 다른 곳(예: 동일 파일의 fixture)에서 직접 사용된다면 import 경로가 정확한지 확인.

- [ ] **Step 6: 영향받는 테스트 실행**

```bash
uv run pytest tests/_mcp_screen_stocks_support.py -x -q 2>&1 | tail -10
```

기대: 모두 pass. 실패하면 **이 시점의 변경분만** 되돌리고 무엇이 깨졌는지 보고. 일반적인 실패 원인:
- import 누락: `_TvField` 등을 직접 사용하는 fixture 가 import 에서 빠졌을 때
- fixture 이름 충돌: `_mcp_tooling_support.py` 에 fixture 가 여러 개 정의돼있어 다른 이름과 충돌

- [ ] **Step 7: 커밋**

```bash
git add tests/_mcp_screen_stocks_support.py
git commit -m "$(cat <<'EOF'
test(mcp): remove inline helper redefinitions from screen_stocks support

Imports DummyMCP / _TvCondition / _TvField / build_tools /
fake_crypto_tvscreener_module from tests._mcp_tooling_support, which is
already declared as the single source of truth in its module docstring.

Phase 2/4 of MCP screen_stocks fixture consolidation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `test_crypto_composite_score.py` 인라인 헬퍼 제거

**Files:**
- Modify: `tests/test_crypto_composite_score.py`

- [ ] **Step 1: 인라인 정의 위치 재확인**

```bash
grep -nE '^(class _TvCondition|class _TvField|class DummyMCP|def build_tools|@pytest.fixture\b|def fake_crypto)' tests/test_crypto_composite_score.py
```

대상 라인 (대략): 37 (`class DummyMCP`), 49 (`def build_tools`), 55 (`class _TvCondition`), 66 (`class _TvField`), 75 (`@pytest.fixture` for `fake_crypto_tvscreener_module`).

- [ ] **Step 2: import 추가**

기존 import 블록에 다음을 추가:

```python
from tests._mcp_tooling_support import (
    DummyMCP,
    _TvCondition,
    _TvField,
    build_tools,
    fake_crypto_tvscreener_module,
)
```

- [ ] **Step 3: 인라인 정의 제거**

위 5개 정의(클래스 4개 + fixture 함수 1개)를 모두 삭제. `from typing import Any, cast` 의 `cast` 가 다른 곳에서 더 사용되는지 확인 후, 미사용이면 그것도 정리.

검증:

```bash
grep -nE '^(class _TvCondition|class _TvField|class DummyMCP|def build_tools|def fake_crypto_tvscreener_module)' tests/test_crypto_composite_score.py
```

기대: 빈 결과.

- [ ] **Step 4: 모듈 import & 테스트 실행**

```bash
uv run python -c "import tests.test_crypto_composite_score" 
uv run ruff check tests/test_crypto_composite_score.py
uv run pytest tests/test_crypto_composite_score.py -x -q 2>&1 | tail -10
```

기대: 모두 통과. `cast` 등 미사용 import 가 있으면 ruff 가 가르쳐줌 — 정리.

- [ ] **Step 5: 커밋**

```bash
git add tests/test_crypto_composite_score.py
git commit -m "$(cat <<'EOF'
test(crypto): import shared MCP helpers instead of redefining

Removes inline DummyMCP / _TvCondition / _TvField / build_tools /
fake_crypto_tvscreener_module copies; uses tests._mcp_tooling_support
as the single source.

Phase 3/4 of MCP screen_stocks fixture consolidation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `TestScreenStocksTvScreenerContract` 메서드 비교

**Files:**
- Read-only: `tests/_mcp_screen_stocks_support.py`, `tests/test_mcp_screen_stocks_tvscreener_contract.py`

- [ ] **Step 1: 두 클래스에 있는 메서드 셋 추출**

```bash
python3 - <<'PY'
import re
from pathlib import Path

def methods(path, klass):
    text = Path(path).read_text().splitlines()
    in_class = False
    class_indent = None
    out = []
    cur_name = None
    cur_lines = []
    for ln in text:
        m = re.match(r'^class (\w+)', ln)
        if m:
            if in_class and cur_name:
                out.append((cur_name, '\n'.join(cur_lines)))
            in_class = (m.group(1) == klass)
            cur_name = None
            cur_lines = []
            continue
        if in_class:
            mm = re.match(r'    (?:async )?def (\w+)\(', ln)
            if mm:
                if cur_name:
                    out.append((cur_name, '\n'.join(cur_lines)))
                cur_name = mm.group(1)
                cur_lines = [ln]
            elif cur_name is not None:
                cur_lines.append(ln)
    if in_class and cur_name:
        out.append((cur_name, '\n'.join(cur_lines)))
    return dict(out)

s = methods('tests/_mcp_screen_stocks_support.py', 'TestScreenStocksTvScreenerContract')
c = methods('tests/test_mcp_screen_stocks_tvscreener_contract.py', 'TestScreenStocksTvScreenerContract')

s_only = sorted(set(s) - set(c))
c_only = sorted(set(c) - set(s))
both = sorted(set(s) & set(c))

print('SUPPORT-ONLY:')
for n in s_only: print(f'  {n}')
print('CONTRACT-ONLY:')
for n in c_only: print(f'  {n}')
print('BOTH:')
for n in both:
    same = s[n] == c[n]
    print(f'  {n} {"identical" if same else "DIFFERENT"}')

# Save to /tmp for next step
import json
with open('/tmp/contract-methods.json', 'w') as f:
    json.dump({'support': s, 'contract': c, 's_only': s_only, 'c_only': c_only, 'both': both}, f)
print('\nSaved to /tmp/contract-methods.json')
PY
```

기대: `s_only` 6개, `c_only` 5개, `both` 8개 (대부분 DIFFERENT).

- [ ] **Step 2: BOTH 8개 메서드의 diff 출력**

```bash
python3 - <<'PY'
import json, difflib
with open('/tmp/contract-methods.json') as f:
    data = json.load(f)
for name in data['both']:
    s = data['support'][name].splitlines()
    c = data['contract'][name].splitlines()
    if s == c:
        continue
    print(f'\n=== {name} (support → contract) ===')
    diff = list(difflib.unified_diff(s, c, fromfile=f'support:{name}', tofile=f'contract:{name}', lineterm='', n=2))
    print('\n'.join(diff[:80]))
PY
```

각 메서드의 1–2줄 차이를 직접 본다. 일반 패턴:
- 한쪽에서만 추가 assertion 한 줄
- 한쪽이 새 helper 함수(`_install_stock_capabilities` 등)를 호출함
- mock 데이터의 한 필드 추가/제거

각 메서드별 결정:
| 패턴 | 결정 |
|---|---|
| contract 가 더 긴 본문 + helper 호출 | contract 보존 |
| support 가 더 긴 본문 + 추가 assertion | support 본문을 contract 로 복사 |
| 의미적 차이 없음 (코멘트/공백) | contract 보존 |

결정 로그를 작업 메시지에 남긴다 (커밋 메시지에 포함).

- [ ] **Step 3: support 의 git blame 으로 마지막 수정 시점 비교 (참고용)**

```bash
git log -1 --format='%h %ai %s' -- tests/_mcp_screen_stocks_support.py
git log -1 --format='%h %ai %s' -- tests/test_mcp_screen_stocks_tvscreener_contract.py
```

contract 파일이 더 최신이면 “contract 우선” 의 강한 근거. 이 정보는 결정 시 참고만 하고, 실제 결정은 본문 비교로 한다.

- [ ] **Step 4: (이 task 는 분석만 — 코드 변경 없음)**

본 단계 산출물: 동명 8 메서드 각각에 대해 “contract 보존 / support 본문 채택 / 병합” 중 무엇을 할지 결정한 표. Task 7 에서 그대로 적용한다.

---

## Task 6: support-only 6 메서드를 contract 파일로 이전

**Files:**
- Modify: `tests/test_mcp_screen_stocks_tvscreener_contract.py`
- Source (read-only this task): `tests/_mcp_screen_stocks_support.py`

대상 메서드 (support 에만 있음):
1. `test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields`
2. `test_us_category_and_analyst_filter_stay_on_tvscreener_without_network_enrichment`
3. `test_us_enrichment_fallback_only_runs_for_rows_missing_tvscreener_fields`
4. `test_us_enrichment_fallback_preserves_existing_tvscreener_values`
5. `test_us_category_preserves_acronym_case_for_tvscreener_filter`
6. `test_us_category_lowercase_technology_canonicalized_for_tvscreener`
7. `test_us_category_with_max_rsi_falls_back_to_legacy_path`

(실제로는 support 가 16, contract 가 14, 동명이 8 → support-only 가 8 일 수도 있다. Task 5 의 `s_only` 결과를 따르면 정확하다.)

- [ ] **Step 1: contract 파일의 클래스 닫힘 위치 파악**

```bash
grep -n 'class TestScreenStocksTvScreenerContract' tests/test_mcp_screen_stocks_tvscreener_contract.py
tail -5 tests/test_mcp_screen_stocks_tvscreener_contract.py
```

클래스가 파일 끝까지 가는지, 아니면 아래에 모듈 레벨 코드가 더 있는지 확인. 새 메서드는 클래스의 마지막 메서드 뒤에 추가한다 (정렬은 기존 메서드 그룹화에 맞춤 — 예: KR 그룹은 KR 다음, US 그룹은 US 다음).

- [ ] **Step 2: support 에서 한 메서드씩 추출하여 contract 에 붙여 넣음**

각 메서드에 대해:

```bash
# 예시: test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields
# support 의 정의 시작 라인 찾기
grep -n 'def test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields' tests/_mcp_screen_stocks_support.py
```

본문을 통째 복사 (메서드 데코레이터부터 다음 메서드 정의 직전까지). 들여쓰기 4칸 유지. contract 파일의 적절한 위치(같은 KR/US 그룹)에 붙여 넣는다.

contract 파일이 helper 함수(`_stock_capability_snapshot`, `_install_stock_capabilities`)를 사용하는데 옮겨오는 메서드가 같은 helper 의 인라인 버전을 갖고 있다면, 옮겨온 메서드를 contract 의 helper 사용 패턴으로 맞출지 결정:
- helper 호출이 한 번뿐이면 그대로 인라인 유지 (간단)
- helper 호출이 패턴화돼있으면 helper 로 정리

본 task 의 기본 정책: **그대로 옮긴다.** helper 패턴 통일은 후속 PR 의 영역.

- [ ] **Step 3: import 의존성 채움**

옮겨온 메서드가 사용하는 모든 심볼이 contract 파일에 import 되어있는지 확인. 일반적으로 필요한 것:
- `pytest`, `pytest.approx`
- `monkeypatch` (fixture)
- `tools = build_tools()` — `build_tools` import (이미 있을 가능성 높음)
- 도메인 모듈 (예: `app.mcp_server.tooling.screening.kr` 의 mock 대상 경로 — 보통 `monkeypatch.setattr` 의 문자열 경로라 import 필요 없음)
- `_TvField` / `_TvCondition` 사용 시: `_mcp_tooling_support` 에서 import

import 추가:

```python
from tests._mcp_tooling_support import _TvField, _TvCondition, build_tools, DummyMCP
```

(이미 일부가 있으면 합친다.)

- [ ] **Step 4: contract 파일만 import & 컴파일 확인**

```bash
uv run python -c "import tests.test_mcp_screen_stocks_tvscreener_contract; print('ok')"
uv run ruff check tests/test_mcp_screen_stocks_tvscreener_contract.py
```

기대: ok + ruff 0 issue.

- [ ] **Step 5: contract 파일 테스트 실행 — 모든 옮겨온 메서드 포함**

```bash
uv run pytest tests/test_mcp_screen_stocks_tvscreener_contract.py -v 2>&1 | tail -40
```

기대: support-only 였던 메서드들이 이제 contract 파일에서 실행되어 모두 PASS. 동명 메서드는 아직 양쪽에 존재 (Task 7 에서 정리).

failure 가 나면 import 누락이나 fixture 가시성 문제. fixture 가 모듈 스코프이고 옮겨온 메서드가 다른 fixture 를 의존한다면 그 fixture 도 함께 이전 (또는 conftest.py 로 옮기기).

- [ ] **Step 6: 커밋**

```bash
git add tests/test_mcp_screen_stocks_tvscreener_contract.py
git commit -m "$(cat <<'EOF'
test(mcp): migrate support-only TvScreenerContract methods to contract file

Six (or seven) test methods that previously lived in
_mcp_screen_stocks_support.py are now in their canonical home alongside
the rest of the TvScreener public-contract suite. Source file unchanged
in this commit; the support-side class is removed in the next task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 동명 8 메서드 정본 결정 및 정리

**Files:**
- Modify: `tests/test_mcp_screen_stocks_tvscreener_contract.py`

Task 5 에서 결정한 표를 그대로 적용한다.

대상 메서드 (양쪽 모두 존재):
1. `test_kr_tvscreener_path_preserves_public_response_contract`
2. `test_us_tvscreener_path_preserves_public_response_contract`
3. `test_kr_default_stock_request_uses_tvscreener_without_legacy_rsi_path`
4. `test_us_default_stock_request_uses_tvscreener_without_legacy_path`
5. `test_kr_stock_request_with_max_rsi_still_uses_tvscreener`
6. `test_us_stock_request_with_max_rsi_still_uses_tvscreener`
7. `test_us_tvscreener_error_falls_back_to_legacy_path`
8. `test_kr_tvscreener_path_passes_requested_submarket`
9. `test_kr_category_with_max_rsi_falls_back_to_legacy_path`

(Task 5 의 `both` 결과가 정확한 목록.)

- [ ] **Step 1: 결정 표 재확인**

Task 5 산출 결정 표에 따라 각 메서드별로 다음 중 하나:
- **contract 보존** — contract 파일에는 변경 없음. 아무 것도 안 함.
- **support 본문 채택** — contract 의 해당 메서드 본문을 support 본문으로 교체.
- **병합** — 양쪽의 추가 assertion / 추가 분기를 통합한 새 본문 작성.

- [ ] **Step 2: contract 파일의 동명 메서드 본문 교체 (필요한 메서드에만)**

Edit 툴로 각 메서드의 본문을 정확히 새 내용으로 교체. 메서드 시그니처(`async def name(self, monkeypatch):`)는 보존.

가장 흔한 “support 본문이 1줄 더 풍부” 시나리오: 추가 assertion (`assert result["meta"]["rsi_enrichment"]["error_samples"] == []` 같은 한 줄)을 contract 의 본문에 추가하고 끝.

- [ ] **Step 3: contract 테스트 재실행**

```bash
uv run pytest tests/test_mcp_screen_stocks_tvscreener_contract.py -v 2>&1 | tail -20
```

기대: 모든 메서드 PASS, 카운트 변화 없음 (메서드 갯수는 같고 본문만 변경됨).

- [ ] **Step 4: 커밋 (변경이 있는 경우만 — 변경이 없으면 이 task 자체를 비커밋으로 마치고 다음 진행)**

```bash
git add tests/test_mcp_screen_stocks_tvscreener_contract.py
git commit -m "$(cat <<'EOF'
test(mcp): unify dual TvScreenerContract method bodies into the contract file

Resolves the eight methods that existed with subtle differences in both
_mcp_screen_stocks_support.py and the contract module. Per-method
decision log:
  - <method_1>: contract preserved
  - <method_2>: support body adopted (extra assertion)
  - ...

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(실제 결정 로그로 채울 것 — placeholder 그대로 두지 말 것.)

---

## Task 8: support 파일에서 `TestScreenStocksTvScreenerContract` 클래스 제거

**Files:**
- Modify: `tests/_mcp_screen_stocks_support.py`

- [ ] **Step 1: 클래스 라인 범위 확정**

```bash
grep -n 'class TestScreenStocksTvScreenerContract' tests/_mcp_screen_stocks_support.py
grep -n '^class ' tests/_mcp_screen_stocks_support.py
```

`TestScreenStocksTvScreenerContract` 가 시작하는 라인 번호와, 그 다음 톱-레벨 정의(`class …` 또는 `@pytest.fixture` 또는 `def …`) 가 시작하는 라인 번호를 확정. 그 사이의 모든 줄이 제거 대상.

- [ ] **Step 2: 클래스 통째 삭제**

확정한 라인 범위를 sed 또는 Edit 툴로 삭제. 들여쓰기 4칸 메서드 본문이 모두 클래스 안에 있다는 사실에 주의.

검증:

```bash
grep -n 'class TestScreenStocksTvScreenerContract' tests/_mcp_screen_stocks_support.py
```

기대: 빈 결과.

- [ ] **Step 3: 다른 클래스가 제거된 메서드의 helper / fixture 를 의존하지 않는지 확인**

옮겨온 메서드들이 사용하던 모듈-레벨 fixture (예: `mock_krx_stocks`, `mock_yfinance_screen`) 가 support 파일에 남아있고, 다른 테스트 클래스(`TestScreenStocksKR`, `TestScreenStocksUS` 등)도 같은 fixture 를 사용 중이라면 — fixture 는 그대로 두어야 한다.

확인:

```bash
grep -n 'mock_krx_stocks\|mock_yfinance_screen\|mock_upbit_coins\|mock_valuation_data' tests/_mcp_screen_stocks_support.py
```

각 fixture 가 다른 곳에서 (지금 제거된 클래스 외에) 여전히 사용 중인지 확인. 사용 안 되는 게 있다면 (가능성 낮음) 함께 제거.

- [ ] **Step 4: support 파일 전체 테스트 실행**

```bash
uv run pytest tests/_mcp_screen_stocks_support.py -v 2>&1 | tail -30
```

기대: 남은 5+ 테스트 클래스 (`TestScreenStocksKR`, `TestScreenStocksKRRegression`, `TestScreenStocksUS`, `TestScreenStocksCrypto`, `TestScreenStocksFundamentalsExpansion`, `TestScreenStocksRsiLogging`) 와 `test_screen_stocks_smoke` 가 모두 PASS.

- [ ] **Step 5: 라인 수 확인 (단축 효과 측정)**

```bash
wc -l tests/_mcp_screen_stocks_support.py
```

기대: 4214 → 약 3,000 (1,000~1,200 줄 감소).

- [ ] **Step 6: 커밋**

```bash
git add tests/_mcp_screen_stocks_support.py
git commit -m "$(cat <<'EOF'
test(mcp): remove dup TvScreenerContract class from screen_stocks support

The single source for these contract tests is now
tests/test_mcp_screen_stocks_tvscreener_contract.py. The support module
keeps its remaining domain test classes (KR/US/Crypto/Fundamentals/RSI).

Phase 4/4 of MCP screen_stocks fixture consolidation. Drops ~1,000
lines and removes the parallel-execution of the same intent from two
modules.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 종합 검증

**Files:** 없음 (검증만)

- [ ] **Step 1: pytest collection 중복 검사**

```bash
uv run pytest --collect-only -q \
  tests/_mcp_screen_stocks_support.py \
  tests/test_mcp_screen_stocks_tvscreener_contract.py \
  tests/test_mcp_screen_stocks_filters_and_rsi.py \
  tests/test_mcp_screen_stocks_crypto.py \
  tests/test_crypto_composite_score.py \
  tests/_mcp_tooling_support.py 2>&1 | \
  awk -F'::' 'NF>=2{print $2"::"$NF}' | sort | uniq -d | tee /tmp/dup-nodeids.txt
```

기대 출력: 빈 파일 (동명 nodeID 가 두 모듈에서 발견되지 않음). 결과가 비어있지 않으면 어디 nodeID 가 중복인지 확인하고 Task 7/8 으로 돌아가 재정리.

- [ ] **Step 2: 영향받은 모든 테스트 실행**

```bash
uv run pytest \
  tests/_mcp_screen_stocks_support.py \
  tests/test_mcp_screen_stocks_tvscreener_contract.py \
  tests/test_mcp_screen_stocks_filters_and_rsi.py \
  tests/test_mcp_screen_stocks_crypto.py \
  tests/test_crypto_composite_score.py \
  tests/_mcp_tooling_support.py 2>&1 | tail -10
```

기대: 모두 PASS, 카운트는 baseline ± (8 — 동명 중복) 수준.

- [ ] **Step 3: 더 넓은 회귀 — `_mcp_tooling_support` 를 사용하는 모든 테스트**

```bash
grep -rl '_mcp_tooling_support\|_mcp_screen_stocks_support' tests/ | sort -u
```

해당 파일들 전체를 한 번 실행:

```bash
FILES=$(grep -rl '_mcp_tooling_support\|_mcp_screen_stocks_support' tests/ | sort -u | tr '\n' ' ')
uv run pytest $FILES -q 2>&1 | tail -10
```

기대: 모두 PASS.

- [ ] **Step 4: lint / typecheck**

```bash
make lint
```

ruff 가 clean 인지 확인. 미사용 import 가 있으면 정리하고 추가 커밋.

```bash
make typecheck
```

ty 가 clean 인지 확인. 시그니처 변경이 없으므로 통상 통과.

- [ ] **Step 5: 라인 수 보고서**

```bash
wc -l \
  tests/_mcp_screen_stocks_support.py \
  tests/test_mcp_screen_stocks_tvscreener_contract.py \
  tests/test_crypto_composite_score.py \
  tests/_mcp_tooling_support.py
```

수치를 PR 설명에 포함.

- [ ] **Step 6: (옵션) 변경 후 SonarCloud 추정 효과 메모**

PR 설명에 다음을 포함:

> SonarCloud `duplicated_lines` 영향: support 파일 중복 줄 1,709 가운데 ~1,000 의 해소가 기대되며 (TvScreenerContract 클래스 제거), 인라인 헬퍼 ~150 줄 추가 단축. 세부 수치는 PR 머지 후 재측정.

---

## Task 10: 푸시 및 PR

**Files:** 없음

- [ ] **Step 1: 브랜치 푸시**

```bash
cd /Users/robin/.superset/worktrees/auto_trader/mcp-fixture-consolidation
git push -u origin chore/mcp-screen-stocks-fixture-consolidation
```

- [ ] **Step 2: PR 생성**

```bash
gh pr create --base main \
  --title "test(mcp): consolidate screen_stocks test fixtures and TvScreenerContract" \
  --body "$(cat <<'EOF'
## Summary

- `_mcp_tooling_support.py` 를 진짜 single source of truth 로 회복: `_mcp_screen_stocks_support.py` 와 `test_crypto_composite_score.py` 의 인라인 `_TvCondition` / `_TvField` / `DummyMCP` / `build_tools` / `fake_crypto_tvscreener_module` 정의 제거 후 import 로 교체
- `TestScreenStocksTvScreenerContract` 클래스를 `test_mcp_screen_stocks_tvscreener_contract.py` 단일 파일로 통합. 두 모듈에서 살짝 다른 형태로 병행 실행되던 동명 8 메서드를 정본 한 곳에서 유지
- 프로덕션 코드 변경 없음 (테스트 전용)

자세한 동기, 결함 분류, 결정 표는 spec 문서 참고:
`docs/superpowers/specs/2026-05-09-mcp-screen-stocks-fixture-consolidation-design.md`

## Test plan

- [x] `uv run pytest tests/_mcp_screen_stocks_support.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_crypto.py tests/test_crypto_composite_score.py -v` — all green
- [x] pytest `--collect-only` 에서 동명 nodeID 가 두 모듈에 동시에 잡히지 않음
- [x] `make lint`, `make typecheck` clean
- [ ] (post-merge) SonarCloud `mgh3326_auto_trader` 의 `duplicated_lines` 감소 확인

## Out of scope

- `_mcp_screen_stocks_support.py` 의 남은 5 테스트 클래스를 도메인별 파일로 분리 (별 PR — 디자인 문서의 “스코프 C”)
- 다른 도메인(market_events, research_reports, …) 의 fixture 통합

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: PR URL 출력**

`gh pr create` 의 stdout 마지막 줄이 PR URL. 이를 사용자에게 보고한다.

- [ ] **Step 4: PR CI 트리거 확인**

```bash
gh pr checks
```

기대: CI 가 트리거됨. CI 결과는 본 작업 범위가 아니므로 사용자가 별도 확인.

---

## Self-Review

- ✅ Spec 의 “변경 1 — 헬퍼 import 통일” → Task 2/3/4 가 다룸
- ✅ Spec 의 “변경 2 — TvScreenerContract 통합” → Task 5/6/7/8 이 다룸
- ✅ Spec 의 “검증” → Task 9 가 다룸
- ✅ Spec 의 “비-목표” (support 분할) — 본 plan 도 비-대상으로 명시
- ✅ Placeholder 제거: 모든 step 에 실행 가능한 명령 / 코드 블록 / 기대 출력 포함. 단 한 곳, Task 7 의 commit message 안에 “결정 로그 placeholder” 가 있으나 이는 의도된 — 실제 결정에 따라 채울 부분.
- ✅ 타입/메서드 이름 일관성 (메서드 셋이 Task 5/6/7 에서 동일 목록 사용)
- ✅ Worktree/브랜치 명시
