from app.services.action_report.snapshot_backed.action_verdict import demote_for_budget


def _state(basis="available_usd", usd=None, krw=0, override=None):
    return {"basis": basis, "usd": usd, "krw": krw, "override_usd": override}


def test_usd_zero_demotes_to_watch_budget_gap():
    v, reasons = demote_for_budget("buy_review", _state(usd=0, krw=0))
    assert v == "watch_only"
    assert "budget_gap" in reasons
    assert "operator_budget_required" in reasons  # override 없음


def test_usd_zero_with_krw_adds_fx_required():
    _, reasons = demote_for_budget("buy_review", _state(usd=0, krw=500000))
    assert "fx_required" in reasons and "budget_gap" in reasons


def test_usd_positive_keeps_buy():
    assert demote_for_budget("buy_review", _state(usd=1000)) == ("buy_review", [])


def test_override_takes_precedence_over_basis():
    assert demote_for_budget("buy_review",
        _state(basis="available_usd", usd=0, override=500)) == ("buy_review", [])


def test_krw_reference_basis_flags_fx_required():
    v, reasons = demote_for_budget("buy_review",
        _state(basis="krw_orderable_reference", usd=0, krw=500000))
    assert v == "watch_only" and reasons == ["fx_required"]


def test_non_buy_unchanged():
    assert demote_for_budget("watch_only", _state(usd=0)) == ("watch_only", [])
    assert demote_for_budget("rejected", _state(usd=0)) == ("rejected", [])
