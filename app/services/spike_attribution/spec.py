"""Frozen pre-registration for ROB-1303 daily spike attribution.

Spike definition, evidence window, attribution vocabulary, follow-through
scoring formula, sample floors, and the forbidden acts are pinned here *before*
any attribution row is scored. Changing this dict changes ``spec_sha256()`` and
fails the pin test — that is the point.

Do not live-read ``trading_policy.yaml``: a later policy edit must not retcon
which spikes were in scope or how their follow-through was measured.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

EXPERIMENT_ID: Final = "rob-1303-spike-attribution"

# Attribution type vocabulary. The first five are the operator-enumerated
# buckets (실적·공시·수급·섹터·unattributed). ``news`` is an explicit addition:
# a broker research note or a media report that is not backed by a filing maps
# to none of the five, and forcing it into ``disclosure`` would be a lie while
# forcing it into ``unattributed`` would discard a real, linkable document.
ATTRIBUTION_TYPES: Final[tuple[str, ...]] = (
    "earnings",
    "disclosure",
    "flow",
    "sector",
    "news",
    "unattributed",
)

# Types that carry a cause. ``unattributed`` is the honest absence of one and is
# never a bucket that other types fall back into after the fact.
ATTRIBUTED_TYPES: Final[frozenset[str]] = frozenset(ATTRIBUTION_TYPES) - {
    "unattributed"
}

# Issue ROB-1303 forbidden acts — copied, not paraphrased.
FORBIDDEN: Final[tuple[str, ...]] = (
    "원인을 발명하지 마라 — 재료로 설명되지 않으면 unattributed",
    "unattributed 를 기타·시장 전반 같은 말로 분칠하지 마라",
    "후보가 여럿이면 여럿으로 남겨라 — 하나로 단정하지 마라",
    "귀속 레코드가 제안·주문·워치로 승격되는 경로 0",
    "채점 완료 전 중간값으로 정책·임계값 변경 논거 삼지 않기",
)

PRE_REGISTRATION: Final[dict[str, Any]] = {
    "experiment_id": EXPERIMENT_ID,
    "issue": "ROB-1303",
    "purpose": (
        "일일 급등락 종목의 원인 후보를 증거 링크와 함께 박제하고, "
        "카탈리스트 유형별 follow-through 지속성을 사전등록 채점한다"
    ),
    "markets": ["kr", "us"],
    "pilot_scope": {
        "market": "kr",
        "session_date": "2026-08-20",
        "symbols": ["035420", "035720"],
        "us_status": "structurally_supported_unexercised",
    },
    # --- spike detection -------------------------------------------------
    "spike_detection": {
        "abs_change_pct_min": 5.0,
        "bases": ["close_to_close", "intraday_extreme"],
        "trigger": "either_basis",
        "close_to_close": "(close - prev_close) / prev_close",
        "intraday_extreme_up": "(high - prev_close) / prev_close",
        "intraday_extreme_down": "(low - prev_close) / prev_close",
        "direction_rule": (
            "sign_of_close_to_close_falling_back_to_intraday_extreme_when_close_unchanged"
        ),
        "direction_rationale": (
            "follow-through anchors on the close, so direction must agree with "
            "the sign of (close - prev_close) or the retention denominator "
            "contradicts the event's own label"
        ),
        "both_directions_in_scope": True,
        "prev_close_source": "previous_row_in_the_same_daily_series",
        "requires_prev_close": True,
        "no_prev_close_verdict": "skip_not_spike",
        "halted_suspect_rule": "excluded_and_reported_never_silently_dropped",
    },
    # --- evidence window -------------------------------------------------
    # Half-open (prev_session_close, spike_session_close]. Anything published
    # after the spike session closed cannot have caused that session's move and
    # is recorded as ``after_move`` — visible, never eligible.
    "evidence_window": {
        "start_exclusive": "previous_trading_day_session_close",
        "end_inclusive": "spike_day_session_close",
        "session_close_local": {"kr": "15:30", "us": "16:00"},
        "session_close_tz": {"kr": "Asia/Seoul", "us": "America/New_York"},
        "kr_close_note": (
            "KRX 정규장 마감 기준. NXT after-hours(20:00)는 일봉 종가의 근거가 "
            "아니므로 창에 포함하지 않는다"
        ),
        "after_window_disposition": "recorded_as_after_move_never_eligible",
        "missing_timestamp_disposition": (
            "recorded_as_timestamp_unknown_never_eligible"
        ),
    },
    # --- materials -------------------------------------------------------
    # Assembled from what already exists. No new feed, scraper, or credential.
    "materials": {
        "news": {
            "tables": ["news_articles", "news_article_related_symbols"],
            "judgment_table": "symbol_news_relevance",
            "judgment_absent_disposition": "unjudged_candidate_never_auto_excluded",
            "rob_491_rule": "auto_trader code never auto-excludes an article",
            "status_vocabulary": ["confirmed", "pending", "excluded"],
            "status_written_by": "external judgment job only, never by this code",
        },
        "disclosure": {
            "table": "market_events",
            "source": "dart",
            "symbol_linkage": "company_name_only_symbol_column_is_null",
            "intraday_time_source": "raw_payload_json.rcept_dt",
            "intraday_time_note": (
                "normalizer 가 release_time_local 을 버리므로 raw_payload 에서 "
                "읽는다. 파생 출처를 레코드에 명시한다"
            ),
        },
        "earnings": {
            "table": "market_events",
            "categories": ["earnings"],
        },
        "flow": {
            "table": "investor_flow_snapshots",
            "availability": "t_plus_1",
            "same_day_disposition": "unavailable_at_attribution_time",
            "eligible_as_cause_in_v1": False,
            "ineligibility_reason": (
                "same-day flow is concurrent with the move (net buying is the "
                "move, not its antecedent), and this repo records only our own "
                "collected_at for the prior day's snapshot, not when the "
                "exchange published it — so a pre-move timestamp would have to "
                "be invented. Recorded as context only."
            ),
        },
        "sector": {
            "tables": ["symbol_sectors", "kr_symbol_universe.sector_id"],
            "coverage": "lazy_fill_partial",
            "absent_disposition": "sector_evidence_unavailable_not_absent_cause",
            "eligible_as_cause_in_v1": False,
            "ineligibility_reason": (
                "sector co-movement is measured on the same session as the "
                "move, so it classifies the move rather than preceding it; "
                "KR sector_id coverage is also partial lazy-fill. Recorded as "
                "context only."
            ),
        },
        "timestamp_trust": {
            "rule": "per-feed clock registry; unregistered or unconfirmed → never eligible",
            "confirmed_kr_exact": ["http_naver_stock_aggregate"],
            "confirmed_kr_date_only": [
                "browser_naver_research",
                "browser_naver_research_company",
                "browser_naver_research_economy",
                "browser_naver_research_industry",
                "browser_naver_research_invest",
                "naver_item_news",
            ],
            "date_only_disposition": "timestamp_unknown_never_eligible",
            "us_feeds": "tz_unconfirmed_never_eligible_pending_confirmation",
        },
        "forbidden_new_sources": True,
    },
    # --- attribution ------------------------------------------------------
    "attribution": {
        "types": list(ATTRIBUTION_TYPES),
        "multi_candidate": "keep_all_eligible_candidates_ranked_never_collapsed",
        "rank_key": "type_priority_then_recency_within_window",
        "type_priority": ["disclosure", "earnings", "news", "flow", "sector"],
        "single_cause_declaration": "forbidden",
        "no_eligible_evidence_verdict": "unattributed",
        "unattributed_relabeling": "forbidden",
        "confidence_vocabulary": [
            "judged_relevant",
            "judged_not_relevant",
            "unjudged",
            "not_applicable",
        ],
        "judgment_status_map": {
            "confirmed": "judged_relevant",
            "excluded": "judged_not_relevant",
            "pending": "unjudged",
            "no_row": "unjudged",
        },
        "judged_not_relevant_disposition": (
            "honour the external ROB-491 verdict — not a cause — but keep the "
            "row on the record with that reason rather than deleting it"
        ),
        "pending_is_not_a_verdict": True,
        "invented_cause": "forbidden",
        "types_reachable_in_v1": ["disclosure", "earnings", "news", "unattributed"],
        "types_unreachable_in_v1": ["flow", "sector"],
        "unreachable_note": (
            "두 유형은 어휘에 남아 있으나 v1 재료로는 사전(事前) 증거가 될 수 없다 "
            "— materials.flow / materials.sector 의 ineligibility_reason 참조. "
            "이 사실은 유형별 채점 분모에서 두 클래스가 비어 있음을 뜻하며, "
            "그것을 unattributed 로 흡수하지 않는다"
        ),
    },
    # --- hook (a): momentum_spike catalyst_basis --------------------------
    "catalyst_basis_hook": {
        "consumer_policy_tier": "momentum_spike_profit_ladder",
        "policy_required_thesis_evidence": ["catalyst_basis", "flow_basis"],
        "supplies": ["catalyst_basis"],
        "does_not_supply": ["flow_basis"],
        "flow_basis_reason": "investor flow is T+1; not available on the spike day",
        "unattributed_satisfies_requirement": False,
        "can_loosen_live_gate": False,
        "can_place_or_propose_order": False,
    },
    # --- hook (b): follow-through pre-registered scoring ------------------
    "follow_through": {
        "anchor": "spike_day_close",
        "reference": "prev_close",
        "windows_trading_days": [3, 10],
        "primary_metric": "retention_ratio",
        "retention_ratio": (
            "(close_at_window_end - prev_close) / (spike_close - prev_close)"
        ),
        "retention_ratio_sign_note": (
            "denominator carries the spike direction, so a down spike scores on "
            "the same formula without a separate branch"
        ),
        "verdict_bins": {
            "extended": "ratio >= 1.0",
            "retained": "0.5 <= ratio < 1.0",
            "faded": "0.0 <= ratio < 0.5",
            "reversed": "ratio < 0.0",
        },
        "sensitivity_metrics": [
            "max_favorable_excursion_ratio",
            "max_adverse_excursion_ratio",
        ],
        "single_scoring_as_of": True,
        "same_formula_all_types": True,
        "do_not_impute_missing_bars": True,
        "insufficient_bars_verdict": "unscorable",
        "bars_after_scoring_as_of_ignored": True,
        "peeking": "forbidden",
        "collection_extension_after_peek": "forbidden",
        "min_events_per_type_for_comparison": 20,
        "below_floor_disposition": "report_counts_only_no_cross_type_comparison",
        "unattributed_is_a_scored_class": True,
        "winner_declaration_before_floor": "forbidden",
    },
    "forecast_tagging": {
        "session_label": EXPERIMENT_ID,
        "cohort": "spike_attribution",
        "promote": False,
        "calibration_eligibility": "calibration_exclude",
        "trade_performance_eligibility": "trade_performance_exclude",
        "probability_placeholder": "0.5",
        "kind": "price_target",
        "direction_rule": "at_or_above_for_up_spike_at_or_below_for_down_spike",
        "target_price_rule": "prev_close + 0.5 * (spike_close - prev_close)",
        "target_price_meaning": "the retained/faded boundary of retention_ratio",
        "zero_denominator_disposition": (
            "close == prev_close is unscorable by construction, so it is not "
            "pre-registered at all rather than recorded as a meaningless row"
        ),
        "outcome_rule_version": "rob1303-retention-ratio-v1",
        "review_date_calendar_offset_days_by_window": {"3": 5, "10": 14},
        "scoring_authority": "rob-1303-spike-attribution.scoring",
        "do_not_use_forecast_resolve_as_experiment_score": True,
    },
    "forbidden": list(FORBIDDEN),
    "scheduler_registration": False,
    "broker_or_order_surface": False,
    "new_credential_surface": False,
}

# Recomputed by tests/services/spike_attribution/test_spec_freeze.py.
# Bump only together with an explicit pre-registration amendment.
PINNED_SPEC_SHA256: Final = (
    "373e908f9f4adc84b1d17cb77695019ffaf740f977e3e4224f4417499c9e0f76"
)


def canonical_spec_bytes(payload: dict[str, Any] | None = None) -> bytes:
    body = PRE_REGISTRATION if payload is None else payload
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def spec_sha256(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_spec_bytes(payload)).hexdigest()


__all__ = [
    "ATTRIBUTED_TYPES",
    "ATTRIBUTION_TYPES",
    "EXPERIMENT_ID",
    "FORBIDDEN",
    "PINNED_SPEC_SHA256",
    "PRE_REGISTRATION",
    "canonical_spec_bytes",
    "spec_sha256",
]
