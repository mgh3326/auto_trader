from __future__ import annotations

import pytest

import app.services.account.service as account_service_module


@pytest.mark.asyncio
async def test_get_cash_equity_kr_prefers_stck_cash100_max_orderable(monkeypatch):
    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_objt_amt": "5000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                "raw": {
                    "dnca_tot_amt": "5000000.0",
                    "stck_cash_objt_amt": "5000000.0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                },
            }

    monkeypatch.setattr(account_service_module, "KISClient", MockKISClient)

    balances = await account_service_module.get_cash("equity_kr")

    assert len(balances) == 1
    assert balances[0].market == "equity_kr"
    assert balances[0].currency == "KRW"
    assert balances[0].balance == 5000000.0
    assert balances[0].orderable == 3534890.5473
    assert balances[0].source == "kis"


@pytest.mark.asyncio
async def test_get_cash_equity_kr_skips_zero_priority_orderables(monkeypatch):
    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_objt_amt": "5000000.0",
                "stck_cash100_max_ord_psbl_amt": "0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash_ord_psbl_amt": "2100000.25",
                "raw": {
                    "dnca_tot_amt": "5000000.0",
                    "stck_cash_objt_amt": "5000000.0",
                    "stck_cash100_max_ord_psbl_amt": "0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash_ord_psbl_amt": "2100000.25",
                },
            }

    monkeypatch.setattr(account_service_module, "KISClient", MockKISClient)

    balances = await account_service_module.get_cash("equity_kr")

    assert len(balances) == 1
    assert balances[0].market == "equity_kr"
    assert balances[0].currency == "KRW"
    assert balances[0].balance == 5000000.0
    assert balances[0].orderable == 2100000.25
    assert balances[0].source == "kis"
