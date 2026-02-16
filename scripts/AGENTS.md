# SCRIPTS KNOWLEDGE BASE

## OVERVIEW
`scripts/` contains operational entrypoints for deployment, migration checks, health validation, environment bootstrap, and support utilities.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Production deployment orchestration | `scripts/deploy.sh` | Migration mode selection, deploy flow, optional health/backup |
| Migration risk precheck | `scripts/migration-check.sh` | DB connectivity and high-risk migration pattern checks |
| Migration execution | `scripts/migrate.sh` | Host-side Alembic execution with pre/post verification |
| Runtime health checks | `scripts/healthcheck.sh` | Container/service/resource/log checks |
| HTTPS/Caddy checks | `scripts/test-caddy-https.sh` | TLS redirect/header/certificate checks |
| Test env bootstrap | `scripts/setup-test-env.sh` | `.env.test` generation for CI/local tests |
| MCP startup wrapper | `scripts/mcp_server.sh` | MCP process boot wrapper with `.env.mcp` |
| Data correction helpers | `scripts/fix_overseas_exchange_codes.py`, `scripts/migrate_symbols_to_dot_format.sql` | Targeted maintenance scripts |
| Production compose context | `docker-compose.prod.yml`, `docker-compose.migration.yml` | Runtime/migration mode coupling |

## CONVENTIONS
- Treat scripts as operator-facing workflows with explicit preconditions.
- Keep `.env.prod` / `DATABASE_URL` / repo path assumptions explicit in script usage.
- Preserve safety prompts and prechecks for migration and deployment actions.
- Keep migration orchestration aligned with Alembic config and compose profiles.
- Keep script behavior and runbook docs synchronized.

## ANTI-PATTERNS
- Do not hardcode credentials/secrets in scripts.
- Do not remove migration risk checks or confirmation steps without replacing safeguards.
- Do not assume non-interactive execution for scripts that currently require operator confirmation.
- Do not diverge script behavior from documented deploy/migration flows.

## NOTES
- Several scripts rely on host-level tools (`docker`, `psql`, system services) and fixed operational paths.
- Production compose and script behavior uses host networking assumptions.
