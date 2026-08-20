# ROB-1301 — Buy-gate A/B shadow implementation report

## 1. Handoff assessment

Start commit (base checked at takeover):
`1fb649be44236e553386f80fe4f5eb2cad078fab`.

`origin/main` had advanced through #1902, #1903, and #1906 at takeover. Their
changed paths do not overlap this implementation. This worktree contained the
predecessor's uncommitted handoff, so it was first preserved in a feature
commit, then rebased without conflicts on `origin/main` at
`5435f2cf7e`. Post-rebase ancestry was verified with
`git merge-base --is-ancestor origin/main HEAD`.

| Predecessor change | Handoff decision |
| --- | --- |
| `app/services/buy_gate_ab_shadow/` | Kept: pure, broker/DB/proposal/watch-free evaluator, tagging, and scoring structure. Corrected persistence provenance and pre-close scoring behavior. |
| `app/mcp_server/tooling/buy_gate_ab_shadow*.py` + analysis/route registries | Kept: observation-only registration with no write call. |
| Playbook, runbook, README, `CLAUDE.md`, `AGENTS.md` | Kept: issue-canonical forbidden three and the no-lane/no-scheduler boundary. Runbook amended for persisted provenance and no intermediate scores. |
| New unit/static tests | Kept and extended: strict boolean gate inputs, shared snapshot digest, persisted pre-registration identity, and pre-collection score refusal. |

The predecessor's incorrect assumption was that an in-code pinned hash alone
prevents later reinterpretation. A later code edit could update both the
payload and its constant, while historical forecast rows would have no frozen
reference. Each emitted `forecast_save` payload now carries the pinned spec
hash, timezone-aware `evaluation_as_of`, and the normalized shared A/B input
snapshot plus SHA-256 digest. The score function also no longer emits returns
or drawdowns during the 28-calendar-day collection period.

The assumed caps are retained: the current policy defines KRW 200,000–400,000
and USD 150–450 per-symbol ranges, so the pre-registered 0.5 multiplier yields
KRW 200,000 and USD 225 without touching execution sizing.

## 2. Scope and safety outcome

Variant A remains outside this package and unchanged. Variant B produces only
shadow forecast-save arguments. No proposal, order, watch, broker call, DB
write, scheduler registration, or policy edit is introduced by the evaluator.
The issue's three forbidden acts are copied verbatim into the frozen spec,
playbook, and tests.

## 3. Verification

Rebase-base verification terminal output:

```text
$ uv run pytest -q tests/services/buy_gate_ab_shadow \
    tests/mcp_server/tooling/test_buy_gate_ab_shadow.py \
    tests/test_playbook_tool_names.py tests/test_route_request_registry_diff.py \
    tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py
.....................................................                    [100%]
53 passed in 13.65s

$ make lint
uv run ruff check app/ tests/ research/ scripts/
All checks passed!
uv run ruff format --check app/ tests/ research/ scripts/
3965 files already formatted
uv run ty check app/ --error-on-warning
All checks passed!
```

`make test-unit` was also started three times during handoff. This execution
environment exposes only a 30-second foreground-output window and did not
return a completion status for those runs; it is therefore **not counted as
passed** here. The required, directly relevant suite above completed with exit
status 0 and is the verification claim for this handoff.
