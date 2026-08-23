"""Frozen addendum for the ROB-1301 *historical* backtest.

The upstream experiment spec lives in ``app.services.buy_gate_ab_shadow.spec``
and is NOT restated or edited here. This module pins only the choices the
upstream spec cannot make for a past-sample replay: which corpus, which
decision dates, which universe, and which pre-registered gates cannot be
reconstructed from bar data alone.

Every value below is fixed *before* any result is read. Changing one changes
``addendum_sha256()`` and fails ``test_addendum_freeze``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

ADDENDUM_ID: Final = "rob-1301-buy-gate-ab-historical-backtest"

# 🔴 The three upstream prohibitions carry over verbatim, plus one that is
# specific to running the experiment on a sample whose outcome already exists.
FORBIDDEN: Final[tuple[str, ...]] = (
    "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
    "라이브 게이트 문언 무접촉",
    "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
    "결과를 본 뒤 조건·공식·표본 범위를 변경 금지(과거 표본이므로 재실행이 자유롭다는 "
    "사실이 곧 이 금지의 존재 이유)",
)

ADDENDUM: Final[dict[str, Any]] = {
    "addendum_id": ADDENDUM_ID,
    "issue": "ROB-1301",
    "relation_to_live_shadow": "prior_sample_only_does_not_replace_live_collection",
    "authority": {
        "gate_logic": "app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate",
        "sample_scoring_formula": "app.services.buy_gate_ab_shadow.scoring.score_window",
        "support_resistance": (
            "app.mcp_server.tooling.fundamentals._support_resistance."
            "get_support_resistance_impl"
        ),
        "rsi": "app.mcp_server.tooling.market_data_indicators._compute_indicators",
        "reimplementation_of_the_above": "forbidden",
    },
    # ---- corpora (frozen, offline, read-only) -----------------------------
    "corpora": {
        "kr": {
            "corpus_id": "kr-corpus-v1",
            "root": (
                "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/runs/"
                "kr-corpus-v1-20260803-1001/dataset"
            ),
            "scope": "main_exploration_only",
            "window": ["2015-01-01", "2024-12-31"],
            "price_mode": "adjusted",
        },
        "us": {
            "corpus_id": "us-corpus-v1",
            "root": "/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/dataset",
            "scope": "main_exploration_only",
            "window": ["2016-01-01", "2024-12-31"],
            "price_mode": "adjusted",
            "survivorship_biased": True,
        },
        "crypto": {
            "corpus_id": "crypto-corpus-v1",
            "root": "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset",
            "scope": "main_exploration_only",
            "frequency": "1d",
            "venues": ["upbit_krw", "binance_usdt_spot"],
            "note": (
                "crypto is outside the upstream spec's markets ['kr','us']; it is "
                "reported as an annex under an explicitly relabelled market and is "
                "never mixed into the kr/us tables."
            ),
        },
    },
    # 🔴 The sealed 2025-01-01..2026-07-31 holdout of all three corpora is NOT
    # opened by this backtest. No holdout access-log line is written because no
    # holdout byte is read. Extending into it is an operator decision, not this
    # job's.
    "holdout": {
        "window": ["2025-01-01", "2026-07-31"],
        "opened": False,
        "reads": 0,
        "extension_requires": "explicit_operator_approval",
    },
    # ---- implementation constants (S2) ------------------------------------
    # 🔴 These four literals used to live only in the runner and reconstructor,
    # so the digest test could not see them drift. They are authoritative here
    # now and the modules read them from this dict. The VALUES are unchanged
    # from the first freeze commit c9a3270 -- git blame is the evidence that
    # nothing was retuned; only their home moved.
    "constants": {
        "cadence_sessions": 5,
        "liquidity_lookback_sessions": 20,
        "rsi_window_bars": 250,
        "support_resistance_window_bars": 60,
    },
    # ---- decision-date sampling -------------------------------------------
    "decision_dates": {
        "cadence": "every_5th_trading_session",
        "cadence_reason": (
            "compute bound, fixed before any result was read; the offset is the "
            "corpus's first eligible session, not a tuned phase"
        ),
        "offset": "first_session_with_250_prior_bars",
        "calendar": "union_of_session_dates_present_in_the_corpus",
    },
    # ---- point-in-time universe -------------------------------------------
    "universe": {
        "min_bars_at_decision": 250,
        "min_bars_reason": "live analyze_stock fetches count=250 for indicators",
        "liquidity_floor_20d_median_traded_value": {
            "kr": 1_000_000_000,
            "us": 5_000_000,
            "crypto_upbit_krw": 1_000_000_000,
            "crypto_binance_usdt_spot": 5_000_000,
        },
        "liquidity_floor_reason": (
            "stands in for the non-reconstructible liquid_midcap bit; sized so a "
            "400,000 KRW / 450 USD assumed order is not a market-moving fraction"
        ),
        "per_date_cap": None,
        "ranking_or_top_n_selection": "none",
    },
    # ---- evidence reconstruction ------------------------------------------
    "evidence": {
        "current_price": "decision_session_close",
        "current_price_note": (
            "matches the live KR/crypto branch (df.close.iloc[-1]); the live US "
            "branch overrides with an intraday quote, which no corpus contains, so "
            "the US run is pinned to the same close-based branch and labelled"
        ),
        "rsi_window_bars": 250,
        "support_resistance_window_bars": 60,
        "support_level_selection": "strongest_within_8pct_below_price_tiebreak_nearest",
        "support_level_selection_reason": (
            "must not reference either variant's strength threshold, or the two "
            "arms would no longer see one identical evidence record"
        ),
    },
    # ---- gates that a bar corpus cannot reconstruct ------------------------
    # 🔴 Each is neutralised to a PASS *identically for A and B*. Neutralising a
    # shared gate enlarges both cohorts by the same rule; it cannot move the
    # A-vs-B contrast, which is the measured quantity. It does make every
    # absolute admit count an upper bound.
    "non_reconstructible_gates": {
        "honest_upside_pct_min_40": {
            "definition": "(analyst avg target price - current price) / current price",
            "why": "point-in-time analyst consensus targets are in no corpus",
            "treatment": "neutralised_to_pass_for_both_arms",
        },
        "other_gate_bit_liquid_midcap": {
            "why": "point-in-time market cap / free float are in no corpus",
            "treatment": "neutralised_to_pass_for_both_arms",
            "partial_stand_in": "universe liquidity floor above",
        },
        "other_gate_bit_concentration": {
            "why": "portfolio-state dependent; no portfolio exists in a backtest",
            "treatment": "neutralised_to_pass_for_both_arms",
        },
        "other_gate_bit_overhang": {
            "why": "no frozen definition available offline",
            "treatment": "neutralised_to_pass_for_both_arms",
        },
        "independent_support_family_count_min_2": {
            "why": (
                "live buy_candidate_fanout gate, but NOT part of the ROB-1301 "
                "pre-registered shared_gates; adding it would be a new gate"
            ),
            "treatment": "recorded_as_annex_only_never_applied_as_a_gate",
        },
    },
    # 🔴 Compute shortcut, fixed before the first run: when RSI >= 45 the shared
    # gate already rejects for BOTH arms whatever the support looks like, so the
    # 60-bar support/resistance reconstruction is skipped for those rows and
    # their support fields are recorded as "not_computed". Cohort assignment is
    # provably unchanged (tests/test_rsi_shortcut_is_cohort_neutral).
    "compute_shortcut": {
        "skip_support_resistance_when": "rsi >= 45",
        "cohort_effect": "none",
        "cost_effect": "support/resistance computed for ~40% of universe rows",
        "reporting_effect": (
            "the control cohort's support-strength histogram covers only its "
            "rsi-passing rows and is labelled as such"
        ),
    },
    # ---- scoring ----------------------------------------------------------
    "scoring": {
        "per_sample_formula": "unchanged_from_buy_gate_ab_shadow.scoring.score_window",
        "windows_trading_days": [5, 20],
        "cohorts": {
            "A": "a_and_b",
            "B_minus_A": "b_only",
            "control_all_fail": "neither",
        },
        "aggregations_added": [
            "median",
            "win_rate_return_gt_zero",
            "median_max_drawdown",
            "percentiles_p10_p90",
        ],
        "scoring_as_of": "last_session_present_in_that_corpus",
        "samples_without_a_full_forward_window": "reported_unscoreable_never_imputed",
        "aggregations_added_note": (
            "additive only; the per-sample numbers being aggregated are produced "
            "by the pre-registered score_window and are not recomputed here"
        ),
        "single_scoring_as_of": True,
        "winner_declaration": "forbidden",
        "policy_recommendation": "out_of_scope_for_this_job",
    },
    # 🔴 Post-freeze amendment, disclosed rather than folded in silently.
    # 2026-08-21: a cluster bootstrap over decision dates
    # (research/buy_gate_ab_backtest/stats.py) was added after this digest was
    # pinned. It is left OUT of the pinned dict on purpose — folding it in
    # would rewrite the digest and erase the evidence that the gate and
    # sampling rules were fixed first. It puts an interval around aggregates
    # that already existed; it changes no gate, no sample, and no per-sample
    # number, and it is applied identically to all three cohorts. The report
    # labels it as post-freeze.
    "forbidden": list(FORBIDDEN),
    # ---- r2 repair round (2026-08-21, post adversarial verification) ------
    # The T2 review returned BLOCKER 2 / SHOULD 5. The repairs that touch this
    # dict are recorded here rather than applied silently:
    #   S2  the four constants above moved into the digest's coverage
    #   B1  build_evidence now takes decision_date and refuses future bars
    #   B2  D+20 censoring is classified and reported, D+5 becomes primary
    # Re-pinning the digest is unavoidable when the covered dict grows. The
    # first freeze digest is kept below so the original freeze stays checkable.
    "amendments": {
        "r2_2026_08_21": {
            "trigger": "adversarial verification BLOCKER 2 / SHOULD 5",
            "superseded_addendum_sha256": (
                "648005cb032f1db202151eb2f813c6f7b7e8796e6ff457764aeaa212572055da"
            ),
            "constant_values_changed": False,
            "gate_thresholds_changed": False,
            "sampling_rule_changed": False,
            "universe_rule_changed": False,
            "what_changed": "coverage and reporting, not the experiment",
        },
    },
    "network_calls": 0,
    "operating_db_reads": 0,
    "operating_db_writes": 0,
    "broker_calls": 0,
    "app_source_changes": 0,
}

# Recomputed by research/buy_gate_ab_backtest/tests/test_addendum_freeze.py.
PINNED_ADDENDUM_SHA256: Final = (
    "64816142bc820547bde366cf10fe2e440edcbb1d7773e3f5f7aa5a67a1635c67"
)
# The digest as it stood at the first freeze, before the r2 repair round grew
# the covered dict. Kept so that freeze can still be checked independently.
FIRST_FREEZE_ADDENDUM_SHA256: Final = (
    "648005cb032f1db202151eb2f813c6f7b7e8796e6ff457764aeaa212572055da"
)


def canonical_addendum_bytes(payload: dict[str, Any] | None = None) -> bytes:
    body = ADDENDUM if payload is None else payload
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def addendum_sha256(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_addendum_bytes(payload)).hexdigest()
