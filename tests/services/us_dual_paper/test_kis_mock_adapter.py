import pytest

from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    """Mock-host client exposing only what KIS mock actually supports.

    Deliberately has NO ``inquire_overseas_margin``: the mock host answers that
    TR with OPSQ0002 ("no such service code"), so an adapter that still calls it
    would raise AttributeError here rather than silently reading a stub.
    """

    def __init__(self):
        self.buyable_calls = 0
        self.holdings_calls = []

    async def inquire_mock_overseas_buyable_amount(self):
        self.buyable_calls += 1
        # Probe-measured VTTS3007R values (2026-07-17 mock host), post-parse.
        return {
            "ovrs_ord_psbl_amt": 99996.18,
            "sll_ruse_psbl_amt": 13.95,
            "exrt": 1488.88,
            "raw": {
                "ord_psbl_frcr_amt": "99996.18",
                "sll_ruse_psbl_amt": "13.95",
                "exrt": "1488.8800",
            },
        }

    async def fetch_my_us_stocks(self, is_mock=False):
        self.holdings_calls.append(is_mock)
        return [{"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "TSLA"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_reads_vtts3007r_buying_power():
    """ROB-951: capability_matrix advertises kis_mock.account_cash_read=True.

    The reader must honour that contract via the same TR the order preflight
    gate uses — not the deprecated OPSQ0002 foreign-margin path.
    """
    fake = _FakeKis()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    assert fake.buyable_calls == 1
    assert fake.holdings_calls == [True]
    assert summary.buying_power_usd == pytest.approx(99996.18)
    # VTTS3007R exposes no separate deposit balance; orderable cash is the
    # single verified figure, matching order_validation._get_available_cash.
    assert summary.cash_usd == pytest.approx(99996.18)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical_kis_mock():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    assert adapter.account_scope == "kis_mock"  # NOT kis_mock_us


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deprecated_overseas_margin_is_never_called():
    """Regression: the OPSQ0002 path must stay unwired."""
    calls = []

    class _FakeKisTracking(_FakeKis):
        async def inquire_overseas_margin(self, is_mock=False):
            calls.append(is_mock)
            raise AssertionError("deprecated OPSQ0002 path must not be called")

    adapter = KisMockUsAdapter(kis_client=_FakeKisTracking(), enabled=True)
    summary = await adapter.read_account_state()
    assert calls == []
    assert summary.buying_power_usd == pytest.approx(99996.18)


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


class _FakeKisBuyableRaises(_FakeKis):
    """VTTS3007R non-zero rt_cd / transport failure surfaces as an exception."""

    async def inquire_mock_overseas_buyable_amount(self):
        self.buyable_calls += 1
        raise RuntimeError("APBK0919 조회할 자료가 없습니다")


class _FakeKisBuyableMissingField(_FakeKis):
    """Output present but the orderable-cash field is absent/unparseable."""

    async def inquire_mock_overseas_buyable_amount(self):
        self.buyable_calls += 1
        return {"ovrs_ord_psbl_amt": None, "sll_ruse_psbl_amt": 13.95, "raw": {}}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake_cls", [_FakeKisBuyableRaises, _FakeKisBuyableMissingField]
)
async def test_buying_power_fail_closed_never_zero(fake_cls):
    fake = fake_cls()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    # Unknown stays unknown — never coerced to 0.0, which would read as
    # "no money" and silently block, or as a satisfied balance check.
    assert summary.buying_power_usd is None
    assert summary.cash_usd is None
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
    assert summary.buying_power_usd == pytest.approx(99996.18)
