# ROB-501 Runtime LLM Provider Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all in-process LLM provider surfaces from auto_trader runtime app code while preserving out-of-process Hermes, MCP consumer, TradingAgents, and lab-script workflows.

**Architecture:** Add a repo-wide static guard over `app/**/*.py`, then delete the unused runtime provider adapters, Gemini-specific rate limiter, and research summary LLM hook. Clean runtime config, dependency metadata, env examples, test bootstrap defaults, and operator docs so they no longer advertise in-process Gemini/OpenAI/Grok paths.

**Tech Stack:** Python 3.13, FastAPI app runtime, pytest, Ruff, uv/pyproject/uv.lock, Linear policy labels.

---

## File Structure

- Modify: `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` — expand the ROB-287 guard from two report-generation directories to all runtime app Python files.
- Delete: `app/services/ai_providers/__init__.py`
- Delete: `app/services/ai_providers/base.py`
- Delete: `app/services/ai_providers/gemini_provider.py`
- Delete: `app/services/ai_providers/openai_provider.py`
- Delete: `app/core/model_rate_limiter.py`
- Modify: `app/analysis/debate.py` — remove `ModelRunner`, `model_runner`, and `_build_llm_debate()`.
- Modify: `tests/analysis/test_debate.py` — remove the LLM-path test and keep deterministic reducer coverage.
- Modify: `app/core/config.py` — remove unused AI Advisor settings.
- Modify: `tests/conftest.py` — remove Google/Gemini/OpenAI/Grok/AI Advisor test defaults and the unused Gemini response fixture.
- Modify: `tests/test_config.py` — remove stale `GOOGLE_API_KEY` from config loading smoke env.
- Modify: `pyproject.toml` — remove `openai`, `google-genai`, and the `google.genai.types` warning filter.
- Modify: `uv.lock` — regenerate after dependency removal with `uv lock`.
- Modify: `env.example` — remove Google Gemini API and AI Advisor env sections.
- Modify: `env.prod.example` — remove Google Gemini API env section.
- Modify: `scripts/setup-test-env.sh` — remove generated dummy `GOOGLE_API_KEY(S)` cases.
- Modify: `README.md` — remove Google Gemini from required env list.
- Modify: `CLAUDE.md` — remove Gemini rate-limiter and Google API env guidance; keep Hermes/TradingAgents out-of-process guidance.
- Modify: `app/core/AGENTS.md` — remove `ModelRateLimiter` as a shared core utility.
- Modify: `app/analysis/AGENTS.md` — describe `app/analysis` as deterministic research pipeline code and remove obsolete `analyzer.py`/Gemini limiter guidance.

Do not modify `scripts/news_issue_lab.py`, `tests/test_news_issue_lab.py`, `docs/runbooks/news-issue-lab.md`, `docs/archive/*`, old `docs/plans/*`, or old `docs/superpowers/plans/*` history in this implementation.

### Task 1: Expand Runtime Static Guard

**Files:**
- Modify: `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`

- [ ] **Step 1: Replace the guard test with a repo-wide app runtime guard**

Replace the entire file with:

