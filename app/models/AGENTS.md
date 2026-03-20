# MODELS KNOWLEDGE BASE

## OVERVIEW
`app/models/` is the ORM and shared enum/domain-type layer for persistence entities used across routers, services, and auth flows.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Base metadata and table base class | `app/models/base.py` | Declarative base and metadata foundation |
| Trading and auth entities/enums | `app/models/trading.py` | `User`, `UserRole`, market/order entities, shared enums |
| Analysis result entities | `app/models/analysis.py` | Structured analysis persistence tables |
| Prompt persistence | `app/models/prompt.py` | Prompt/result text persistence model |
| Manual holdings entities | `app/models/manual_holdings.py` | Broker/manual holdings and alias entities |
| Symbol trade settings | `app/models/symbol_trade_settings.py` | Per-symbol trading configuration model |
| News entities | `app/models/news.py` | News article persistence model |
| KOSPI200 entities | `app/models/kospi200.py` | KOSPI200 constituent persistence model |
| Public model exports | `app/models/__init__.py` | Curated model and enum export boundary |
| API schema counterparts | `app/schemas/` | DTO layer consuming model semantics |

## CONVENTIONS
- Keep persistence concerns in models; API transport shape belongs in `app/schemas/`.
- Reuse shared enums/types from model modules where already defined.
- Keep model exports in `app/models/__init__.py` intentional and curated.
- Preserve SQLAlchemy relationship and enum conventions used by existing entities.
- Add/modify migrations when model shape changes.

## ANTI-PATTERNS
- Do not import router modules into models.
- Do not embed service orchestration logic in model definitions.
- Do not create duplicate enum domains when existing model enums already represent the same concept.
- Do not bypass migration flow when altering model columns/constraints.

## NOTES
- `app/schemas/` currently exports a subset via `app/schemas/__init__.py`; some schema modules are imported directly.
- Model changes usually require coordinated updates in `app/schemas/`, services, and tests.
