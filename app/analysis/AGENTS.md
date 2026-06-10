# ANALYSIS KNOWLEDGE BASE

## OVERVIEW
`app/analysis/` contains the deterministic research pipeline stages and summary reducer used by `ResearchPipelineService`.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Analysis response contracts | `app/analysis/models.py` | Structured analysis schema (`StockAnalysisResponse`, ranges) |
| Technical indicator enrichment | `app/analysis/indicators.py` | Feature enrichment for analysis input |
| Job entrypoints using analyzers | `app/jobs/analyze.py`, `app/jobs/kis_trading.py` | Main runtime callsites for analyzer flows |
| Persistence coupling | `app/services/stock_info_service.py` | Stock-info persistence bridge |

## CONVENTIONS
- Reuse shared analysis schemas in `models.py` for structured outputs.
- Do not add in-process LLM providers or model-runner hooks here; LLM reasoning belongs to MCP consumers or Hermes out of process.

## ANTI-PATTERNS
- Do not write structured analysis results outside established save helpers.

## NOTES
- Scheduled TaskIQ tasks are declared in `app/tasks/`, not in `app/analysis/`.

