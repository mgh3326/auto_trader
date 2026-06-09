import json
import os

from external_strategy_sieve.catalog import load_catalog
from external_strategy_sieve.rubric import RUBRIC
from external_strategy_sieve.scorer import freeze_shortlist, score_card


def _card_dict(cid):
    return {
        "candidate_id": cid,
        "source_url": "https://example.com/x",
        "source_bucket": "freqtrade_github",
        "license": "MIT",
        "code_availability": "open",
        "strategy_family": "trend",
        "spot_or_futures": "spot",
        "long_short": "long_only",
        "timeframe": "1h",
        "holding_horizon": "intraday",
        "entry_exit_summary": "x",
        "data_requirements": ["ohlcv"],
        "tail_risk_flags": [],
        "lookahead_repaint_risk": "low",
        "implementation_complexity": "low",
        "novelty_vs_failed_families": "adjacent",
        "expected_cost_sensitivity": "medium",
        "source_verified": False,
        "score_status": "unverified_seed",
        "recommended_disposition_pre_validation": "shadow_only",
    }


def test_load_valid_catalog(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([_card_dict("a"), _card_dict("b")]))
    load = load_catalog(str(p))
    assert load.errors == ()
    assert [c.candidate_id for c in load.cards] == ["a", "b"]


def test_duplicate_id_is_an_error(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([_card_dict("dup"), _card_dict("dup")]))
    load = load_catalog(str(p))
    assert any("duplicate" in e.lower() and "dup" in e for e in load.errors)


def test_bad_enum_propagates_as_error(tmp_path):
    bad = _card_dict("a")
    bad["source_bucket"] = "reddit"
    p = tmp_path / "c.json"
    p.write_text(json.dumps([bad]))
    load = load_catalog(str(p))
    assert any("source_bucket" in e for e in load.errors)


def test_unknown_field_is_an_error(tmp_path):
    bad = _card_dict("a")
    bad["surprise"] = 1
    p = tmp_path / "c.json"
    p.write_text(json.dumps([bad]))
    load = load_catalog(str(p))
    assert any("surprise" in e for e in load.errors)


# --- Committed catalog (post-survey) end-to-end tests ---

_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "candidates.json"
)


def test_catalog_loads_clean_and_meets_minimum():
    # Acceptance: >=15 well-described candidates, no validation errors.
    load = load_catalog(_CATALOG_PATH)
    assert load.errors == ()
    assert len(load.cards) >= 15


def test_catalog_covers_at_least_four_buckets():
    load = load_catalog(_CATALOG_PATH)
    buckets = {c.source_bucket for c in load.cards}
    assert len(buckets) >= 4


def test_catalog_shortlist_is_verified_only_and_diverse():
    # R1 + R4 end-to-end on the real catalog: the frozen shortlist contains only
    # source-verified, non-rejected candidates and is family-diverse.
    load = load_catalog(_CATALOG_PATH)
    scored = [score_card(c, RUBRIC) for c in load.cards]
    result = freeze_shortlist(scored, RUBRIC)
    assert result.shortlist  # non-empty
    assert len(result.shortlist) <= RUBRIC.shortlist_max
    for s in result.shortlist:
        assert s.score_status == "verified"
        assert s.disposition != "reject"
    fam_counts = {}
    for s in result.shortlist:
        fam_counts[s.strategy_family] = fam_counts.get(s.strategy_family, 0) + 1
    assert all(c <= RUBRIC.max_per_family for c in fam_counts.values())
    assert len(fam_counts) >= RUBRIC.min_distinct_families


def test_catalog_non_verified_cards_never_shortlisted():
    # R1: taxonomy_only / unverified cards must not be source_verified-eligible.
    load = load_catalog(_CATALOG_PATH)
    scored = [score_card(c, RUBRIC) for c in load.cards]
    eligible_ids = {s.candidate_id for s in scored if s.eligible_for_shortlist}
    for c in load.cards:
        if c.score_status != "verified" or not c.source_verified:
            assert c.candidate_id not in eligible_ids
