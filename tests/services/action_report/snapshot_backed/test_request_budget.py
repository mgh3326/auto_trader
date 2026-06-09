from decimal import Decimal

from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


def _base(**kw):
    return ReportGenerationRequest(
        market="us",
        account_scope="kis_live",
        created_by_profile="p",
        title="t",
        summary="s",
        kst_date="2026-06-09",
        **kw,
    )


def test_budget_basis_defaults_available_usd():
    assert _base().budget_basis == "available_usd"
    assert _base().operator_budget_override_usd is None


def test_budget_override_accepts_decimal():
    r = _base(
        budget_basis="operator_budget_override",
        operator_budget_override_usd=Decimal("1000"),
    )
    assert r.operator_budget_override_usd == Decimal("1000")
