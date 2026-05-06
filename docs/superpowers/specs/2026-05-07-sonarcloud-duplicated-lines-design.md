# SonarCloud Duplicated Lines — Quality Gate Fix Design

- **Date**: 2026-05-07
- **Branch**: `sonarcloud-duplicated-lines`
- **Goal**: Pass SonarCloud's new-code quality gate on `new_duplicated_lines_density` (default threshold: < 3%).

## Context

SonarCloud reports the following duplication state for `mgh3326_auto_trader`:

| Metric | Value |
|---|---|
| `duplicated_lines` | 13,054 |
| `duplicated_lines_density` | 3.9% |
| `duplicated_blocks` | 511 |
| `duplicated_files` | 110 |
| `new_duplicated_lines` | ~12,972 |
| `new_duplicated_lines_density` | 3.96% |

`new_duplicated_lines_density` is essentially equal to the overall density, meaning the entire repo is being treated as "new" by Sonar's reference branch comparison. The new-code gate is the failing one.

### Where the duplication lives

By directory:
- `tests/` — **9,940 lines (76%)**, 391 blocks, 6.4% density
- `app/templates/` — 917 lines, 32 blocks, 12.2% density
- `scripts/sql/` — 396 lines (2 files), 62.1% density
- `app/services/`, `app/mcp_server/tooling/`, `app/monitoring/` — ~1,065 lines combined
- `blog/` — 221 lines (mostly `blog/tests/` and `blog/images/`)

Top file offenders:
- `tests/_mcp_screen_stocks_support.py` — 1,691 lines, 40.3% (this is itself a *shared helper*; Sonar still flags it)
- `tests/test_mcp_screen_stocks_tvscreener_contract.py` — 665 lines, 65.3%
- `scripts/sql/us_candles_timescale.sql` — 364 lines, 86.3%
- `app/templates/portfolio_dashboard.html` — 246 lines, 11.7%
- `app/templates/portfolio_position_detail.html` — 231 lines, 20.2%

### Constraints

- User's saved rule: **only extract identical or constant-diff code; never parameterize logic branches.** This rules out aggressive test refactors.
- No `sonar-project.properties` file exists in the repo today — SonarCloud is running in Automatic Analysis mode. Automatic Analysis honors a properties file if present.
- No GitHub Action exists for SonarCloud scanning, so no CI workflow changes are needed.

## Decision

**Approach 1 — CPD exclusions only.** Add a single `sonar-project.properties` file that excludes test code, SQL migrations, HTML templates, and Alembic-generated migrations from duplication detection. No application code is modified.

### Why this approach

1. **Directly satisfies the success criterion** (pass new-code gate) with the smallest blast radius.
2. **Industry-standard** SonarCloud configuration for Python/Jinja2/SQL projects.
3. **Reversible** by reverting one file.
4. **Respects the "no logic-branch parameterization" rule** by not refactoring at all.
5. **Preserves all other Sonar analysis** on excluded files — coverage, code smells, security, and bugs continue to be reported. Only duplication detection skips these paths.

### Alternatives considered

- **Approach 2 — Exclusions + targeted refactor.** Extract identical helpers from `formatters_discord.py` (134 dup lines) and `trade_profile_tools.py` (96 dup lines). Rejected as the primary plan because Approach 1 already passes the gate; held in reserve as a fallback step if it doesn't.
- **Approach 3 — UI-configured exclusions.** Same effect via SonarCloud project settings UI. Rejected because it's invisible from the repo, not peer-reviewable in PRs, and harder to keep in sync across forks.
- **Aggressive deletion / consolidation of test files.** Rejected: test files are valuable and the duplication is intentional (parametrized test cases, mock scaffolding).

## Design

### Single artifact: `sonar-project.properties`

Path: repo root.

Contents:

