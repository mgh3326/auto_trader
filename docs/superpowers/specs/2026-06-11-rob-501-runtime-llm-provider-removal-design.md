# ROB-501 Runtime LLM Provider Removal Design

## Goal

Remove the in-process LLM provider surface from the auto_trader runtime so LLM judgment is owned only by MCP consumers or Hermes. auto_trader remains a deterministic evidence, validation, and persistence layer.

## Locked Decisions

1. Scope is option B: remove the runtime in-process LLM provider surface, not Gemini-only cleanup.
2. The guarded scope is runtime app code plus operator-facing config/docs/tests. It includes `app/`, `pyproject.toml`, `env.example`, `env.prod.example`, `README.md`, `CLAUDE.md`, and test bootstrap defaults.
3. Lab and historical material is out of scope. Do not modify `scripts/news_issue_lab.py`, `tests/test_news_issue_lab.py`, `docs/runbooks/news-issue-lab.md`, `docs/archive/*`, or old `docs/plans/*` / `docs/superpowers/plans/*` history unless a runtime reference depends on it.
4. Research pipeline behavior stays deterministic. `app.analysis.debate.build_summary()` must not expose a `model_runner` or any future in-process LLM hook.
5. TradingAgents/Hermes remain out-of-process. This work must not remove `TRADINGAGENTS_*`, Hermes ingest/export paths, or OpenClaw callback record fields that only persist upstream model names.
6. ROB-501 is a high-risk policy boundary change. Linear is labeled `high_risk_change`, `needs_stronger_model_review`, and `candidate_for_opus`; implementation must stay held for stronger-model/CTO review before merge or operational use.

## Current Findings

- `app/services/ai_providers/gemini_provider.py` imports `google.genai` and defines `GeminiProvider`; no runtime caller imports it.
- `app/services/ai_providers/openai_provider.py` imports the OpenAI SDK and is also unused at runtime.
- `app/services/ai_providers/base.py` only supports those unused provider adapters.
- `app/core/model_rate_limiter.py` is Gemini-specific and unused by runtime callers.
- `app/analysis/debate.py` contains a `model_runner` hook and `_build_llm_debate()` simulation path even though the default research pipeline path is deterministic.
- `app/core/config.py`, `env.example`, `tests/conftest.py`, `pyproject.toml`, `README.md`, `CLAUDE.md`, `app/core/AGENTS.md`, and `app/analysis/AGENTS.md` still describe or support the old in-process LLM surface.
- The existing ROB-287 guard only scans `app/services/action_report/snapshot_backed/` and `app/services/investment_stages/`. ROB-501 needs the same policy enforced repo-wide across `app/`.

## Architecture

The implementation is a deletion and guard hardening PR. It introduces no new runtime abstraction.

The existing static guard becomes the runtime boundary test for all `app/**/*.py`. It fails if app code imports `openai`, `google.genai`, `google.generativeai`, `app.services.ai_providers`, or `app.core.model_rate_limiter`; defines provider/runner classes such as `GeminiProvider`, `OpenAIProvider`, `AiProvider`, `ModelRateLimiter`, or `ModelRunner`; or keeps the deleted provider/rate-limiter files.

The research pipeline summary builder becomes explicitly deterministic: `build_summary(stage_outputs)` accepts only stage outputs, derives warnings/staleness, and calls `_build_deterministic_v1()`. If a future LLM summary is needed, it must live outside auto_trader and push validated results through Hermes/MCP-owned persistence paths.

Runtime config and dependency metadata are cleaned to match the boundary. `openai`, `google-genai`, Gemini Advisor settings, Google Gemini env examples, and the pydantic warning filter for `google.genai.types` are removed. Historical plans and lab scripts remain untouched to preserve audit history and experimental workflows.

## File Responsibilities

- `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` becomes the repo-wide app runtime static guard.
- `app/analysis/debate.py` keeps deterministic summary reduction only.
- `tests/analysis/test_debate.py` removes the LLM hook regression test and keeps deterministic summary tests.
- `app/services/ai_providers/*` is deleted because there are no runtime consumers.
- `app/core/model_rate_limiter.py` is deleted because the Gemini call path is gone.
- `app/core/config.py`, `tests/conftest.py`, `tests/test_config.py`, `pyproject.toml`, `uv.lock`, `env.example`, `env.prod.example`, `scripts/setup-test-env.sh`, `README.md`, `CLAUDE.md`, `app/core/AGENTS.md`, and `app/analysis/AGENTS.md` are cleaned so runtime docs and bootstrap match the new policy.

## Error Handling

There is no new runtime error path. The intended failure mode is test-time: if an engineer reintroduces an in-process LLM import or file under `app/`, the static guard fails in CI with the offending path and import/name.

Research pipeline summary errors remain unchanged except that the synthetic LLM path disappears. The deterministic reducer is the only supported path.

## Testing

Primary verification:

- `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q`
- `uv run pytest tests/analysis/test_debate.py tests/analysis/test_pipeline.py tests/analysis/test_pipeline_safety.py tests/core/test_research_pipeline_flags.py tests/test_config.py -q`
- `uv run ruff check app/analysis/debate.py tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py tests/analysis/test_debate.py tests/conftest.py tests/test_config.py app/core/config.py`
- `uv lock --check`
- `rg -n "from openai|import openai|google\\.genai|google\\.generativeai|google import genai|GeminiProvider|OpenAIProvider|AiProvider|ModelRateLimiter|ModelRunner|GOOGLE_API_KEY|GOOGLE_API_KEYS|GEMINI_ADVISOR|AI_ADVISOR" app tests/conftest.py tests/test_config.py env.example env.prod.example README.md CLAUDE.md pyproject.toml`

Expected final search result: no runtime/provider/config matches except policy text in the guard test, comments documenting removed paths, or unrelated persisted upstream model names such as OpenClaw callback fixtures.

## Rollback

Rollback is a normal git revert of the ROB-501 PR. Because this is deletion-only with no DB migration and no new runtime state, rollback does not require data migration. If rollback reintroduces in-process LLM providers, the Linear high-risk labels and review hold must remain until the ownership boundary is re-reviewed.

## Self-Review

- Placeholder scan: no unresolved placeholder markers remain.
- Internal consistency: scope consistently excludes lab/historical docs and includes runtime app/config/docs/tests.
- Scope check: one implementation plan is enough because all tasks serve one boundary cleanup.
- Ambiguity check: OpenAI/Grok provider adapters are removed only from in-process runtime surfaces; out-of-process TradingAgents/Hermes paths are explicitly preserved.
