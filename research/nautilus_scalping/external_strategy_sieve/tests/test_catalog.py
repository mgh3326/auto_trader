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


# --- Seed-catalog end-to-end tests ---

_SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "candidates.json")


def test_seed_catalog_loads_clean():
    load = load_catalog(_SEED_PATH)
    assert load.errors == () or load.errors == []
    assert 8 <= len(load.cards) <= 12


def test_seed_catalog_covers_at_least_four_buckets():
    load = load_catalog(_SEED_PATH)
    buckets = {c.source_bucket for c in load.cards}
    assert len(buckets) >= 4


def test_all_seeds_are_unverified_and_not_shortlist_eligible():
    # R1: cold-start seeds must never be promotable before verification.
    load = load_catalog(_SEED_PATH)
    assert all(c.score_status == "unverified_seed" for c in load.cards)
    assert all(c.source_verified is False for c in load.cards)
    scored = [score_card(c, RUBRIC) for c in load.cards]
    assert all(not s.eligible_for_shortlist for s in scored)
    result = freeze_shortlist(scored, RUBRIC)
    assert result.shortlist == ()