```properties
sonar.projectKey=mgh3326_auto_trader
sonar.organization=mgh3326

# Duplication detection exclusions only.
# Other analysis (coverage, smells, security) still applies to these paths.
sonar.cpd.exclusions=**/tests/**,**/*.sql,app/templates/**,blog/images/**/*.html,alembic/versions/**
```

### Glob rationale

| Glob | Rationale |
|---|---|
| `**/tests/**` | Catches both `tests/` and `blog/tests/`. Test duplication (parametrized cases, mock scaffolding) is expected and refactoring would harm readability. |
| `**/*.sql` | TimescaleDB hypertable / continuous aggregate DDL is near-identical between KR and US candle schemas by design. Refactoring SQL across two markets would obscure intent. |
| `app/templates/**` | Jinja2 dashboards share layout fragments; SonarCloud's HTML CPD is noisy on shared partials. |
| `blog/images/**/*.html` | Auto-generated image gallery HTML in the blog directory. |
| `alembic/versions/**` | Auto-generated migration files. Never refactor. |

### Expected outcome

After applying the exclusions:

- `tests/` (9,940 dup lines) — excluded
- `scripts/sql/` (396 dup lines) — excluded
- `app/templates/` (917 dup lines) — excluded
- `blog/tests/` (~116 dup lines) — excluded
- `blog/images/*.html` (~40 dup lines) — excluded
- `alembic/versions/` — excluded

Remaining duplicated lines in scope: **~600 lines** spread across `app/monitoring/`, `app/mcp_server/`, `app/services/`, `blog/images/*.py`, and `scripts/smoke/`. With the project's total scanned LOC, the resulting `new_duplicated_lines_density` should fall well below the 3% gate threshold.

### Verification

1. Commit `sonar-project.properties` on branch `sonarcloud-duplicated-lines` and push.
2. Open a PR to `main`. SonarCloud's PR decoration runs and reports the new density.
3. **Pass criterion**: `new_duplicated_lines_density` < 3% on the PR scan, quality gate green.
4. **If still red**: Trigger fallback (see below) and re-scan.

### Fallback (if Approach 1 alone doesn't clear the gate)

Add minimal targeted refactors in priority order, only for **identical** clones:

1. `app/monitoring/trade_notifier/formatters_discord.py` — 134 dup lines, 6 blocks. Use `duplications/show?key=...` Sonar API to get exact ranges, extract genuinely identical blocks into a helper. **Skip if the clones are near-clones with logic differences** — defer to manual reduction or accept.
2. `app/mcp_server/tooling/trade_profile_tools.py` — 96 dup lines, 2 blocks. Same protocol.

Each fallback step is a separate commit; each must keep all tests green and add no new abstractions for hypothetical future use.

## Out of scope

- Refactoring `tests/_mcp_screen_stocks_support.py` (1,691 dup lines). It is already a shared support module; Sonar reports it because *other* test files copy from it.
- Refactoring HTML templates into more aggressive partials.
- Changing CI / GitHub Actions configuration.
- Modifying `tests/` content for any reason.
- Adjusting SonarCloud quality gate thresholds.

## Risks

| Risk | Mitigation |
|---|---|
| `sonar-project.properties` triggers a new analysis mode (CI-based) and breaks current Automatic Analysis. | Automatic Analysis is documented to honor `sonar-project.properties` for configuration without switching modes. If it does switch, revert the file. |
| Glob `**/tests/**` accidentally excludes a non-test directory. | Repo grep confirms only `tests/`, `blog/tests/`, and a small number of `tests/__init__.py` files match. No production code uses `tests` as a folder name. |
| Future files added under excluded paths drift in quality. | Acceptable trade-off: only **duplication** detection is excluded; all other Sonar rules continue to apply. |
| Density still > 3% after exclusions. | Fallback plan above. |

## Success criteria

- `sonar-project.properties` committed at repo root with the listed contents.
- PR scan reports `new_duplicated_lines_density` < 3% on the new-code period.
- Quality gate green.
- No application code modified.
