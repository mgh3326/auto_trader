# SCHEMAS KNOWLEDGE BASE

## OVERVIEW
`app/schemas/` defines Pydantic transport schemas used by API/router boundaries; it is separate from ORM entity definitions in `app/models/`.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Package export surface | `app/schemas/__init__.py` | Curated schema exports used by import callsites |
| Manual holdings and portfolio DTOs | `app/schemas/manual_holdings.py` | Broker account/manual holding request/response contracts |
| News analysis DTOs | `app/schemas/news.py` | News scrape/analysis request-response contracts |
| Trading DTOs | `app/schemas/trading.py` | OHLCV/orderbook transport payload schemas |
| ORM-side related entities | `app/models/` | Persistence entities and enums consumed by schema fields |

## CONVENTIONS
- Keep schemas focused on transport validation/serialization concerns.
- Use `ConfigDict(from_attributes=True)` for ORM-backed response models where needed.
- Keep field validators deterministic and side-effect free.
- Keep schema examples and descriptions aligned with actual API behavior.
- Keep `__all__` exports intentional and synchronized with module ownership.

## ANTI-PATTERNS
- Do not move service/business orchestration logic into schema modules.
- Do not treat schemas as replacements for SQLAlchemy model definitions.
- Do not introduce breaking response field changes without router/test updates.
- Do not duplicate the same DTO shape across multiple modules without a clear boundary reason.

## NOTES
- Schema changes often require coordinated updates in routers, tests, and API documentation/examples.
