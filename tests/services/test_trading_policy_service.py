import pytest

from app.services import trading_policy_service as svc


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == "2026-07-02.1"
    assert len(stamp["content_hash"]) == 12


def test_content_hash_stable_across_calls():
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == "2026-07-02.1"
    assert view["content_hash"]
    t = view["thresholds"]
    # buy lane references these (playbook lane tags)
    assert t["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert t["portfolio.sector_cluster_cap_pct"]["source"] == "default"
    assert t["recovery_gate.min_conditions_met"]["value"] == 2
    assert t["sell.loss_guard_min_multiple"]["value"] == 1.01
    # sell-only threshold must NOT appear in the buy lane
    assert "sell.rsi_place_min" not in t


def test_get_policy_for_sell_lane_has_sell_keys():
    t = svc.get_policy_for("kr", "sell")["thresholds"]
    assert t["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in t


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
