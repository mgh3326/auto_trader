import rob974_h4_smoke as smoke


def test_contract_fixture_smoke_declares_all_predecessor_integrations() -> None:
    report = smoke.run_contract_fixture_smoke()
    assert report["contract_fixture_h4_smoke"] == "PASS"
    assert report["actual_h1_integration"] == "PASS"
    assert report["actual_h2_integration"] == "PASS"
    assert report["actual_h3_integration"] == "PASS"
    assert report["actual_h6a_integration"] == "PASS"
    assert report["fake_free_full_scope"] == "DELEGATED_TO_H6B_SCORECARD"
    assert "fake_free_full_scope_evidence_sha256" not in report
    assert len(report["full_campaign_hash"]) == 64
    assert report["campaign_run_id"].startswith("rob974h6a-")


def test_h4_smoke_cannot_self_attest_cp10_closure() -> None:
    assert not hasattr(smoke, "ROB984_CP10_FAKE_FREE_EVIDENCE_SHA256")
    assert not hasattr(smoke, "_fake_free_full_scope_marker")
    report = smoke.run_contract_fixture_smoke()
    assert "CLOSED_BY_ROB984_CP10" not in str(report["fake_free_full_scope"])
