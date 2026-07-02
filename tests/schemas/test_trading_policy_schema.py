from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.trading_policy import TradingPolicyDocument

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def test_shipped_config_validates():
    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == "2026-07-02.1"
    # verbatim seed values from the playbook policy_keys
    assert doc.thresholds["portfolio.sector_cluster_cap_pct"].value == 10
    assert doc.thresholds["sell.loss_guard_min_multiple"].value == 1.01
    assert doc.thresholds["screen.rsi_max"].value == 45
    assert doc.thresholds["buy.deep_limit_pct_range"].value == [-12, -3]
    assert set(doc.market_overrides.keys()) == {"kr", "us", "crypto"}
    assert "semis_memory" in doc.sector_clusters


def test_extra_key_rejected():
    raw = _raw()
    raw["unexpected_top_level"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_threshold_key_rejected():
    raw = _raw()
    raw["thresholds"]["screen.rsi_max"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)
