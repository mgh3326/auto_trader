import pytest

from app.services import trading_policy_service as svc


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == "2026-07-12.1"
    assert len(stamp["content_hash"]) == 12


def test_content_hash_stable_across_calls():
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == "2026-07-12.1"
    assert view["content_hash"]
    t = view["thresholds"]
    # buy lane references these (playbook lane tags)
    assert t["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert t["portfolio.sector_cluster_cap_pct"]["source"] == "default"
    assert t["recovery_gate.min_conditions_met"]["value"] == 2
    assert t["sell.loss_guard_min_multiple"]["value"] == 1.01
    # sell-only threshold must NOT appear in the buy lane
    assert "sell.rsi_place_min" not in t
    assert view["decision_rules"] == {}


def test_get_policy_for_crypto_buy_exposes_report_derived_market_rules():
    view = svc.get_policy_for("crypto", "buy")

    assert view["version"] == "2026-07-12.1"
    assert set(view["market_rules"]) == {
        "recovery_gate",
        "support_resistance",
        "no_chasing",
    }
    gate = view["market_rules"]["recovery_gate"]
    assert gate["min_conditions_met"] == 2
    assert gate["of"] == 4
    assert "lanes" not in gate
    assert (
        view["market_rules"]["no_chasing"]["daily_change_pct_threshold"]
        is None
    )


def test_get_policy_for_filters_crypto_market_rules_by_lane():
    discovery = svc.get_policy_for("crypto", "discovery")["market_rules"]
    assert set(discovery) == {"support_resistance", "no_chasing"}

    sell = svc.get_policy_for("crypto", "sell")["market_rules"]
    assert set(sell) == {"support_resistance"}

    assert svc.get_policy_for("kr", "buy")["market_rules"] == {}


def test_get_policy_for_sell_lane_has_sell_keys():
    view = svc.get_policy_for("kr", "sell")
    t = view["thresholds"]
    assert t["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in t
    rule = view["decision_rules"]["sell.trim_preplace"]
    assert rule["tiers"][0]["id"] == "rsi_confirmed_resistance"
    assert rule["tiers"][0]["conditions"]["rsi_min_policy_key"] == (
        "sell.rsi_place_min"
    )
    assert rule["tiers"][1]["conditions"]["resistance_near_pct_max"] == 2
    assert rule["tiers"][2]["action"] == "register_watch"
    assert rule["tie_breaks"]["sell.upside_place_max_pct"] == "size_limit_only"


def test_unknown_market_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("jp", "buy")


def test_unknown_lane_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("kr", "scalp")


def test_market_override_applied(monkeypatch, tmp_path):
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(svc._POLICY_PATH.read_text(encoding="utf-8"))
    raw["market_overrides"]["us"]["screen.rsi_max"] = 55
    p = tmp_path / "trading_policy.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(svc, "_POLICY_PATH", Path(p))
    svc.load_trading_policy.cache_clear() if hasattr(
        svc.load_trading_policy, "cache_clear"
    ) else None
    svc._reset_cache_for_tests()
    t = svc.get_policy_for("us", "discovery")["thresholds"]
    assert t["screen.rsi_max"]["value"] == 55
    assert t["screen.rsi_max"]["source"] == "override"


def test_sector_cluster_for():
    assert svc.sector_cluster_for("반도체") == "semis_memory"
    assert svc.sector_cluster_for("Financial Services") == "financials"
    assert svc.sector_cluster_for("정체불명업종") is None
    assert svc.sector_cluster_for(None) is None


def test_sector_cluster_for_no_cjk_substring_false_positive():
    # ROB-646 Finding 3: "의료" must not spill into unrelated 업종 like
    # "의료정밀" (medical *precision instruments*). Member "의료" removed +
    # matcher is one-directional (member is a substring of the label, not the
    # reverse), so "의료정밀" resolves to no cluster.
    assert svc.sector_cluster_for("의료정밀") is None


def test_sector_cluster_for_broad_healthcare_not_bio():
    # ROB-646 Finding 3: a generic "Healthcare" sector (e.g. managed-care /
    # health insurers) must not be bucketed as bio.
    assert svc.sector_cluster_for("Healthcare") is None
    assert svc.sector_cluster_for("Healthcare Plans") is None


def test_sector_cluster_for_prefix_coverage_preserved():
    # One-directional match still gives real KR coverage: the Naver 업종
    # "반도체와반도체장비" contains the member "반도체".
    assert svc.sector_cluster_for("반도체와반도체장비") == "semis_memory"
    # yfinance em-dash variants still map (member is a substring of the label).
    assert svc.sector_cluster_for("Drug Manufacturers—General") == "bio"
