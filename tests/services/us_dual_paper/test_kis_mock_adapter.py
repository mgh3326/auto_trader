import pytest

from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    def __init__(self):
        self.margin_calls = []
        self.holdings_calls = []

    async def inquire_overseas_margin(self, is_mock=False):
        self.margin_calls.append(is_mock)
        return [
            {"crcy_cd": "KRW", "natn_name": "한국", "frcr_dncl_amt1": 0.0},
            {
                "crcy_cd": "USD",
                "natn_name": "미국",
                "frcr_dncl_amt1": 500.0,
                "frcr_ord_psbl_amt1": 480.0,
            },
        ]

    async def fetch_my_us_stocks(self, is_mock=False):
        self.holdings_calls.append(is_mock)
        return [{"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "TSLA"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_pins_is_mock_true():
    fake = _FakeKis()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    assert fake.margin_calls == [True]
    assert fake.holdings_calls == [True]
    assert summary.cash_usd == pytest.approx(500.0)
    assert summary.buying_power_usd == pytest.approx(480.0)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical_kis_mock():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    assert adapter.account_scope == "kis_mock"  # NOT kis_mock_us


class _FakeKisWithOrders(_FakeKis):
    def __init__(self):
        super().__init__()
        self.order_calls = []

    async def inquire_overseas_orders(self, exchange_code="NASD", is_mock=False):
        self.order_calls.append((exchange_code, is_mock))
        # one pending order on NASD, one on NYSE, none on AMEX
        if exchange_code == "NASD":
            return [{"odno": "1001"}]
        if exchange_code == "NYSE":
            return [{"odno": "2002"}]
        return []


class _FakeKisOrdersFail(_FakeKis):
    async def inquire_overseas_orders(self, exchange_code="NASD", is_mock=False):
        raise RuntimeError("overseas orders endpoint unsupported in mock")


class _FakeKisMarginUnsupported(_FakeKis):
    """KIS mock real behavior: overseas margin is OPSQ0002 (no service), holdings work."""

    async def inquire_overseas_margin(self, is_mock=False):
        self.margin_calls.append(is_mock)
        raise RuntimeError("OPSQ0002 없는 서비스 코드 입니다")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_margin_unsupported_is_graceful_holdings_still_read():
    fake = _FakeKisMarginUnsupported()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    # overseas margin unsupported in KIS mock (OPSQ0002) → cash/bp None, not a crash
    assert summary.cash_usd is None
    assert summary.buying_power_usd is None
    # holdings read still succeeds on the mock host
    assert summary.position_count == 2
    assert fake.holdings_calls == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_order_count_summed_across_exchanges_is_mock_pinned():
    fake = _FakeKisWithOrders()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    assert summary.open_order_count == 2  # NASD(1) + NYSE(1) + AMEX(0)
    # every overseas-orders read must be is_mock=True
    assert all(is_mock is True for _exch, is_mock in fake.order_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_order_count_none_when_reader_fails():
    adapter = KisMockUsAdapter(kis_client=_FakeKisOrdersFail(), enabled=True)
    summary = await adapter.read_account_state()
    # best-effort: mock may not support overseas open-order reads
    assert summary.open_order_count is None
    assert summary.cash_usd == pytest.approx(500.0)
