# SonarCloud Duplicated Lines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass SonarCloud's `new_duplicated_lines_density` quality gate (< 3%) by adding CPD exclusions for tests, SQL, HTML templates, and migrations — no application code changes.

**Architecture:** Single new file `sonar-project.properties` at repo root. SonarCloud Automatic Analysis honors this file at scan time. The properties file declares `sonar.cpd.exclusions` only — every other Sonar rule (coverage, smells, security) continues to apply to excluded paths.

**Tech Stack:** SonarCloud Automatic Analysis (no GitHub Actions changes), `sonar-project.properties` config, `git`, `gh` CLI for PR.

**Spec:** `docs/superpowers/specs/2026-05-07-sonarcloud-duplicated-lines-design.md`

---

## File Structure

- **Create:** `sonar-project.properties` — repo-root SonarCloud config; only `sonar.cpd.exclusions` plus required `sonar.projectKey` and `sonar.organization`.

That is the only file change. The remaining tasks are verification and a conditional fallback.

---

## Task 1: Add `sonar-project.properties`

**Files:**
- Create: `sonar-project.properties`

- [ ] **Step 1: Confirm the file does not already exist**

Run: `ls sonar-project.properties 2>/dev/null && echo EXISTS || echo MISSING`
Expected: `MISSING`

If it exists already, STOP and report — the spec assumes clean creation.

- [ ] **Step 2: Verify the only directories named `tests` are the intentional ones**

This guards the `**/tests/**` glob from accidentally excluding production code.

Run: `find . -type d -name tests -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/.git/*'`
Expected output (exact, in any order):

```
./tests
./blog/tests
```

If any other directory is listed, STOP and report — the glob would over-exclude.

- [ ] **Step 3: Verify the project key and organization match SonarCloud**

Run: `curl -sS "https://sonarcloud.io/api/components/show?component=mgh3326_auto_trader" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['component']['key'], d['organization'])"`
Expected: `mgh3326_auto_trader mgh3326`

If different, update Step 4 contents to match.

- [ ] **Step 4: Create `sonar-project.properties`**

Write the file at the repo root with exactly these contents:

```properties
sonar.projectKey=mgh3326_auto_trader
sonar.organization=mgh3326

# Duplication detection exclusions only.
# Other analysis (coverage, smells, security) still applies to these paths.
sonar.cpd.exclusions=**/tests/**,**/*.sql,app/templates/**,blog/images/**/*.html,alembic/versions/**
```

Notes for the engineer:
- File is at repo root, not under `docs/` or `.github/`.
- No trailing whitespace on any line.
- Final newline at end of file.
- The exclusion list is a single comma-separated value on one line.

- [ ] **Step 5: Sanity-check the file contents**

Run: `cat sonar-project.properties`
Expected: exact text above, no surprises.

Run: `wc -l sonar-project.properties`
Expected: 5 lines (3 properties + 2 comment lines, plus blank line between sections may make this 6 — both acceptable).

- [ ] **Step 6: Commit**

```bash
git add sonar-project.properties
git commit -m "$(cat <<'EOF'
chore(sonar): add CPD exclusions to clear duplicated-lines gate

Excludes tests, SQL migrations, Jinja2 templates, generated blog
HTML, and Alembic migration files from duplication detection only.
All other Sonar analysis (coverage, smells, security) still applies.

See docs/superpowers/specs/2026-05-07-sonarcloud-duplicated-lines-design.md
EOF
)"
```

Expected: clean commit, no hook failures.

---

## Task 2: Push and open the PR

**Files:** none (git/GitHub operations only)

- [ ] **Step 1: Confirm we are on the right branch**

Run: `git branch --show-current`
Expected: `sonarcloud-duplicated-lines`

If different, STOP — do not push.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin sonarcloud-duplicated-lines`
Expected: pushes successfully; remote tracking established.

- [ ] **Step 3: Open the PR against `main`**

Run:

```bash
gh pr create --base main --title "chore(sonar): add CPD exclusions to clear duplicated-lines gate" --body "$(cat <<'EOF'
## Summary
- Adds `sonar-project.properties` with `sonar.cpd.exclusions` for `**/tests/**`, `**/*.sql`, `app/templates/**`, `blog/images/**/*.html`, and `alembic/versions/**`.
- Only duplication detection is excluded for these paths; coverage, code smells, and security analysis still apply.
- No application code is modified.

## Why
Project-wide `new_duplicated_lines_density` is 3.96%, failing the new-code quality gate. 76% of duplication is in `tests/` (parametrized cases, mock scaffolding) and the rest is in SQL migrations and shared Jinja2 layout fragments — none of which should be parameterized away.

