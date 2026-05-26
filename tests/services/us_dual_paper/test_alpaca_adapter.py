import pytest

from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter


class _FakeCash:
    cash = "100.50"
    buying_power = "200.00"


class _FakeService:
    async def get_cash(self):
        return _FakeCash()

    async def list_positions(self):
        return [object(), object()]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_summarizes_numbers_only():
    adapter = AlpacaPaperAdapter(service_factory=lambda: _FakeService())
    summary = await adapter.read_account_state()
    assert summary.cash_usd == pytest.approx(100.50)
    assert summary.buying_power_usd == pytest.approx(200.00)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical():
    adapter = AlpacaPaperAdapter(service_factory=lambda: _FakeService())
    assert adapter.account_scope == "alpaca_paper"
