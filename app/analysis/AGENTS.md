# ANALYSIS KNOWLEDGE BASE

## OVERVIEW
`app/analysis/` contains the shared LLM analysis pipeline and market-specific analyzer specializations.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Shared analysis pipeline | `app/analysis/analyzer.py` | Prompt assembly, model retry/rotation, DB save path |
| Market-specific analyzers | `app/analysis/service_analyzers.py` | `UpbitAnalyzer`, `YahooAnalyzer`, `KISAnalyzer` collectors |
| Analysis response contracts | `app/analysis/models.py` | Structured analysis schema (`StockAnalysisResponse`, ranges) |
| Prompt and formatting helpers | `app/analysis/prompt.py` | Text/JSON prompt builders and formatting utilities |
| Technical indicator enrichment | `app/analysis/indicators.py` | Feature enrichment for analysis input |
| News-oriented prompt path | `app/analysis/news_prompt.py` | Separate prompt surface for news analysis |
| Job entrypoints using analyzers | `app/jobs/analyze.py`, `app/jobs/kis_trading.py` | Main runtime callsites for analyzer flows |
| Limiter and persistence coupling | `app/core/model_rate_limiter.py`, `app/services/stock_info_service.py` | Model availability and stock-info persistence bridge |

## CONVENTIONS
- Keep generic orchestration in `analyzer.py`; market-specific data collection belongs in `service_analyzers.py`.
- Reuse shared analysis schemas in `models.py` for structured outputs.
- Keep prompt behavior centralized in `prompt.py` and `news_prompt.py`.
- Preserve explicit close/cleanup behavior for analyzer instances in job callers.
- Treat model limiter usage as part of analysis runtime contract.

## ANTI-PATTERNS
- Do not move market-specific fetch logic into `analyzer.py`.
- Do not bypass model-rate-limiter checks in LLM call paths.
- Do not write structured analysis results outside established save helpers.
- Do not duplicate prompt-building logic in jobs/services when shared helpers exist.

## NOTES
- Scheduled TaskIQ tasks are declared in `app/tasks/`, not in `app/analysis/`.
- `app/services/llm_news_service.py` shares limiter and prompt-adjacent behavior with this domain.