Spec: `docs/superpowers/specs/2026-05-07-sonarcloud-duplicated-lines-design.md`

## Test plan
- [ ] SonarCloud PR scan completes
- [ ] `new_duplicated_lines_density` < 3% on the PR scan
- [ ] Quality gate is green
- [ ] Coverage / smells / security ratings unchanged on excluded files (regression check)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Save it for Task 3.

---

## Task 3: Verify SonarCloud results on the PR

**Files:** none (verification only)

- [ ] **Step 1: Wait for SonarCloud's PR scan to finish**

SonarCloud Automatic Analysis is event-driven and typically completes within 5–15 minutes of the push. Check status:

```bash
gh pr checks
```

Expected: a SonarCloud check appears (named like `SonarCloud Code Analysis` or similar). Wait until it transitions out of `pending`.

- [ ] **Step 2: Pull the PR-scan duplication metrics from the API**

Run (replace `<PR_NUMBER>` with the number from Task 2 Step 3):

```bash
curl -sS "https://sonarcloud.io/api/measures/component?component=mgh3326_auto_trader&metricKeys=new_duplicated_lines_density,new_duplicated_lines,duplicated_lines_density,duplicated_lines&pullRequest=<PR_NUMBER>" | python3 -m json.tool
```

Expected: `new_duplicated_lines_density` value < `3.0`. Record the actual value.

If the value is < 3.0 → quality gate should be green. Proceed to Step 3.
If the value is ≥ 3.0 → STOP this task. Open Task 4 (fallback).

- [ ] **Step 3: Confirm the quality gate is green via the API**

Run:

```bash
curl -sS "https://sonarcloud.io/api/qualitygates/project_status?projectKey=mgh3326_auto_trader&pullRequest=<PR_NUMBER>" | python3 -m json.tool
```

Expected: `"status": "OK"` in the response.

If `"ERROR"` and the failing condition is `new_duplicated_lines_density`, proceed to Task 4. If the failing condition is something *other* than duplication (coverage, smells, etc.), STOP and report — that's outside this plan's scope.

- [ ] **Step 4: Spot-check that excluded files lost only their CPD signal, not other analysis**

Pick one excluded file as a smoke test:

```bash
curl -sS "https://sonarcloud.io/api/measures/component?component=mgh3326_auto_trader:tests/_mcp_screen_stocks_support.py&metricKeys=duplicated_lines,code_smells,coverage&pullRequest=<PR_NUMBER>" | python3 -m json.tool
```

Expected:
- `duplicated_lines` = `0` (or absent) — exclusion working.
- `code_smells` and `coverage` still present (values unchanged from main).

If `code_smells` is suddenly 0 and was non-zero on main, the exclusion is too broad — STOP and revisit.

- [ ] **Step 5: Mark plan complete**

If Steps 2–4 all passed, this plan is done. Report the new density value, the green gate, and the PR URL to the user. Do **not** proceed to Task 4.

---

## Task 4 (CONDITIONAL FALLBACK): Targeted refactor for `formatters_discord.py`

> **Run only if Task 3 Step 2 reported `new_duplicated_lines_density >= 3.0`.** Otherwise skip this task and Task 5.

**Files:**
- Modify: `app/monitoring/trade_notifier/formatters_discord.py`
- Possibly create: `app/monitoring/trade_notifier/_formatters_shared.py` (only if there are *identical* clones to extract)
- Test: existing tests under `tests/` that cover `formatters_discord.py`

**Constraint (saved feedback rule):** Only extract identical or constant-diff code. Never parameterize logic branches. If the duplicates contain `if/else` differences or non-trivial parameter variation, STOP and report — do not refactor.

- [ ] **Step 1: Identify the exact duplicate ranges via SonarCloud's duplications API**

Run:

```bash
curl -sS "https://sonarcloud.io/api/duplications/show?key=mgh3326_auto_trader:app/monitoring/trade_notifier/formatters_discord.py" | python3 -m json.tool
```

Expected: a `duplications` array. Each entry has `blocks[]` listing files and line ranges that are clones of each other.

Record each pair of `(file, from, size)` ranges.

- [ ] **Step 2: Read both sides of every clone pair**

For each clone pair from Step 1, use `Read` (or `sed -n 'FROM,TOp'`) to view the exact lines on both sides. Look for:
- Identical text → safe to extract.
- One-line constant difference (e.g., a string literal or numeric constant) → safe to extract with that constant as a parameter.
- `if/else`, branching control flow, different function calls, different field accesses → **NOT SAFE**. Skip this clone.

If *every* clone is unsafe, STOP and skip to Task 5.

