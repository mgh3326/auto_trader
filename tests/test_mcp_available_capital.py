from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_get_available_capital_aggregates_accounts_and_manual_cash(monkeypatch):
    """Test that get_available_capital aggregates broker accounts and manual cash."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {
                    "account": "upbit",
                    "currency": "KRW",
                    "orderable": 1000000.0,
                },
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "orderable": 2000000.0,
                },
                {
                    "account": "kis_overseas",
                    "currency": "USD",
                    "orderable": 100.0,
                },
            ],
            "summary": {"total_krw": 3000000.0, "total_usd": 100.0},
            "errors": [],
        }

    async def mock_get_usd_krw_rate():
        return 1300.0

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 15000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def mock_now_kst():
        return datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", mock_get_usd_krw_rate)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", mock_now_kst)

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["accounts"][0]["account"] == "upbit"
    assert result["accounts"][1]["account"] == "kis_domestic"
    assert result["accounts"][2]["account"] == "kis_overseas"
    assert result["accounts"][2].get("krw_equivalent") == pytest.approx(130000.0)

    assert result["manual_cash"]["amount"] == 15000000
    assert result["manual_cash"]["stale_warning"] is False

    assert result["summary"]["total_orderable_krw"] == pytest.approx(
        1000000.0 + 2000000.0 + 130000.0 + 15000000.0
    )
    assert result["summary"]["exchange_rate_usd_krw"] == pytest.approx(1300.0)
    assert result["summary"]["as_of"] == "2026-04-01T09:00:00+00:00"

    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_available_capital_excludes_manual_when_flag_disabled(monkeypatch):
    """Test that include_manual=False excludes manual cash from aggregation."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {"account": "upbit", "currency": "KRW", "orderable": 1000000.0},
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 5000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(include_manual=False)

    assert result["manual_cash"] is None
    assert result["summary"]["total_orderable_krw"] == pytest.approx(1000000.0)


@pytest.mark.asyncio
async def test_get_available_capital_handles_missing_manual_cash(monkeypatch):
    """Test that missing manual cash is handled gracefully (amount = 0)."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {"account": "upbit", "currency": "KRW", "orderable": 1000000.0}
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return None

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["manual_cash"] is None
    assert result["summary"]["total_orderable_krw"] == pytest.approx(1000000.0)


@pytest.mark.asyncio
async def test_get_available_capital_marks_stale_manual_cash(monkeypatch):
    """Test that manual cash older than 3 days gets stale_warning=True."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [],
            "summary": {"total_krw": 0.0, "total_usd": 0.0},
            "errors": [],
        }

    stale_date = datetime.now(UTC) - timedelta(days=5)

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 1000000},
            "updated_at": stale_date.isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["manual_cash"]["stale_warning"] is True


