# AUTH KNOWLEDGE BASE

## OVERVIEW
`app/auth/` owns authentication and authorization flows across API tokens, web sessions, and admin role management.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Auth wiring in app startup | `app/main.py` | Includes auth routers and mounts global `AuthMiddleware` |
| Global request auth gate | `app/middleware/auth.py` | Path allowlist, session checks, `request.state.user` population |
| API token endpoints | `app/auth/router.py` | `/auth` register/login/refresh/logout/me |
| Web/session endpoints | `app/auth/web_router.py` | `/web-auth` session cookie flow, login templates, role/session helpers |
| Admin endpoints | `app/auth/admin_router.py` | `/admin` role and user activation management |
| Route dependencies | `app/auth/dependencies.py` | JWT-based dependency helpers (`get_current_user*`) |
| Token/password primitives | `app/auth/security.py`, `app/auth/token_repository.py` | JWT generation, password hash/verify, refresh token persistence |
| Auth schemas and roles | `app/auth/schemas.py`, `app/auth/role_hierarchy.py`, `app/auth/constants.py` | Request/response models and role comparisons |

## CONVENTIONS
- Keep auth business rules inside `app/auth/*`; routers outside this package should consume dependencies/helpers.
- Preserve middleware + dependency split: middleware gates access, dependencies enforce route-level identity semantics.
- Route modules should stay scoped (`router.py` for API auth, `web_router.py` for session UI, `admin_router.py` for admin actions).
- Reuse role hierarchy helpers for permission checks instead of ad hoc role comparisons.
- Keep refresh-token lifecycle changes aligned with token repository logic and logout/revoke flows.

## ANTI-PATTERNS
- Do not bypass `AuthMiddleware`/session helpers by adding unreviewed public paths.
- Do not duplicate password/token logic outside `app/auth/security.py` and `app/auth/token_repository.py`.
- Do not hardcode JWT secrets or credential defaults in route modules.
- Do not mix unrelated service/business logic into auth endpoint handlers.

## NOTES
- `manage_users.py` in repo root overlaps with some admin actions; web/admin API remains the primary application interface.
- If auth behavior changes, verify both API and web-session paths plus middleware allowlist behavior.