- [ ] **Step 3: Find existing tests for the file**

Run: `rg -l "from app.monitoring.trade_notifier.formatters_discord|import formatters_discord" tests/`
Expected: a list of test files. Read them to understand which behaviors are already covered.

If there are no tests for the affected functions, STOP and report — extracting code without tests violates TDD discipline. The user will need to decide whether to add tests first or accept the gate failure.

- [ ] **Step 4: Run the existing tests to establish a green baseline**

Run: `uv run pytest tests/<the-relevant-files> -v`
Expected: all PASS. Record the count.

- [ ] **Step 5: Extract one clone group at a time**

For each safe clone pair (smallest first):
1. Create or reuse a private helper in `_formatters_shared.py` (if more than one consumer needs it) or a module-private function in `formatters_discord.py` (if only that file uses it).
2. Replace both clone sites with calls to the helper.
3. Re-run the tests from Step 4. They must still PASS.
4. If they fail, revert the change for that clone and move on — do not chase the failure.

- [ ] **Step 6: Run the full unit test suite**

Run: `make test-unit`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/monitoring/trade_notifier/
git commit -m "$(cat <<'EOF'
refactor(trade_notifier): extract identical clones in formatters_discord

Extracts only literally identical (or constant-diff) blocks flagged
by SonarCloud duplication analysis. No logic branches were
parameterized.

EOF
)"
```

- [ ] **Step 8: Push and re-check the PR scan**

Run: `git push`
Wait for the new SonarCloud scan to complete (`gh pr checks`).

Re-run the API check from Task 3 Step 2. If `new_duplicated_lines_density` is now < 3.0 → done, skip Task 5. Otherwise → proceed to Task 5.

---

## Task 5 (CONDITIONAL FALLBACK): Targeted refactor for `trade_profile_tools.py`

> **Run only if Task 4 finished and the gate is still red.** Otherwise skip.

**Files:**
- Modify: `app/mcp_server/tooling/trade_profile_tools.py`
- Test: existing tests covering this module

Same constraint as Task 4: identical/constant-diff only.

- [ ] **Step 1: Pull duplicate ranges**

Run:

```bash
curl -sS "https://sonarcloud.io/api/duplications/show?key=mgh3326_auto_trader:app/mcp_server/tooling/trade_profile_tools.py" | python3 -m json.tool
```

Record the clone block pairs.

- [ ] **Step 2: Read both sides; classify safe vs. unsafe**

Same procedure as Task 4 Step 2.

If no clones are safe → STOP and report. The user decides whether to relax the quality-gate threshold or accept the failure.

- [ ] **Step 3: Find existing tests**

Run: `rg -l "trade_profile_tools" tests/`
Expected: a list of files. Open them and confirm coverage of the duplicated regions.

If no tests cover the duplicated code → STOP and report (same reasoning as Task 4 Step 3).

- [ ] **Step 4: Establish green baseline**

Run: `uv run pytest tests/<relevant-files> -v`
Expected: all PASS.

- [ ] **Step 5: Extract safe clones**

Same procedure as Task 4 Step 5.

- [ ] **Step 6: Full unit test suite**

Run: `make test-unit`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/
git commit -m "$(cat <<'EOF'
refactor(mcp): extract identical clones in trade_profile_tools

Same constraint as the formatters_discord refactor — only
literally identical blocks were extracted.

EOF
)"
```

- [ ] **Step 8: Push and re-check**

Run: `git push`
Wait for SonarCloud, then re-run Task 3 Step 2. The gate must now be green.

If it is still red, STOP and report — the remaining duplication likely sits in `app/services/` blocks that are intentional near-clones. The user must decide between (a) accepting the failure, (b) widening exclusions further, or (c) writing tests to enable a deeper refactor. Do not unilaterally do any of these.

---

## Self-review summary

- **Spec coverage:**
  - Single artifact `sonar-project.properties` → Task 1 ✓
  - Glob list matches spec exactly → Task 1 Step 4 ✓
  - PR-based verification → Tasks 2–3 ✓
  - Fallback to refactor `formatters_discord.py` and `trade_profile_tools.py` → Tasks 4–5 ✓
  - Out-of-scope items (no template refactor, no test refactor, no CI changes) — none of them appear as tasks ✓
- **Placeholder scan:** no TBD/TODO; every code block is concrete; the conditional fallback tasks have full procedures, not "do similar steps as Task N".
- **Type/name consistency:** project key `mgh3326_auto_trader`, org `mgh3326`, branch `sonarcloud-duplicated-lines`, file `sonar-project.properties` — used identically across all tasks.
