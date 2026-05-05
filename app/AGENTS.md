# APP KNOWLEDGE BASE

## OVERVIEW
`app/` is the runtime package for API serving, task execution, MCP exposure, monitoring, and domain business logic.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| API bootstrap and process lifecycle | `app/main.py` | FastAPI creation, lifespan, router registration, middleware |
| Shared runtime infrastructure | `app/core/` | Config, DB/session wiring, TaskIQ broker/scheduler, utility helpers |
| Auth and request gating | `app/auth/`, `app/middleware/` | API/web/admin auth flows and middleware access control |
| HTTP route surfaces | `app/routers/` | Endpoint modules; keep handlers thin over services/jobs |
| Business and provider integrations | `app/services/` | KIS/Upbit/Naver/Yahoo clients and orchestration services |
| Jobs and scheduled task execution | `app/jobs/`, `app/tasks/` | Orchestration units and periodic task declarations |
| MCP process and tool contracts | `app/mcp_server/` | MCP bootstrap; detailed tool rules in `app/mcp_server/tooling/` |
| Observability and notifications | `app/monitoring/` | Sentry initialization and notifier integrations |
| Persistence and DTO boundaries | `app/models/`, `app/schemas/` | SQLAlchemy entity layer vs Pydantic API schemas |

## CONVENTIONS
- Keep process bootstrap centralized in `app/main.py` and `app/mcp_server/main.py`.
- Keep transport layers thin: routers/auth should delegate business logic to services/jobs.
- Keep periodic task declarations in `app/tasks/`; job orchestration belongs in `app/jobs/`.
- Reuse shared utilities in `app/core/` for symbols, config, rate limiting, and time handling.
- Preserve model/schema separation: DB entities in `app/models/`, API contracts in `app/schemas/`.

## DOMAIN SPECIFICS

### Committee Decision Workflow (ROB-107)
- **Workflow Control:** Committee sessions use `workflow_status` for state transitions and `account_mode` for execution targeting.
- **Service Layer:** Use `CommitteeSessionService` in `app/services/trading_decisions/committee_service.py` for state management.
- **Persistence:**
  - `automation` (JSONB): Control flags for auto-approval and execution.
  - `artifacts` (JSONB): Structured store for `evidence`, `risk_review`, `portfolio_approval`, and `execution_preview`.
- **Frontend:** Mirror types in `frontend/trading-decision/src/api/types.ts`.

## ANTI-PATTERNS
- Do not duplicate app bootstrap wiring across random modules.
- Do not call external providers directly from routers when service abstractions already exist.
- Do not place `@broker.task(...)` declarations inside `app/jobs/`.
- Do not bypass auth/middleware boundaries by introducing ad hoc public paths.
- Do not hardcode secrets or account credentials in runtime modules.

## NOTES
- This file is the parent guide for all `app/*` domains.
- Child AGENTS files in `app/*` are more specific and override this file for their subtree.
- `app/mcp_server/tooling/` and `app/services/` are high-change-surface areas; keep edits narrow and contract-aware.
