# AT retirement inventory — 2026-11-01 (ROB-1429)

Operator 2026-11-01 restarted the auto_trader strategy track and asked to
retire stalled work. This inventory gives a keep or retire recommendation for
each stalled item named in the U1 brief. It is a recommendation only: this PR
changes no hk task, Linear issue or GitHub PR state. Desk or the operator
applies the transitions.

Evidence was read on 2026-11-01 from `gh pr view`, `handoffkeep tasks show`,
the at-integrator triage (`hk:doc report/2026-10-16/at-integrator-triage`)
and `origin/main` at `3ab5b4762`.

Rule used: **keep** an item when it is small, still true on `origin/main`,
and protects a live path or retires dead code. **Retire** it when its date
has passed, its premise is gone, it waits on an approval nobody is pursuing,
or the restart plan replaces it.

## Summary

| Item | Kind | Recommendation |
|---|---|---|
| PR #2234 (ROB-1397) | closed draft | Retire (stays closed) |
| PR #2233 (ROB-1396) | closed draft | Retire (stays closed) |
| PR #2225 (ROB-1404 2A) | closed draft | Retire as code; keep as reference |
| hk 829 | H6 core_qty | Retire (operator acknowledgement needed) |
| hk 869 | #765 tester SHOULDs | Keep |
| hk 873 | C3 held_majors_support_net retirement | Keep |
| hk 874 | #817 tester SHOULDs | Keep |
| hk 885 | screener snapshot freshness | Retire |
| hk 909 | after-market earnings playbook | Retire |
| hk 914 | #896 shadow scoring holes | Retire, unless #896 scoring is resumed |
| hk 915 | altseason route block | Retire as resolved |

## Closed drafts

All three were closed on 2026-11-01 (07:03 UTC) while still drafts.

### PR #2234 — ROB-1397 RSI warm-up across MCP surfaces — retire

- Content: README clarification that RSI-14 needs 15 closes of calculation
  history, plus a `get_ohlcv(count=14)` regression test.
- Why retire: ROB-1382 (single RSI source and formula) is already on `main`
  and documented in the market-data contract. The remaining change is
  documentation and tests for an edge of the MCP surface, not strategy work.
- If needed again: the restart computes features in the research environment
  under the split rules (`docs/research/experiment-split-leakage-rules.md`),
  not from the MCP surface. Re-file only if an RSI warm-up mismatch is seen
  on a live surface. Desk should close or re-scope ROB-1397 in Linear.

### PR #2233 — ROB-1396 canonical daily candle lookup tests — retire

- Content: tests that exercise the real `get_ohlcv` and quick-batch lookup
  paths instead of a replaced `_canonical_daily_frame`.
- Why retire: test-only hardening with no open defect. The restart builds its
  own point-in-time datasets (U2/U3/U4).
- If needed again: re-file when a canonical-candle lookup regression appears.

### PR #2225 — ROB-1404 Part 2A KR entry-method comparison — retire as code, keep as reference

- Content: research package `research/rob1404_kr_entry/` (not on `main`).
  Five verification rounds ended BLOCKER: `explore_2b` counted missing hourly
  bars as "no rebound" (145020).
- Why retire: the operator decided not to merge it (Q-1019-14 = A). Its
  question moved to ROB-1411 E3, which carries the window-completeness rule
  that 2B lacked (PREREG §6).
- Keep: the closed branch stays on GitHub as an exploration input for E3 and
  for the U5 KR adapter. Do not cite its 2A tables as evidence; they were
  never verified at head.

## hk backlog

All eight were relaned to at-integrator on 2026-10-16 and have had no event
since.

### hk 829 — H6 logical core tag (core_qty) — retire (operator acknowledgement needed)

- State: backlog since 09-28, T3, blocked on #841 and the U12m source
  decision (triage §3).
- Why retire: the operator approved the start on 09-27, but nothing moved for
  five weeks, both prerequisites are still open, and the restart plan has no
  H6 unit. Because the start was operator-approved, retirement needs the
  operator's acknowledgement.
- If needed again: re-file as a live-account protection task with the astra
  design notes (comment 543) when H6 resumes.

### hk 869 — pin #765 parking-account guards in committed tests — keep

- State: T2, tester SHOULD from 09-28.
- Why keep: five guards (M5b, M6, M11, M12, M14) were only killed by
  tester-held tests. This protects the live Toss parking account path and is
  independent of the strategy track. Small.
- Before starting: check that the guarded code is unchanged on `main`.

### hk 873 — retire C3 held_majors_support_net — keep

- State: operator Q-30 (09-28) approved the retirement; not done.
- Evidence: C3 is still on `main` in `config/trading_policy.yaml`,
  `app/schemas/trading_policy.py`,
  `app/services/invest_view_model/buy_plan/service.py` and
  `app/schemas/invest_buy_plan.py`.
- Why keep: it removes a dead strategy (zero fills, retention condition not
  met) with an operator decision already in place. Small operator policy PR.

### hk 874 — #817 tester SHOULDs — keep

- Evidence: `docs/contracts/policy-freeze-deviation-20260928-parking-sell-no-approval.md`
  still has `merge_sha: TBD_ON_MERGE`. The Toss pre-send hook still records a
  proven no-send block as `unverified`, and seven boundary mutants are caught
  only by tester-held tests.
- Why keep: live sell-path correctness and record accuracy. The 2026 CSAT
  (수능) special KRX hours item is due this month.

### hk 885 — screener snapshot freshness — retire

- Evidence: PR #2128 covers part ① (silent investor-flow failure) only for
  a commit that writes zero rows on a KRX trading day (or on a date the
  calendar cannot classify). A partial loss still succeeds: per-symbol fetch
  failures become warnings, and the screener reader serves the newest
  partition whose row count meets the 50% floor of the active universe
  without marking it degraded. Partial losses above that floor stay silent;
  that gap is retired with this task and is not claimed as covered. Part ②
  (support_proximity builder cadence) needs a new schedule, which needs
  approval (hard rule 6), and nobody is pursuing it.
- Why retire: candidate supply is now measured through the ROB-1411 record
  points instead of tuning screener freshness.
- If needed again: re-file part ② as an operator decision request for a
  schedule.

### hk 909 — after-market earnings reaction playbook v0 — retire

- Evidence: the first run was set for 2026-10-01 05:30 KST and was blocked
  by T-a. The date has passed, and the triage noted it must be rescheduled
  after the 908 result.
- Why retire: no owner, past date, not in the restart plan.

### hk 914 — #896 shadow scoring holes (auto_trader-operator) — retire unless #896 scoring resumes

- Content: two holes that inflate the agreement rate (malformed proposals
  dropped into "agree", broken runner rows counted as "no action both") and a
  policy-owner question.
- Why retire by default: the fixes only matter if the #896 two-week shadow is
  scored. The restart replaces ad hoc shadow scoring with registered
  hypotheses (`research/experiments/registry.json`).
- Keep instead if the operator still plans to score #896: then these fixes
  must land before scoring.

### hk 915 — get_upbit_altseason blocked on the crypto route — retire as resolved

- Evidence: Q-58 / task #911 re-admitted `get_upbit_altseason` on the live
  crypto route (`app/mcp_server/tooling/route_request_lanes.py` around lines
  534–545; `app/mcp_server/tooling/live_profile_registration.py` around lines
  125–134).
- Why retire: the blocker that made the C1 recovery gate a permanent hold is
  gone. Re-measure C1 only if the new crypto strategy keeps C1.
