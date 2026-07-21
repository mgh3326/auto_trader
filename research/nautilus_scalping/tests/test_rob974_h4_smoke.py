from rob974_h4_smoke import run_contract_fixture_smoke


def test_contract_fixture_smoke_declares_all_predecessor_integrations() -> None:
    report = run_contract_fixture_smoke()
    assert report["contract_fixture_h4_smoke"] == "PASS"
    assert report["actual_h1_integration"] == "PASS"
    assert report["actual_h2_integration"] == "PASS"
    assert report["actual_h3_integration"] == "PASS"
    assert report["actual_h6a_integration"] == "PASS"
    assert report["fake_free_full_scope"] == "DEFERRED_TO_H6B_INTEGRATION_E2E"
    assert len(report["full_campaign_hash"]) == 64
    assert report["campaign_run_id"].startswith("rob974h6a-")