```python
"""ROB-501 — runtime in-process LLM static guard.

The auto_trader runtime app is deterministic evidence, validation, and
persistence code. LLM judgment is owned by MCP consumers or Hermes outside this
process. This guard scans all ``app/**/*.py`` runtime files and fails if an
in-process LLM provider surface is reintroduced.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

GUARDED_PATHS: tuple[pathlib.Path, ...] = (REPO_ROOT / "app",)

FORBIDDEN_IMPORT_MODULES: frozenset[str] = frozenset(
    {
        "app.core.model_rate_limiter",
        "app.services.ai_providers",
        "google.genai",
        "google.generativeai",
        "openai",
    }
)

FORBIDDEN_DEFINED_NAMES: frozenset[str] = frozenset(
    {
        "AiProvider",
        "AiProviderError",
        "AiProviderResult",
        "GeminiProvider",
        "ModelRateLimiter",
        "ModelRunner",
        "OpenAIProvider",
        "RateLimitedGeminiProvider",
    }
)

FORBIDDEN_RUNTIME_FILES: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "app" / "core" / "model_rate_limiter.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "__init__.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "base.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "gemini_provider.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "openai_provider.py",
)


def _iter_python_files(roots: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            p
            for p in root.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        )
    return sorted(files)


def _imports_in(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def _attribute_accesses(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            found.add(f"{node.value.id}.{node.attr}")
    return found


def _forbidden_import_matches(imports: set[str]) -> set[str]:
    matches: set[str] = set()
    for imported in imports:
        for forbidden in FORBIDDEN_IMPORT_MODULES:
            if imported == forbidden or imported.startswith(f"{forbidden}."):
                matches.add(imported)
    return matches


@pytest.mark.parametrize("path", _iter_python_files(GUARDED_PATHS))
def test_no_runtime_in_process_llm_imports(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = _imports_in(tree)
    definitions = _defined_names(tree)
    accesses = _attribute_accesses(tree)

    offending_imports = _forbidden_import_matches(imports)
    offending_definitions = definitions & FORBIDDEN_DEFINED_NAMES
    offending_accesses = {
        access
        for access in accesses
        if access.endswith(".ask")
        and any(
            access.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_DEFINED_NAMES
        )
    }

    if offending_imports or offending_definitions or offending_accesses:
        rel = path.relative_to(REPO_ROOT)
        messages: list[str] = []
        if offending_imports:
            messages.append(f"imports forbidden modules: {sorted(offending_imports)!r}")
        if offending_definitions:
            messages.append(
                f"defines forbidden names: {sorted(offending_definitions)!r}"
            )
        if offending_accesses:
            messages.append(f"calls forbidden .ask: {sorted(offending_accesses)!r}")
        pytest.fail(
            "ROB-501 guard violated — "
            f"{rel} re-introduced an in-process LLM runtime surface: "
            + "; ".join(messages)
        )


def test_forbidden_runtime_llm_files_are_absent() -> None:
    existing = [p.relative_to(REPO_ROOT) for p in FORBIDDEN_RUNTIME_FILES if p.exists()]
    assert existing == [], f"forbidden runtime LLM files still exist: {existing!r}"


def test_guard_paths_actually_scan_app_runtime() -> None:
    for root in GUARDED_PATHS:
        assert root.exists(), f"guard root missing: {root}"
    files = _iter_python_files(GUARDED_PATHS)
    assert len(files) > 100, "expected guard to inspect the app runtime package"
```

- [ ] **Step 2: Run the guard and confirm it fails before deletion**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q
```

Expected: FAIL. The failure should mention existing forbidden runtime surfaces such as `app/services/ai_providers/gemini_provider.py`, `app/services/ai_providers/openai_provider.py`, `app/core/model_rate_limiter.py`, or `ModelRunner`.

- [ ] **Step 3: Commit the failing guard**

```bash
git add tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py
git commit -m "test(ROB-501): guard app runtime against in-process LLM providers"
```

### Task 2: Delete Provider Surface and Deterministic-Only Debate

**Files:**
- Delete: `app/services/ai_providers/__init__.py`
- Delete: `app/services/ai_providers/base.py`
- Delete: `app/services/ai_providers/gemini_provider.py`
- Delete: `app/services/ai_providers/openai_provider.py`
- Delete: `app/core/model_rate_limiter.py`
- Modify: `app/analysis/debate.py`
- Modify: `tests/analysis/test_debate.py`

- [ ] **Step 1: Delete the unused runtime provider and limiter files**

Use file deletion through the editor/apply-patch mechanism for:

```text
app/services/ai_providers/__init__.py
app/services/ai_providers/base.py
app/services/ai_providers/gemini_provider.py
app/services/ai_providers/openai_provider.py
app/core/model_rate_limiter.py
```

- [ ] **Step 2: Remove the LLM hook from `app/analysis/debate.py`**

In `app/analysis/debate.py`, replace the imports and `build_summary`/LLM-hook area at the top with:

```python
"""ROB-112 — deterministic research summary builder with citation links."""

from app.schemas.research_pipeline import (
    BullBearArgument,
    PriceAnalysis,
    StageOutput,
    StageVerdict,
    SummaryDecision,
    SummaryOutput,
)


