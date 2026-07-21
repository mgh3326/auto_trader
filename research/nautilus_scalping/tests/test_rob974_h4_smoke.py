import pytest
import rob974_h4_smoke as smoke


def test_contract_fixture_smoke_declares_all_predecessor_integrations() -> None:
    report = smoke.run_contract_fixture_smoke()
    assert report["contract_fixture_h4_smoke"] == "PASS"
    assert report["actual_h1_integration"] == "PASS"
    assert report["actual_h2_integration"] == "PASS"
    assert report["actual_h3_integration"] == "PASS"
    assert report["actual_h6a_integration"] == "PASS"
    assert report["fake_free_full_scope"].startswith("CLOSED_BY_ROB984_CP10:sha256:")
    assert report["fake_free_full_scope"].endswith(
        report["fake_free_full_scope_evidence_sha256"]
    )
    assert len(report["full_campaign_hash"]) == 64
    assert report["campaign_run_id"].startswith("rob974h6a-")


def test_fake_free_full_scope_cannot_close_without_cp10_evidence() -> None:
    marker = getattr(smoke, "_fake_free_full_scope_marker", None)
    assert callable(marker), "R1 requires an evidence-gated H4 smoke marker"
    assert marker(None) == "DEFERRED_TO_H6B_INTEGRATION_E2E"
    with pytest.raises(ValueError, match="evidence receipt differs"):
        marker("f" * 64)
