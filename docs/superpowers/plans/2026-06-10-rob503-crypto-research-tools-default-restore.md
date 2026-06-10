# ROB-503 크립토 리서치 도구 DEFAULT 복원 + 리네임 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ROB-488이 `MCP_PROFILE=crypto` 전용으로 분리한 크립토 리서치 MCP 도구 12종을 모든 프로파일에 상시 등록(regression 복구)하고, generic 이름인 Binance 파생 3종을 `get_crypto_*`로 리네임한다.

**Architecture:** `registry.py`의 `include_crypto` 게이트와 그 파라미터 배선을 제거해 크립토 read-only 리서치 도구를 무조건 등록한다. 주문 surface의 프로파일 분기(`McpProfile` 체계)는 무변경. 리네임은 `@mcp.tool(name=...)` 레벨만 — impl/handle 함수와 서비스 레이어는 그대로.

**Tech Stack:** Python 3.13, FastMCP, pytest (`DummyMCP` 등록 테스트 — 실 HTTP 없음), uv.

**Spec:** `docs/superpowers/specs/2026-06-10-rob503-crypto-research-tools-default-restore-design.md`

**Worktree/Branch:** `/Users/mgh3326/work/auto_trader.rob-503`, branch `rob-503` (이미 체크아웃됨)

**중요 제약:**
- migration 0. 브로커/주문 mutation 경로 무변경.
- impl 식별자(`get_fear_greed_index_impl`, `handle_get_funding_rate`, `handle_get_open_interest`, `handle_get_long_short_ratio`)는 **리네임 금지** — `app/jobs/daily_scan.py`, `app/services/invest_view_model/market_dashboard_service.py`가 직접 import한다.
- 블랭킷 sed로 `get_funding_rate`를 치환하면 `handle_get_funding_rate`까지 깨진다. 항상 따옴표 포함 패턴(`"get_funding_rate"`)이나 수동 edit 사용.
- lint는 `app/` + `tests/` 둘 다 (CI가 둘 다 검사).

---

## 사전 확인 (변경 대상 좌표, 2026-06-10 기준)

| 파일 | 내용 |
|---|---|
| `app/mcp_server/tooling/registry.py:110-113` | `include_crypto_tools = profile is McpProfile.CRYPTO` 게이트 |
| `app/mcp_server/tooling/registry.py:1-32` | 모듈 docstring의 프로파일→surface 매핑 |
| `app/mcp_server/tooling/fundamentals_registration.py` | `include_crypto` 파라미터 패스스루 |
| `app/mcp_server/tooling/fundamentals_handlers.py:66-110` | `FUNDAMENTALS_TOOL_NAMES` / `CRYPTO_FUNDAMENTALS_TOOL_NAMES` + 주석 |
| `app/mcp_server/tooling/fundamentals_handlers.py` | `if include_crypto:` 블록 3개 (≈145행: `get_crypto_profile`; ≈277행: kimchi/funding/OI/LSR/regime/catalysts/order_flow/social; ≈430행: upbit_index/altseason) |
| `app/mcp_server/tooling/analysis_registration.py:30-31,55-58,312-322` | `include_crypto` 파라미터 + `get_crypto_fear_greed` 게이트 |
| `app/mcp_server/__init__.py:54-60` | `AVAILABLE_TOOL_NAMES`의 "Crypto-profile-only tools" 섹션 |
| `tests/test_mcp_profiles.py` | `_CRYPTO_PROFILE_TOOL_NAMES` + DEFAULT 부재 단언 |
| `tests/test_mcp_fundamentals_tools.py` | `tools["get_funding_rate"]` 등 따옴표 키 ~30곳 |
| `docs/runbooks/rob449-452-mcp-activation.md:3-7` | "MCP_PROFILE=crypto 서버에서만 등록" 노트 |

---

### Task 1: Binance 파생 3종 MCP 도구 리네임