class StageLinkSpec:
    def __init__(
        self,
        stage_analysis_id: int,
        weight: float = 1.0,
        direction: str = "support",
        rationale: str | None = None,
    ):
        self.stage_analysis_id = stage_analysis_id
        self.weight = weight
        self.direction = direction
        self.rationale = rationale


async def build_summary(
    stage_outputs: dict[int, StageOutput],
) -> tuple[SummaryOutput, list[StageLinkSpec]]:
    """Build a deterministic summary from research stage outputs."""

    warnings = []
    stale_count = 0

    for output in stage_outputs.values():
        if output.verdict == StageVerdict.UNAVAILABLE:
            reason = "not_implemented"
            if hasattr(output.signals, "reason"):
                reason = output.signals.reason
            warnings.append(f"{output.stage_type}: UNAVAILABLE ({reason})")

        if output.source_freshness and output.source_freshness.stale_flags:
            stale_count += 1

    force_hold = False
    if stale_count >= 2:
        force_hold = True
        warnings.append(f"Forcing HOLD: {stale_count} stages have stale data.")

    return _build_deterministic_v1(
        stage_outputs, force_hold=force_hold, warnings=warnings
    )
```

Keep the existing `_build_deterministic_v1()` body below it. Remove the old `ModelRunner` protocol and `_build_llm_debate()` function entirely.

- [ ] **Step 3: Remove the LLM path test**

In `tests/analysis/test_debate.py`, delete this whole test block:

```python
@pytest.mark.asyncio
async def test_build_summary_llm_path():
    async def mock_runner(prompt, **kwargs):
        return {"decision": "buy"}

    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.BULL),
    }

    summary, links = await build_summary(stage_outputs, model_runner=mock_runner)

    assert summary.model_name == "mock-llm"
    assert summary.raw_payload == {"simulation": True}
    assert summary.token_input == 100
    assert summary.token_output == 50
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py tests/analysis/test_debate.py -q
```

Expected: PASS. The guard should no longer find provider imports/files or `ModelRunner`.

- [ ] **Step 5: Run research pipeline regression tests**

Run:

```bash
uv run pytest tests/analysis/test_pipeline.py tests/analysis/test_pipeline_safety.py tests/core/test_research_pipeline_flags.py -q
```

Expected: PASS. `run_research_session()` should still call `build_summary(stage_outputs_map)` without a `model_runner`.

- [ ] **Step 6: Commit runtime deletion**

```bash
git add app/analysis/debate.py tests/analysis/test_debate.py tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py
git add -u app/services/ai_providers app/core/model_rate_limiter.py
git commit -m "refactor(ROB-501): remove runtime in-process LLM provider surface"
```

### Task 3: Clean Runtime Config, Test Defaults, and Dependencies

**Files:**
- Modify: `app/core/config.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Remove AI Advisor settings from `app/core/config.py`**

Delete this block:

```python
    # AI Advisor
    openai_api_key: str | None = None
    gemini_advisor_api_key: str | None = None
    grok_api_key: str | None = None
    ai_advisor_timeout: float = 60.0
    ai_advisor_default_provider: str = "gemini"
```

- [ ] **Step 2: Remove stale LLM test env defaults from `tests/conftest.py`**

From `default_env_values`, remove these keys:

```python
        "GOOGLE_API_KEY": "DUMMY_GOOGLE_API_KEY",
        "GOOGLE_API_KEYS": "DUMMY_GOOGLE_API_KEY_1,DUMMY_GOOGLE_API_KEY_2",
        "OPENAI_API_KEY": "",
        "GEMINI_ADVISOR_API_KEY": "",
        "GROK_API_KEY": "",
        "AI_ADVISOR_TIMEOUT": "60.0",
        "AI_ADVISOR_DEFAULT_PROVIDER": "gemini",
```

Also remove the unused fixture:

```python
@pytest.fixture
def sample_gemini_response():
    """Sample Gemini AI response data."""
    return {
        "text": "Based on technical analysis, this stock shows bullish signals with RSI at 30.5 and MACD crossing above signal line.",
        "confidence": 0.85,
        "recommendation": "BUY",
    }
```

- [ ] **Step 3: Remove stale config smoke env**

In `tests/test_config.py`, remove `"GOOGLE_API_KEY": "test_google_key",` from the `@patch.dict` block in `TestConfigLoading.test_environment_variables_loading`.

