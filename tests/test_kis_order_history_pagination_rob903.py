"""ROB-903: display-path pagination cost reduction for KIS order-history.

Root cause (Sentry 24h): ``inquire_daily_order_overseas`` /
``inquire_daily_order_domestic`` paginate until KIS's continuation cursor
(``ctx_area_nk*``) stops advancing or a page is empty. For some symbols KIS
re-emits duplicate rows across continuation pages with an ever-changing cursor,
so pagination runs all the way to ``max_pages`` (100). Each page also pays a
redundant ``asyncio.sleep(0.1)`` on top of the already-enforced 19 req/s KIS
sliding-window rate limiter. Worst case ≈ 100 × (~34ms HTTP + 100ms sleep) ≈
13.4s (~10s of which is pure sleep) — matching the observed p95 = 13.9s.

Two display-scoped, default-preserving knobs are added to the shared broker
pagination methods:

* ``inter_page_delay`` (default 0.1) — the display path passes 0.0 and relies
  on the KIS rate limiter for continuation pacing.
* ``stop_when_no_new_rows`` (default False) — the display path passes True so
  pagination halts once a non-empty page contributes zero *new* order rows
  (the KIS duplicate-cursor signature). Every unique row is already collected,
  so no fill evidence is lost.

Reconcile / fill-evidence callers keep the defaults, so their behavior is
byte-for-byte unchanged (locked by the *invariant* tests below).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_overseas_client():
    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    token_manager = MagicMock()
    token_manager.clear_token = AsyncMock()
    parent._token_manager = token_manager
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    return OverseasOrderClient(parent), parent


def _make_domestic_client():
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    token_manager = MagicMock()
    token_manager.clear_token = AsyncMock()
    parent._token_manager = token_manager
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    return DomesticOrderClient(parent), parent


def _dup_cursor_page(nk: str) -> dict:
    """A page whose rows are all duplicates of page 1 but whose cursor keeps
    advancing — the KIS runaway-pagination signature."""
    return {
        "rt_cd": "0",
        "output1": [{"odno": "001", "pdno": "AAPL"}],
        "ctx_area_fk200": nk,
        "ctx_area_nk200": nk,
    }


@pytest.mark.unit
class TestOverseasDisplayPaginationRob903:
    @pytest.mark.asyncio
    async def test_stop_when_no_new_rows_halts_on_duplicate_cursor(self):
        """Display path: pagination stops as soon as a page adds no new rows."""
        instance, parent = _make_overseas_client()
        # Cursor advances forever, rows never change → without the guard this
        # would run to max_pages and raise "truncated".
        pages = [_dup_cursor_page(f"NK{i}") for i in range(200)]
        parent._request_with_rate_limit = AsyncMock(side_effect=pages)

        result = await instance.inquire_daily_order_overseas(
            start_date="20260317",
            end_date="20260317",
            stop_when_no_new_rows=True,
            inter_page_delay=0.0,
        )

        # Page 1 has a new row; page 2 is all duplicates → stop after 2 calls.
        assert parent._request_with_rate_limit.call_count == 2
        assert result == [{"odno": "001", "pdno": "AAPL"}]

    @pytest.mark.asyncio
    async def test_inter_page_delay_zero_skips_sleep(self, monkeypatch):
        instance, parent = _make_overseas_client()
        pages = [
            {
                "rt_cd": "0",
                "output1": [{"odno": "001", "pdno": "AAPL"}],
                "ctx_area_fk200": "NK1",
                "ctx_area_nk200": "NK1",
            },
            {
                "rt_cd": "0",
                "output1": [{"odno": "002", "pdno": "MSFT"}],
                "ctx_area_fk200": "",
                "ctx_area_nk200": "",
            },
        ]
        parent._request_with_rate_limit = AsyncMock(side_effect=pages)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(
            "app.services.brokers.kis.overseas_orders.asyncio.sleep", sleep_mock
        )

        await instance.inquire_daily_order_overseas(
            start_date="20260317", end_date="20260317", inter_page_delay=0.0
        )

        sleep_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_paginates_exhaustively_reconcile_invariant(self):
        """RECONCILE LOCK: defaults must keep exhaustive pagination — a duplicate
        cursor still runs to max_pages and raises, exactly as today."""
        instance, parent = _make_overseas_client()
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[_dup_cursor_page(f"NK{i}") for i in range(500)]
        )

        with pytest.raises(RuntimeError, match="truncated"):
            await instance.inquire_daily_order_overseas(
                start_date="20260317", end_date="20260317", max_pages=100
            )
        assert parent._request_with_rate_limit.call_count == 100

    @pytest.mark.asyncio
    async def test_default_delay_still_sleeps_between_pages(self, monkeypatch):
        """RECONCILE LOCK: default inter_page_delay keeps the 0.1s spacing."""
        instance, parent = _make_overseas_client()
        pages = [
            {
                "rt_cd": "0",
                "output1": [{"odno": "001", "pdno": "AAPL"}],
                "ctx_area_fk200": "NK1",
                "ctx_area_nk200": "NK1",
            },
            {
                "rt_cd": "0",
                "output1": [{"odno": "002", "pdno": "MSFT"}],
                "ctx_area_fk200": "",
                "ctx_area_nk200": "",
            },
        ]
        parent._request_with_rate_limit = AsyncMock(side_effect=pages)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(
            "app.services.brokers.kis.overseas_orders.asyncio.sleep", sleep_mock
        )

        await instance.inquire_daily_order_overseas(
            start_date="20260317", end_date="20260317"
        )

        sleep_mock.assert_awaited_once_with(0.1)


@pytest.mark.unit
class TestDomesticDisplayPaginationRob903:
    @pytest.mark.asyncio
    async def test_stop_when_no_new_rows_halts_on_duplicate_cursor(self):
        instance, parent = _make_domestic_client()
        pages = [
            {
                "rt_cd": "0",
                "output1": [{"odno": "001", "pdno": "005930"}],
                "ctx_area_fk100": f"NK{i}",
                "ctx_area_nk100": f"NK{i}",
            }
            for i in range(200)
        ]
        parent._request_with_rate_limit = AsyncMock(side_effect=pages)

        result = await instance.inquire_daily_order_domestic(
            start_date="20260317",
            end_date="20260317",
            stop_when_no_new_rows=True,
            inter_page_delay=0.0,
        )

        assert parent._request_with_rate_limit.call_count == 2
        assert result == [{"odno": "001", "pdno": "005930"}]

    @pytest.mark.asyncio
    async def test_default_paginates_exhaustively_reconcile_invariant(self):
        instance, parent = _make_domestic_client()
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output1": [{"odno": "001", "pdno": "005930"}],
                    "ctx_area_fk100": f"NK{i}",
                    "ctx_area_nk100": f"NK{i}",
                }
                for i in range(500)
            ]
        )

        with pytest.raises(RuntimeError, match="truncated"):
            await instance.inquire_daily_order_domestic(
                start_date="20260317", end_date="20260317", max_pages=100
            )
        assert parent._request_with_rate_limit.call_count == 100


@pytest.mark.unit
class TestDisplayFetchersWireFastPagination:
    """The ROB-903 display tool wires the fast-pagination knobs; reconcile does not."""

    @pytest.mark.asyncio
    async def test_fetch_us_orders_passes_fast_pagination_kwargs(self, monkeypatch):
        import app.mcp_server.tooling.orders_history as oh

        captured: dict = {}

        async def fake_overseas(*args, **kwargs):
            captured.update(kwargs)
            return []

        async def fake_pending(*args, **kwargs):
            return []

        fake_client = MagicMock()
        fake_client.inquire_daily_order_overseas = fake_overseas
        fake_client.inquire_overseas_orders = fake_pending
        monkeypatch.setattr(oh, "_create_kis_client", lambda *, is_mock: fake_client)
        monkeypatch.setattr(
            oh, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
        )

        await oh._fetch_us_orders("AAPL", "filled", effective_days=30, is_mock=False)

        assert captured.get("inter_page_delay") == 0.0
        assert captured.get("stop_when_no_new_rows") is True

    @pytest.mark.asyncio
    async def test_fetch_kr_orders_passes_fast_pagination_kwargs(self, monkeypatch):
        import app.mcp_server.tooling.orders_history as oh

        captured: dict = {}

        async def fake_domestic(*args, **kwargs):
            captured.update(kwargs)
            return []

        async def fake_pending(*args, **kwargs):
            return []

        fake_client = MagicMock()
        fake_client.inquire_daily_order_domestic = fake_domestic
        fake_client.inquire_korea_orders = fake_pending
        monkeypatch.setattr(oh, "_create_kis_client", lambda *, is_mock: fake_client)

        await oh._fetch_kr_orders("005930", "filled", effective_days=30, is_mock=False)

        assert captured.get("inter_page_delay") == 0.0
        assert captured.get("stop_when_no_new_rows") is True