`get_funding_rate` → `get_crypto_funding_rate`, `get_open_interest` → `get_crypto_open_interest`, `get_long_short_ratio` → `get_crypto_long_short_ratio`. 도구 이름과 등록용 set만 변경, handle_* impl은 불변.

**Files:**
- Modify: `tests/test_mcp_profiles.py` (set 갱신 + 옛 이름 부재 테스트)
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py` (`name=` 3건 + set 2개)
- Modify: `app/mcp_server/__init__.py` (`AVAILABLE_TOOL_NAMES` 3건)
- Modify: `tests/test_mcp_fundamentals_tools.py` (따옴표 키 치환)

- [ ] **Step 1.1: 실패하는 테스트 작성 — 프로파일 테스트의 크립토 set을 새 이름으로 갱신**

`tests/test_mcp_profiles.py`에서 `_CRYPTO_PROFILE_TOOL_NAMES` 정의를 다음으로 교체하고, 바로 아래에 `_REMOVED_GENERIC_TOOL_NAMES`를 추가:

```python
_CRYPTO_RESEARCH_TOOL_NAMES = {
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_crypto_fear_greed",
}
# ROB-503: generic 이름은 제거됨 (crypto-only 구현인데 이름이 시장 비특정).
# get_fear_greed_index는 ROB-488에서 get_crypto_fear_greed로 리네임.
_REMOVED_GENERIC_TOOL_NAMES = {
    "get_fear_greed_index",
    "get_funding_rate",
    "get_open_interest",
    "get_long_short_ratio",
}
```

파일 내 `_CRYPTO_PROFILE_TOOL_NAMES`의 나머지 참조 2곳(`TestDefaultProfile.test_does_not_register_split_profile_tools`의 `split_only`, `TestCryptoProfile.test_registers_crypto_profile_tools`)을 `_CRYPTO_RESEARCH_TOOL_NAMES`로 바꾼다. `TestCryptoProfile.test_registers_crypto_profile_tools`에 옛 이름 부재 단언 추가:

```python
    def test_registers_crypto_profile_tools(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert _CRYPTO_RESEARCH_TOOL_NAMES <= mcp.tools.keys()
        assert _REMOVED_GENERIC_TOOL_NAMES.isdisjoint(mcp.tools.keys())
```

- [ ] **Step 1.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_mcp_profiles.py -x -q`
Expected: `TestCryptoProfile::test_registers_crypto_profile_tools` FAIL — `get_crypto_funding_rate` 등이 등록 안 됨 (옛 이름으로 등록 중).

- [ ] **Step 1.3: fundamentals_handlers.py 리네임**

`app/mcp_server/tooling/fundamentals_handlers.py`에서:

1. `FUNDAMENTALS_TOOL_NAMES`와 `CRYPTO_FUNDAMENTALS_TOOL_NAMES` 두 set 모두에서
   `"get_funding_rate"` → `"get_crypto_funding_rate"`, `"get_open_interest"` → `"get_crypto_open_interest"`, `"get_long_short_ratio"` → `"get_crypto_long_short_ratio"`.
2. `@mcp.tool(name="get_funding_rate", ...)` → `name="get_crypto_funding_rate"`, 같은 식으로 2건 더. 데코레이터 아래 로컬 `async def get_funding_rate(...)` 등 클로저 함수명도 새 이름으로 맞춘다 (클로저라 외부 참조 없음 — 가독성용).
3. `handle_get_funding_rate` / `handle_get_open_interest` / `handle_get_long_short_ratio` **import와 호출은 그대로 둔다.**

- [ ] **Step 1.4: `app/mcp_server/__init__.py` AVAILABLE_TOOL_NAMES 갱신**

`AVAILABLE_TOOL_NAMES`에서 같은 3건 리네임. 주석 `# Crypto-profile-only tools`는 Task 2에서 의미가 바뀌므로 여기서 미리 `# Crypto research tools`로 변경.

- [ ] **Step 1.5: test_mcp_fundamentals_tools.py 키 치환 (따옴표 포함 패턴만)**

```bash
sed -i '' \
  -e 's/"get_funding_rate"/"get_crypto_funding_rate"/g' \
  -e 's/"get_open_interest"/"get_crypto_open_interest"/g' \
  -e 's/"get_long_short_ratio"/"get_crypto_long_short_ratio"/g' \
  tests/test_mcp_fundamentals_tools.py
```

치환 후 남은 비-따옴표 옛 이름(모듈 docstring, 클래스 docstring 등 코멘트류)을 확인:

```bash
grep -n "get_funding_rate\|get_open_interest\|get_long_short_ratio" tests/test_mcp_fundamentals_tools.py
```

남은 것이 docstring/주석이면 새 이름으로 수동 수정 (단 `handle_get_*` 형태는 그대로 둔다).

- [ ] **Step 1.6: 테스트 통과 확인**

Run: `uv run pytest tests/test_mcp_profiles.py tests/test_mcp_fundamentals_tools.py -q`
Expected: PASS (전체 green)

- [ ] **Step 1.7: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_mcp_profiles.py tests/test_mcp_fundamentals_tools.py
git commit -m "refactor(ROB-503): Binance 파생 3종 get_crypto_* 리네임 — generic 이름 혼동 해소

호출자 0인 시점(도구가 어느 서버에도 미등록)이라 리네임 비용 없음.
impl(handle_get_*)은 불변.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: include_crypto 게이트 제거 — 크립토 리서치 도구 전 프로파일 등록

**Files:**
- Modify: `tests/test_mcp_profiles.py` (전 프로파일 등록 단언으로 반전)
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/tooling/fundamentals_registration.py`
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py` (`if include_crypto:` 3블록 dedent)
- Modify: `app/mcp_server/tooling/analysis_registration.py`

- [ ] **Step 2.1: 실패하는 테스트 작성 — 전 프로파일 등록 단언**

`tests/test_mcp_profiles.py`에서:

1. `TestDefaultProfile.test_does_not_register_split_profile_tools`의 `split_only`에서 `_CRYPTO_RESEARCH_TOOL_NAMES`를 **제거**:

```python
    def test_does_not_register_split_profile_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        split_only = _US_PAPER_TOOL_NAMES | _DB_PAPER_TOOL_NAMES | KIWOOM_MOCK_TOOL_NAMES
        assert split_only.isdisjoint(mcp.tools.keys())
```

2. 새 테스트 클래스 추가 (파일 끝부분, `TestResolveMcpProfile` 앞):

```python
class TestCryptoResearchToolsAllProfiles:
    """ROB-503: crypto read-only research tools register on EVERY profile.

    ROB-488 had gated them to MCP_PROFILE=crypto, which broke single-server
    operation (crypto live trading runs on the DEFAULT server). Read-only
    tools carry no order-surface risk, so profile isolation buys nothing.
    """

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_crypto_research_tools_registered(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        missing = _CRYPTO_RESEARCH_TOOL_NAMES - mcp.tools.keys()
        assert not missing, f"profile={profile.value} missing: {sorted(missing)}"

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_removed_generic_names_absent(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        leaked = _REMOVED_GENERIC_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"profile={profile.value} leaked old names: {sorted(leaked)}"
```

3. `TestCryptoProfile.test_registers_crypto_profile_tools`는 새 클래스와 중복되므로 삭제 (CRYPTO 고유 단언인 `test_registers_crypto_trading_surface` 등은 유지).

- [ ] **Step 2.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_mcp_profiles.py -q`
Expected: `TestCryptoResearchToolsAllProfiles::test_crypto_research_tools_registered` 가 DEFAULT/HERMES_PAPER_KIS/US_PAPER/DB_PAPER/KIWOOM 5개 프로파일에서 FAIL (CRYPTO만 PASS).

- [ ] **Step 2.3: registry.py 게이트 제거 + docstring 갱신**

`app/mcp_server/tooling/registry.py:109-113`:

```python
    # Always: side-effect-free research + read-only tools
    register_market_data_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)