@pytest.mark.asyncio
async def test_get_available_capital_toss_filter_uses_manual_cash_path(monkeypatch):
    """Test that account='toss' uses the manual cash path."""
    from app.mcp_server.tooling import portfolio_cash

    cash_balance_calls = []

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        cash_balance_calls.append(account)
        return {
            "accounts": [],
            "summary": {"total_krw": 0.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 5000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(account="toss")

    assert "toss" in cash_balance_calls or any(
        call in (None, "toss") for call in cash_balance_calls
    )
    assert result["manual_cash"]["amount"] == 5000000


@pytest.mark.asyncio
async def test_get_available_capital_toss_api_cash_is_reference_only(monkeypatch):
    """Toss API cash is visible as balance but excluded from orderable capital."""
    from decimal import Decimal

    from app.mcp_server.tooling import portfolio_cash
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    async def mock_get_usd_krw_rate():
        return 1300.0

    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_cash, "fetch_toss_cash_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", mock_get_usd_krw_rate)
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(account="toss", include_manual=False)

    assert [account["balance"] for account in result["accounts"]] == [
        123456.0,
        789.01,
    ]
    assert [account["orderable"] for account in result["accounts"]] == [0.0, 0.0]
    assert result["accounts"][1]["krw_equivalent"] == 0.0
    assert result["summary"]["total_orderable_krw"] == 0.0


@pytest.mark.asyncio
async def test_get_available_capital_does_not_add_toss_balance_to_all_account_total(
    monkeypatch,
):
    """All-account orderable total must remain broker-orderable, not Toss balance."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        assert account is None
        return {
            "accounts": [
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "balance": 1_000_000.0,
                    "orderable": 900_000.0,
                },
                {
                    "account": "toss",
                    "broker": "toss",
                    "currency": "KRW",
                    "balance": 123_456.0,
                    "orderable": 0.0,
                },
                {
                    "account": "toss",
                    "broker": "toss",
                    "currency": "USD",
                    "balance": 789.01,
                    "orderable": 0.0,
                },
            ],
            "summary": {"total_krw": 1_123_456.0, "total_usd": 789.01},
            "errors": [],
        }

    async def mock_get_usd_krw_rate():
        return 1300.0

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", mock_get_usd_krw_rate)
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(include_manual=False)

    assert result["accounts"][2]["krw_equivalent"] == 0.0
    assert result["summary"]["total_orderable_krw"] == 900_000.0


@pytest.mark.asyncio
async def test_get_available_capital_excludes_stale_manual_cash_from_total(monkeypatch):
    """Stale manual cash is excluded from total_orderable_krw and reported separately (ROB-467)."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "orderable": 1000000.0,
                },
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    stale_date = datetime.now(UTC) - timedelta(days=70)

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 10000000},
            "updated_at": stale_date.isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    # Stale manual cash stays visible for transparency...
    assert result["manual_cash"]["amount"] == 10000000
    assert result["manual_cash"]["stale_warning"] is True
    # ...but is flagged as excluded and removed from the orderable total.
    assert result["manual_cash"]["included_in_total"] is False
    assert result["summary"]["total_orderable_krw"] == pytest.approx(1000000.0)
    assert result["summary"]["manual_cash_excluded_krw"] == pytest.approx(10000000.0)


@pytest.mark.asyncio
async def test_get_available_capital_includes_fresh_manual_cash_in_total(monkeypatch):
    """Fresh manual cash is included in total with included_in_total=True and zero excluded (ROB-467)."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "orderable": 1000000.0,
                },
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 5000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["manual_cash"]["stale_warning"] is False
    assert result["manual_cash"]["included_in_total"] is True
    assert result["summary"]["total_orderable_krw"] == pytest.approx(6000000.0)
    assert result["summary"]["manual_cash_excluded_krw"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_get_available_capital_adds_cost_profiles(monkeypatch):
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None, **_kwargs):
        return {
            "accounts": [
                {
                    "account": "kis_domestic",
                    "broker": "kis",
                    "currency": "KRW",
                    "orderable": 1_000_000.0,
                },
                {
                    "account": "toss",
                    "broker": "toss",
                    "currency": "KRW",
                    "orderable": 1_000_000.0,
                },
            ],
            "summary": {"total_krw": 2_000_000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_account_costs_setting():
        return {
            "version": 1,
            "routing": {
                "position_consolidation_threshold_bps": {"kr": 25, "us": 40}
            },
            "accounts": {
                "kis_domestic": {
                    "broker": "kis",
                    "markets": {"kr": {"commission_bps": 1.4, "fx_spread_bps": 0}},
                },
                "toss": {
                    "broker": "toss",
                    "markets": {"kr": {"commission_bps": 0, "fx_spread_bps": 0}},
                },
            },
        }

    monkeypatch.setattr(portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl)
    monkeypatch.setattr(portfolio_cash, "get_account_costs_setting", mock_get_account_costs_setting)
    monkeypatch.setattr(portfolio_cash, "get_manual_cash_setting", lambda: None)
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    result = await portfolio_cash.get_available_capital_impl()

    kis = next(row for row in result["accounts"] if row["account"] == "kis_domestic")
    toss = next(row for row in result["accounts"] if row["account"] == "toss")
    assert kis["cost_profile"] == {
        "commission_bps": 1.4,
        "fx_spread_bps": 0.0,
        "source": "user_setting",
        "review_required": False,
    }
    assert toss["cost_profile"]["commission_bps"] == pytest.approx(0)
