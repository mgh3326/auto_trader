from external_strategy_sieve.scorer import score_card, bucketize, freeze_shortlist
from external_strategy_sieve.rubric import RUBRIC
from external_strategy_sieve.tests.test_schema import _good_card


def _strong_verified(**overrides):
    base = dict(
        license="MIT", code_availability="open", data_requirements=("ohlcv",),
        expected_cost_sensitivity="low", spot_or_futures="both",
        novelty_vs_failed_families="novel", holding_horizon="intraday",
        implementation_complexity="low", lookahead_repaint_risk="none",
        tail_risk_flags=(), source_verified=True, score_status="verified",
        recommended_disposition_pre_validation="keep",
    )
    base.update(overrides)
    return _good_card(**base)


def test_strong_card_scores_keep_and_is_eligible():
    s = score_card(_strong_verified(), RUBRIC)
    assert s.disposition == "keep"
    assert s.composite_normalized == 100.0
    assert s.eligible_for_shortlist is True
    assert s.gates_triggered == ()


def test_unverified_seed_is_never_eligible():
    # R1: even a metadata-strong seed cannot be shortlist-eligible.
    s = score_card(_strong_verified(source_verified=False, score_status="unverified_seed"), RUBRIC)
    assert s.eligible_for_shortlist is False


def test_high_cost_card_cannot_be_keep():
    # G5 cost cap.
    s = score_card(_strong_verified(expected_cost_sensitivity="high"), RUBRIC)
    assert "G5_cost" in s.gates_triggered
    assert s.disposition != "keep"
    assert s.eligible_for_shortlist is False


def test_martingale_card_is_capped_below_keep():
    # R5 / G3: a high-composite card with martingale cannot be keep.
    s = score_card(_strong_verified(tail_risk_flags=("martingale",)), RUBRIC)
    assert "G3_severe_tail_risk" in s.gates_triggered
    assert s.disposition != "keep"


def test_gpl_card_triggers_license_gate():
    # G1: strong copyleft (license_safety<=1) cannot be keep.
    s = score_card(_strong_verified(license="GPL-3.0"), RUBRIC)
    assert "G1_license" in s.gates_triggered
    assert s.disposition != "keep"


def test_opaque_code_triggers_gate():
    # G2: opaque/code_not_confirmed cannot be keep.
    s = score_card(_strong_verified(code_availability="opaque"), RUBRIC)
    assert "G2_code_opaque" in s.gates_triggered
    assert s.disposition != "keep"


def test_weak_card_lands_reject_by_band():
    s = score_card(_good_card(
        license="proprietary", code_availability="opaque",
        data_requirements=("ohlcv", "fundamentals"), expected_cost_sensitivity="high",
        spot_or_futures="spot", novelty_vs_failed_families="duplicate",
        holding_horizon="position", implementation_complexity="high",
        lookahead_repaint_risk="high", tail_risk_flags=("martingale",),
        source_verified=True, score_status="verified",
    ), RUBRIC)
    assert s.disposition == "reject"


def test_scoring_is_deterministic():
    card = _strong_verified()
    assert score_card(card, RUBRIC) == score_card(card, RUBRIC)


# --- Bucket + shortlist tests ---


def _verified_keep(cid, family):
    return _strong_verified(candidate_id=cid, strategy_family=family)


def test_bucketize_separates_by_status():
    cards = [
        _strong_verified(candidate_id="v1"),
        _good_card(candidate_id="seed1"),  # unverified_seed
        _good_card(candidate_id="tax1", score_status="taxonomy_only"),
        _good_card(candidate_id="cnc1", score_status="code_not_confirmed"),
        _good_card(candidate_id="ua1", score_status="source_unavailable"),
        _good_card(candidate_id="rej1", score_status="reject"),
    ]
    scored = [score_card(c, RUBRIC) for c in cards]
    buckets = bucketize(scored)
    assert [s.candidate_id for s in buckets["verified_ranked"]] == ["v1"]
    assert [s.candidate_id for s in buckets["unverified_seed"]] == ["seed1"]
    assert {s.candidate_id for s in buckets["taxonomy_only"]} == {"tax1", "cnc1"}
    assert [s.candidate_id for s in buckets["source_unavailable"]] == ["ua1"]
    assert [s.candidate_id for s in buckets["reject"]] == ["rej1"]


def test_verified_ranked_is_sorted_desc_with_id_tiebreak():
    a = score_card(_verified_keep("bbb", "trend"), RUBRIC)
    b = score_card(_verified_keep("aaa", "breakout"), RUBRIC)
    buckets = bucketize([a, b])
    # equal composite -> tie-break ascending candidate_id
    assert [s.candidate_id for s in buckets["verified_ranked"]] == ["aaa", "bbb"]


def test_freeze_shortlist_refuses_unverified():
    # R1 end-to-end: a non-verified card may never enter the shortlist.
    scored = [score_card(_good_card(candidate_id=f"seed{i}"), RUBRIC) for i in range(8)]
    result = freeze_shortlist(scored, RUBRIC)
    assert result.shortlist == ()
    assert "no source-verified keep candidates" in " ".join(result.gaps).lower()


def test_freeze_shortlist_enforces_family_diversity():
    # 5 trend + 2 breakout + 1 volatility, all verified-keep.
    families = ["trend"] * 5 + ["breakout", "breakout", "volatility"]
    scored = [score_card(_verified_keep(f"c{i}", fam), RUBRIC) for i, fam in enumerate(families)]
    result = freeze_shortlist(scored, RUBRIC)
    fam_counts = {}
    for s in result.shortlist:
        fam_counts[s.strategy_family] = fam_counts.get(s.strategy_family, 0) + 1
    assert all(c <= RUBRIC.max_per_family for c in fam_counts.values())
    assert len(set(s.strategy_family for s in result.shortlist)) >= RUBRIC.min_distinct_families


def test_freeze_shortlist_reports_gap_when_too_few_families():
    # Only 2 distinct families available but min is 3.
    families = ["trend", "trend", "breakout", "breakout"]
    scored = [score_card(_verified_keep(f"c{i}", fam), RUBRIC) for i, fam in enumerate(families)]
    result = freeze_shortlist(scored, RUBRIC)
    assert any("distinct families" in g.lower() for g in result.gaps)