- [ ] **Step 4: Remove in-process LLM SDK dependencies from `pyproject.toml`**

Remove these dependencies:

```toml
    "openai>=2.37.0,<2.38.0",
    "google-genai>=1.16.0,<2.0.0",
```

Remove the `google.genai.types` warning filter block:

```toml
filterwarnings = [
    "ignore::pydantic.warnings.PydanticDeprecatedSince212:google.genai.types"
]
```

- [ ] **Step 5: Regenerate the lock file**

Run:

```bash
uv lock
```

Expected: PASS. `uv.lock` should drop the direct `openai` and `google-genai` dependency graph if no remaining dependency requires them.

- [ ] **Step 6: Run config/dependency tests**

Run:

```bash
uv run pytest tests/test_config.py tests/core/test_research_pipeline_flags.py -q
uv lock --check
```

Expected: both commands PASS.

- [ ] **Step 7: Commit config and dependency cleanup**

```bash
git add app/core/config.py tests/conftest.py tests/test_config.py pyproject.toml uv.lock
git commit -m "chore(ROB-501): remove in-process LLM config and dependencies"
```

### Task 4: Clean Operator Docs and Knowledge Files

**Files:**
- Modify: `env.example`
- Modify: `env.prod.example`
- Modify: `scripts/setup-test-env.sh`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `app/core/AGENTS.md`
- Modify: `app/analysis/AGENTS.md`

- [ ] **Step 1: Remove Gemini and AI Advisor env examples**

In `env.example`, delete the Google Gemini API section:

```dotenv
# ========================================
# Google Gemini API 설정
# ========================================
# 단일 Google API 키 (기본값)
GOOGLE_API_KEY=your_google_api_key_here
# 여러 Google API 키 (콤마로 구분, 선택사항)
GOOGLE_API_KEYS=key1,key2,key3
```

In `env.example`, delete the AI Advisor section:

```dotenv
# AI Advisor (optional - for portfolio AI consultation)
OPENAI_API_KEY=
GEMINI_ADVISOR_API_KEY=
GROK_API_KEY=
AI_ADVISOR_TIMEOUT=60.0
AI_ADVISOR_DEFAULT_PROVIDER=gemini
```

In `env.prod.example`, delete:

```dotenv
# Google API (for Gemini AI)
GOOGLE_API_KEY=your_google_api_key
GOOGLE_API_KEYS=["your_google_api_key_1","your_google_api_key_2"]
```

- [ ] **Step 2: Remove setup-test-env Google cases**

In `scripts/setup-test-env.sh`, delete these cases:

```bash
            "GOOGLE_API_KEY")
                echo "GOOGLE_API_KEY=DUMMY_GOOGLE_API_KEY" >> "$OUTPUT_TARGET"
                ;;
            "GOOGLE_API_KEYS")
                echo "GOOGLE_API_KEYS=DUMMY_GOOGLE_API_KEY_1,DUMMY_GOOGLE_API_KEY_2" >> "$OUTPUT_TARGET"
                ;;
```

- [ ] **Step 3: Remove README required Gemini env**

In `README.md`, remove this bullet from the required env list:

```markdown
- `GOOGLE_API_KEY`: Google Gemini API 키
```

- [ ] **Step 4: Update `CLAUDE.md` runtime guidance**

Remove the Redis Gemini rate-limiter section that names:

```text
app/core/model_rate_limiter.py
app/analysis/analyzer.py
model_rate_limit:{model}:{masked_api_key}
GOOGLE_API_KEY
GOOGLE_API_KEYS
```

Replace it with this concise policy note near the prior location:

```markdown
### Runtime LLM ownership boundary

auto_trader runtime code must not import or instantiate in-process LLM providers
(Gemini/OpenAI/Grok/etc.). LLM judgment belongs to MCP consumers or Hermes
out-of-process flows. The static guard in
`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`
scans `app/**/*.py` for forbidden provider imports and deleted provider files.
```

In the required env section, delete the `# Google AI`, `GOOGLE_API_KEY`, and `GOOGLE_API_KEYS` lines. Keep KIS, Upbit, database, Redis, Telegram, OpenDART, and `TRADINGAGENTS_*` guidance.

