# Freeze deviation record — §664 KR new-entry one-share exception

Schema: GPT Pro checkpoint #4 verdict, appendix A. Canonical file
`~/work/herdr-inbox/gptpro-checkpoint4-verdict-20260830.md` lives on the
director's Mac and could not be read from the desktop builder; the field list
below is taken verbatim from hk memory `fable-probe/project-freeze-deviation-record-protocol`
(which quotes appendix A) and the in-repo precedent
`docs/contracts/policy-freeze-deviation-20260907-underwater-support-net.md` (PR #2060).
Written **before merge**, as required for a relaxation/exception PR.

```yaml
freeze_epoch_id: UNKNOWN
  # Appendix A defines no value for this field. The §177 precedent wrote
  # q4-tier-freeze-2026-08-30; it is not asserted here, and the ROB-1331 Q6
  # epoch is a different axis and is deliberately not substituted.
change_id: s664-kr-one-share-exception-20260924
pr_number: 2097
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  Operator decision 2026-09-24 23:2x recorded in hk:doc
  decision/2026-09-24/kiwoom-mock-h1-krb1-stop-664 §2 ("#664 정책 PR을 지금
  진행할지 // 이거 둘다 진행하자"); ceiling 10,000,000 decided 2026-09-24 in
  hk:doc strategy-lab/2026-09-24/policy-proposal-kr-one-share-exception §2/§4
  (3,000,000 draft withdrawn). Operator wording: "너무 타이트한 정책. 하이닉스
  같은 걸 사야 할 때 못 사면 문제." / "매수 주문이 발생하지 않길 기대한다면
  계좌에 입금하지 않는다." hk task #664.
market: [kr]
account_scope: >-
  Every KR buy account mode whose sizing is judged against
  buy.per_symbol_notional_krw_range — kis_live, toss_live AND the mock
  accounts kis_mock / kiwoom_mock. The exception is market-scoped, not
  account-scoped (see mock_experiment_effect). US and crypto are untouched;
  the pre-existing US exception is still NOT honoured by decision_table_validate.
change_class:
  - new_exception    # buy.per_symbol_notional_krw_range.one_share_exception
affected_policy_keys:
  - thresholds.buy.per_symbol_notional_krw_range.one_share_exception   # NEW {enabled: true, absolute_ceiling_krw: 10000000, max_deep_rungs: 1}
  - thresholds.buy.per_symbol_notional_krw_range.semantics             # sentence appended; original text kept as prefix
  - version                                                            # 2026-09-08.1 -> 2026-09-24.1
  - source                                                             # append-only provenance
  # Band VALUE [200000, 400000] unchanged. order_proposals.auto_approve
  # per_order_cap.kr 2,000,000 and daily_cap.kr 5,000,000 unchanged. Machine
  # proof: tests/schemas/test_trading_policy_schema.py
  #   ::test_rob_1289_preserves_all_preexisting_policy_keys_and_values
  #   ::test_s147_invariants_match_the_rob1289_baseline_exactly
  #   ::test_s664_changes_no_cap_gate_or_concentration_value
  # which pin the exact §664 block + wording via _strip_s664_kr_one_share_exception
  # and then assert closed equivalence over every other key.
consumer_paths:
  - app/schemas/trading_policy.py                                  # OneShareExceptionPolicy: KRW ceiling, exactly one ceiling, max_deep_rungs >= 1; PolicyThreshold pins ceiling currency to band unit and ceiling > band high
  - app/services/trading_policy_service.py                         # get_policy_for projection uses model_dump(exclude_none=True): US projection keyset byte-identical
  - app/services/decision_table_validate/one_share_exception.py    # NEW pure predicate (reads the closed PARKING_ALLOWLIST_SCOPES constant)
  - app/services/decision_table_validate/validator.py              # _validate_buy_policy honours the exception; _validate_one_share_rung_limit (table-wide max_deep_rungs)
  - app/services/decision_table_apply/service.py                   # unchanged code; re-invokes decision_table_validate, so it inherits the admission
  - get_trading_policy MCP tool / operator-repo session prompts     # read the new block; operator-repo prompt text is out of scope of this PR
  # NOT consumers (unchanged, verified by grep): order_proposals/auto_approve.py,
  # order_validation, buy_candidate_fanout, app/services/buy_gate_ab_shadow/**.
effective_at: >-
  on merge (policy version 2026-09-24.1). Merge only after operator
  confirmation AND the auto_trader merge freeze lifting.
live_only_claim: false
mock_projection_hash_before: "6d5a858ff5cb"   # policy content_hash at 2026-09-08.1 (origin/main 3931fdb3e)
mock_projection_hash_after: "bede1cc8dfd6"    # policy content_hash at 2026-09-24.1 (round 2; round-1 head 1d164aa was ee9d01057614)
  # Interpretation caveat carried from the protocol memory: whether
  # mock_projection_hash means this raw-YAML content hash is unconfirmed.
  # The ROB-1351 v2 sealed hashes do NOT move:
  #   PINNED_SPEC_SHA256_V2              c156fdb3c3fcd5e122bf71d37e64bc3b8feac087dee7f93f8ac1a012b2cca14f
  #   PINNED_POLICY_PROJECTION_SHA256_V2 33488817d1191b7ad54800da1b397682e1097627ce8fb454a36faf5394018507
mock_experiment_effect: UNKNOWN
  # Split verdict, see prose: ROB-1351 v2 = NONE (proved from code);
  # operator-repo mock pilots (kiwoom_mock H1 #661, kis_mock flagship) =
  # UNKNOWN because the exception reaches mock-account sizing.
rollback: >-
  Revert the PR. The exception is additive; no consumer stores derived state
  (the validator is pure and per-call), there is no migration, no scheduler,
  no broker/order-path change. Proposals already created from a validated
  table under 2026-09-24.1 remain as proposals and are stamped with that
  version; reverting does not cancel them (the operator would cancel by hand).
evidence: >-
  See "mock_experiment_effect" and "What is NOT changed" below; tests and
  mutants listed in the PR body.
```

## mock_experiment_effect — prose (required; the enum cannot hold the nuance)

**ROB-1351 v2 (`rob-1351-buy-gate-moderate-live`): NONE — membership cannot change.**

1. *What decides membership.* `evaluate_v2.evaluate_candidate`
   (`app/services/buy_gate_ab_shadow/evaluate_v2.py:319`) calls
   `_shared_reject_reasons` (`:263`: RSI < 45, support distance within 8%,
   honest upside ≥ 40, session review bits `liquid_midcap`/`concentration`/
   `overhang`) and `_support_ok` (`:286`: support strength vs moderate/weak).
   Every threshold comes from the sealed `PRE_REGISTRATION_V2`
   (`spec_v2.py:57` `shared_gates`), not from live policy. `current_price`
   (`:143`) is recorded only as the frozen entry price (`:369`); no
   notional, quantity, affordability or band input exists.
2. *What live policy the v2 path reads.* Only four keys, via
   `SEALED_TO_LIVE_POLICY_KEYS_V2` (`policy_alignment_v2.py:27`):
   `screen.support_strength_min`, `screen.rsi_max`,
   `screen.support_within_pct`, `screen.upside_min_pct`. None is touched;
   `test_v2_server_guards.py::test_current_real_policy_matches_sealed_v2_projection`
   passes on the new document (145/145 in `tests/services/buy_gate_ab_shadow`).
3. *Candidate supply.* `buy_candidate_fanout.py` reads no per-symbol band
   (grep: its only `notional` fields belong to the §177 underwater tier), so
   no high-priced KR candidate was ever filtered out upstream and none is
   newly let in.
4. *Hashes.* `app/services/buy_gate_ab_shadow/**` is byte-identical to
   origin/main; both pinned v2 hashes are unchanged.

**The version-stamp split.** Records that cite the policy stamp —
`decision_table_validate` responses (`validator.py:80`
`policy_version_stamp()`), the fan-out response
(`buy_candidate_fanout.py:1285`), and session evidence/forecasts that quote
`{version, content_hash}` — will read `2026-09-24.1 / bede1cc8dfd6` instead of
`2026-09-08.1 / 6d5a858ff5cb`. v2 forecast payloads themselves carry the sealed
`policy_projection_sha256`, not the live version, so they do not split; the
split is in surrounding session evidence and must be kept as a cohort
dimension when scoring (record both versions, do not pool silently).

**Unfavourable facts, not omitted.**
- v2 forecasts stamp a sealed `assumed_notional` = cap_krw 400,000 × 0.5 =
  200,000 KRW (`forecast_tag_v2.py:28-33`, `assumed_notional`). A candidate
  that is actually bought at one share of ~1,800,000 KRW under this exception
  is still recorded at 200,000 assumed notional. Returns are scored from the
  frozen decision price (`a_primary_entry: frozen_decision_price_not_fill`), so
  scores are unaffected, but the assumed notional no longer represents the live
  exposure for exception symbols.
- `concentration` is a session-supplied review bit. Code cannot prove a session
  will not judge a 5–10× larger one-share position differently on that bit;
  that would be a judgment channel into membership, outside this repo.
- The brief/decision doc describe v2 as a window closing 09-28. In this repo
  v2 is **unarmed** (`epoch_v2.py:1-4`, "No marker is selected here"); the
  `2026-09-28` boundary is ROB-1331 **v1**'s `collection_end_exclusive`
  (`spec.py:189`), and v1 was terminated. Production DB epoch rows were not
  inspected (no production access in this lane).

**Operator-repo mock pilots: UNKNOWN.** The exception is market-scoped, not
account-scoped: a `kiwoom_mock` or `kis_mock` decision table is validated
with the same admission. The proposal (§3) recommends disabling the exception
in the kiwoom_mock H1 pilot (E0 10,000,000; one exception entry = 20–30% of
E0). This PR does not implement that; the H1 session rule (#661) must state
it, or a follow-up must scope the exception by account_mode. KR-B1 is being
stopped and is not affected going forward.

## What is NOT changed

- Band value `[200000, 400000]`; the exception never covers a below-band
  order, a multi-share order, a sell, a row with held-position evidence or
  without the affirmative flat proof, or a cash-parking symbol.
- `order_proposals.auto_approve.per_order_cap.kr` = 2,000,000 and
  `daily_cap.kr` = 5,000,000. `auto_approve.py` is not taught the exception:
  a one-share buy above 2,000,000 is demoted to a human card as
  `per_order_cap_exceeded` (`auto_approve.py:877`), asserted by
  `test_over_per_order_cap_one_share_is_valid_and_then_carded_not_rejected`.
  The one KR buy scope whose per-order cap is raised (cash parking,
  459580/357870, 10,000,000 in expanded mode) is excluded from the exception,
  so "above 2,000,000 → card" holds for every symbol the exception admits
  (`test_cash_parking_symbols_never_take_the_exception`).
- RSI / support strength / support distance / honest upside / deep band,
  `portfolio.sector_cluster_cap_pct`, `portfolio.max_symbols_per_theme`.
- No migration, no scheduler, no broker call, no order-path code.

## Counter-evidence (carried from the proposal, US §139 style)

One share at ~2,000,000 KRW is 5–10× a standard 200,000–400,000 tranche; a
−10% move on it costs 5–10× a standard tranche loss. The book (~99M KRW)
keeps concentration near 2%, but the `sell.loss_cut` path is still awaiting
operator approval (087010 open question), so a losing one-share position is
managed by hand until it is.

## Round-1 independent verification (codex-sol, OpenAI) — FAIL, fixed in round 2

Report: `~/work/herdr-inbox/jobs/v664-one-share-codex/report.md` (head 1d164aa).
The devin-ds41 verdict is kept on record separately; per director-1, ds41
alone does not satisfy a T3 gate (hk:doc 2227).

1. *Held evidence by closed list* — `held_qty gt 0` and `action.position_qty`
   passed. Fix: the exception now requires an **affirmative**
   `position_quantity eq 0` condition whose `source` names the row's symbol,
   and any other holding-shaped row key, action key, condition key, metric or
   source (NFKC/lower-cased substring set) denies it. Silence is not a new
   entry.
2. *Parking cross-exception* — `459580`/`357870` one-share at 2,500,000 validated
   and, in expanded mode, auto-approved under the raised 10,000,000 parking
   per-order cap with no card. Fix: every `PARKING_ALLOWLIST_SCOPES` symbol is
   denied the exception (pre-§664 behaviour restored for them).
3. *Rung budget keyed on raw strings* — `000660` / `000660 ` each got a rung;
   `['000660','000660']` counted as one symbol. Fix: the exception requires the
   raw `symbols` list to be exactly one canonical six-character ASCII KRX
   code, and the table-wide budget is keyed on NFKC+strip+upper.

## Known residual limits of the code boundary

- `decision_table_validate` is pure and cannot read holdings. It trusts the
  declared `position_quantity eq 0` condition; `decision_table_apply` does not
  evaluate row conditions either, so the live check is the helmsman session's
  condition match before a real apply. A table that *lies* about a held
  symbol (declares flat while held) is not caught here.
- The prep prompt (operator repo) must emit the `position_quantity eq 0`
  condition on exception rows, otherwise the exception is never granted
  (fail-closed; a usability cost, not a safety one).
- The exception is only code-enforced where a decision table is validated.
  Session-written proposals that bypass decision tables were never band-checked
  in code (the band is advisory under ROB-646); for them the exception is a
  policy statement, and the per-order cap remains the code boundary.
- `max_deep_rungs` is counted per table (all rows, all accounts), not across
  separate tables/days.