```

(`include_crypto_tools` 변수와 `include_crypto=` 키워드 삭제)

모듈 docstring 수정 2곳:

```
"default" (McpProfile.DEFAULT):
  All side-effect-free research tools (crypto research included — ROB-503) +
  read-only portfolio tools +
  legacy ambiguous order tools (place_order / cancel_order / modify_order /
  get_order_history with account_mode switching) +
  typed kis_live_* and kis_mock_* variants (additive). Split-profile tools
  (Alpaca/us-dual paper, DB paper, Kiwoom mock) are omitted.
```

```
"crypto" (McpProfile.CRYPTO):
  Default research/read-only surface (crypto research tools register on every
  profile since ROB-503), the generic account_mode order tools (crypto live
  trading entry point), and live_reconcile_orders (US/crypto evidence-gated
  settle).
```

- [ ] **Step 2.4: fundamentals_registration.py 파라미터 제거**

```python
def register_fundamentals_tools(mcp: FastMCP) -> None:
    _register_fundamentals_tools_impl(mcp)
```

(`include_crypto` 키워드 인자와 `*` 제거)

- [ ] **Step 2.5: fundamentals_handlers.py 게이트 3블록 dedent**

1. `_register_fundamentals_tools_impl` 시그니처에서 `*, include_crypto: bool = True` 제거:

```python
def _register_fundamentals_tools_impl(mcp: FastMCP) -> None:
```

2. `if include_crypto:` 3곳(≈145행 `get_crypto_profile` 블록, ≈277행 kimchi~crypto_social 블록, ≈430행 upbit_index/altseason 블록)의 `if` 라인을 삭제하고 블록 본문을 한 단계 dedent. 블록 내용(데코레이터·함수 본문)은 변경하지 않는다.
3. set 위 주석(≈66-68행, ≈97행)을 정정:

```python
# Full fundamentals tool namespace. Crypto research tools register on every
# profile (ROB-503 restored them from the ROB-488 crypto-profile gate).
FUNDAMENTALS_TOOL_NAMES: set[str] = {
```

```python
# Crypto research subset (registered on all profiles; kept as metadata for
# tests/surface audits).
CRYPTO_FUNDAMENTALS_TOOL_NAMES: set[str] = {
```

- [ ] **Step 2.6: analysis_registration.py 파라미터 제거 + dedent**

1. 시그니처: `def register_analysis_tools(mcp: FastMCP) -> None:` (`*, include_crypto` 제거)
2. ≈312행 `if include_crypto:` 삭제, `get_crypto_fear_greed` 데코레이터+함수 dedent.
3. ≈30-31행 주석 정정:

```python
# Full analysis tool namespace. get_crypto_fear_greed registers on every
# profile (ROB-503).
ANALYSIS_TOOL_NAMES: set[str] = {
```

- [ ] **Step 2.7: 포맷 + 테스트 통과 확인**

```bash
uv run ruff format app/mcp_server/ tests/test_mcp_profiles.py
uv run ruff check app/ tests/
uv run pytest tests/test_mcp_profiles.py tests/test_mcp_fundamentals_tools.py -q
```

Expected: ruff clean, 전체 PASS. (dedent 후 ruff format이 필수 — CI lint는 app/+tests/ 둘 다 검사)

- [ ] **Step 2.8: include_crypto 잔존 참조 0 확인**

```bash
grep -rn "include_crypto" app/mcp_server/ tests/test_mcp_profiles.py
```

Expected: `news_handlers.py`의 `include_crypto_relevance`(무관한 다른 파라미터)만 매치. `include_crypto`(정확 일치)는 0건.

- [ ] **Step 2.9: Commit**

```bash
git add app/mcp_server/tooling/registry.py app/mcp_server/tooling/fundamentals_registration.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/tooling/analysis_registration.py tests/test_mcp_profiles.py
git commit -m "fix(ROB-503): 크립토 리서치 도구 12종 전 프로파일 상시 등록 — include_crypto 게이트 제거

ROB-488 프로파일 분리 후 PROFILE cutover 미수행 상태로 재배포되어
DEFAULT 서버에서 크립토 레짐/파생 도구가 일괄 소실된 regression 복구.
read-only 도구라 프로파일 격리 실익이 없고, 크립토 라이브 운영(BTC 래더)이
DEFAULT 서버에서 이루어지는 현실을 반영. 주문 surface 프로파일 분기는 불변.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: 런북/문서 정정 + 전수 검증

**Files:**
- Modify: `docs/runbooks/rob449-452-mcp-activation.md:3-7`
- Verify: 레포 전체 옛 이름 잔존 grep, 풀 게이트

- [ ] **Step 3.1: 런북 노트 교체**

`docs/runbooks/rob449-452-mcp-activation.md` 상단 blockquote(3-7행)를 다음으로 교체:

```markdown
> **MCP_PROFILE (ROB-488 → ROB-503)**: 크립토 리서치 도구는 ROB-488에서
> `MCP_PROFILE=crypto` 전용으로 분리되었다가, **ROB-503에서 모든 프로파일 상시
> 등록으로 복원**되었다 (read-only라 프로파일 격리 실익이 없고, 크립토 라이브
> 운영이 default 서버에서 이루어짐). Binance 파생 3종은
> `get_crypto_funding_rate` / `get_crypto_open_interest` /
> `get_crypto_long_short_ratio`로 리네임되었다 (구 generic 이름 제거).
> `get_crypto_fear_greed`(구 `get_fear_greed_index`)는 ROB-488 이름 유지.
> 주문 surface의 프로파일 분기는 그대로다 (generic 주문 도구 +
> `live_reconcile_orders`는 default/crypto 프로파일에 포함).
```

- [ ] **Step 3.2: 옛 도구 이름 잔존 전수 grep**

```bash
grep -rn '"get_funding_rate"\|"get_open_interest"\|"get_long_short_ratio"\|"get_fear_greed_index"' app/ tests/ docs/runbooks/ scripts/
grep -rn 'get_fear_greed_index\b' app/ --include="*.py" | grep -v "_impl"
```

Expected: 1번째 grep 0건. 2번째 grep 0건 (`get_fear_greed_index_impl`만 존재해야 함 — impl은 의도적 보존). `docs/plans/`·`docs/superpowers/`의 과거 설계 문서는 역사 기록이므로 수정하지 않는다.

- [ ] **Step 3.3: 풀 게이트**

```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
uv run ty check app/
uv run pytest tests/test_mcp_profiles.py tests/test_mcp_fundamentals_tools.py tests/test_crypto_market_regime_tool.py tests/test_daily_scan.py -q
```

Expected: 전부 clean/PASS. (`test_daily_scan`은 `get_fear_greed_index_impl` 직접 사용 — 불변 확인용)

- [ ] **Step 3.4: Commit**

```bash
git add docs/runbooks/rob449-452-mcp-activation.md
git commit -m "docs(ROB-503): 런북 MCP_PROFILE 노트 정정 — 크립토 리서치 도구 전 프로파일 등록 + 리네임 반영

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## 완료 기준 / 운영 후속 (PR 본문에 명시)

- 코드 완료: 위 3 task 커밋 + PR 생성 (base `main`) + CI green
- **operator 후속 (un-Done 게이트)**: 배포 후
  1. MCP 도구 목록에서 12종 노출 확인 (`get_crypto_funding_rate` 등 새 이름 기준)
  2. `get_crypto_fear_greed` / `get_crypto_market_regime` 각 1회 정상 응답
  3. `get_execution_strength` description이 참조하는 `get_crypto_order_flow` 실재 (복원으로 자동 해소)
- 이슈 본문의 "옛 이름 4종은 의도적 제거(리네임)"를 Linear 코멘트로 명시해 operator가 옛 이름으로 재검증하지 않게 한다.