Delete the troubleshooting subsection for Google API 429 errors.

- [ ] **Step 5: Update child AGENTS knowledge files**

In `app/core/AGENTS.md`, remove the table row:

```markdown
| Model rate limiter | `app/core/model_rate_limiter.py` | Shared LLM model availability/rate-limiting state |
```

In `app/analysis/AGENTS.md`, replace the overview with:

```markdown
`app/analysis/` contains the deterministic research pipeline stages and summary reducer used by `ResearchPipelineService`.
```

In `app/analysis/AGENTS.md`, remove references to:

```text
app/analysis/analyzer.py
app/analysis/service_analyzers.py
app/analysis/prompt.py
app/analysis/news_prompt.py
app/core/model_rate_limiter.py
Do not bypass model-rate-limiter checks in LLM call paths.
```

Add this convention:

```markdown
- Do not add in-process LLM providers or model-runner hooks here; LLM reasoning belongs to MCP consumers or Hermes out of process.
```

- [ ] **Step 6: Run doc/config search**

Run:

```bash
rg -n "GOOGLE_API_KEY|GOOGLE_API_KEYS|GEMINI_ADVISOR|AI_ADVISOR_DEFAULT_PROVIDER|app/core/model_rate_limiter.py|app/analysis/analyzer.py|Google API 429|Redis 기반 스마트 모델 제한" env.example env.prod.example README.md CLAUDE.md app/core/AGENTS.md app/analysis/AGENTS.md scripts/setup-test-env.sh tests/conftest.py tests/test_config.py app/core/config.py pyproject.toml
```

Expected: no matches.

- [ ] **Step 7: Commit docs and knowledge cleanup**

```bash
git add env.example env.prod.example scripts/setup-test-env.sh README.md CLAUDE.md app/core/AGENTS.md app/analysis/AGENTS.md
git commit -m "docs(ROB-501): remove runtime Gemini provider guidance"
```

### Task 5: Final Verification and Review Hold

**Files:**
- No code changes expected.
- Linear: ROB-501 comment/labels.

- [ ] **Step 1: Run the focused verification suite**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q
uv run pytest tests/analysis/test_debate.py tests/analysis/test_pipeline.py tests/analysis/test_pipeline_safety.py tests/core/test_research_pipeline_flags.py tests/test_config.py -q
uv run ruff check app/analysis/debate.py tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py tests/analysis/test_debate.py tests/conftest.py tests/test_config.py app/core/config.py
uv lock --check
```

Expected: all commands PASS.

- [ ] **Step 2: Run final runtime-boundary search**

Run:

```bash
rg -n "from openai|import openai|google\\.genai|google\\.generativeai|google import genai|GeminiProvider|OpenAIProvider|AiProvider|ModelRateLimiter|ModelRunner|GOOGLE_API_KEY|GOOGLE_API_KEYS|GEMINI_ADVISOR|AI_ADVISOR" app tests/conftest.py tests/test_config.py env.example env.prod.example README.md CLAUDE.md pyproject.toml
```

Expected: no runtime/config matches except the static guard's forbidden-name constants and explanatory policy text. Do not treat old historical docs outside this search scope as blockers.

- [ ] **Step 3: Check working tree**

Run:

```bash
git status --short
```

Expected: clean after the task commits, or only expected uncommitted files if the implementer intentionally deferred commits.

- [ ] **Step 4: Apply final review hold in Linear**

Add a Linear ROB-501 comment:

```markdown
Implementation is ready for ROB-501, but hold_for_final_review applies because this changes the runtime LLM ownership boundary. No merge, deploy, or operational use until CTO/Opus review clears the boundary and guard coverage.
```

If not already present, add the `hold_for_final_review` label to ROB-501.

- [ ] **Step 5: Final handoff**

Summarize:

```text
ROB-501 runtime in-process LLM provider removal is implemented.
Removed: app/services/ai_providers/*, app/core/model_rate_limiter.py, model_runner LLM hook, AI Advisor config/env/deps.
Preserved: Hermes/MCP/out-of-process ownership, TradingAgents settings, lab scripts, historical docs.
Verification: <paste command results>.
Review hold: high_risk_change + needs_stronger_model_review + candidate_for_opus + hold_for_final_review.
```
